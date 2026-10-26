import re
import ast
import torch
import yaml
import csv
import os
from abc import abstractmethod
from transformers import AutoModelForCausalLM, AutoTokenizer
from utils.public_function import llm_invoke, clear_inference_memory
from utils.MessagePool import MessagePool, Message
from utils.ReviewPool import ReviewPool, Review
from reflection.ReflectionVector import ReflectionVector
from typing import List, Optional
from box import Box
from prompts.generate_symptom_prompt import GEN_SYMPTOM_SYSTEM_PROMPT
from prompts.reflect_prompt import REFLECT_SYSTEM_PROMPT

from peft import PeftModel, LoraConfig
# trl 仅 PPO 训练分支使用;推理链不依赖。Trained weights 不随仓库分发,
# 推理环境无 trl 时降级为不可训练(try/except 为 OpsPaw 适配,原行为不变)。
try:
    from trl import PPOConfig, PPOTrainer, AutoModelForCausalLMWithValueHead
except ImportError:
    PPOConfig = PPOTrainer = AutoModelForCausalLMWithValueHead = None

# loading configuration
with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

class AgentContext:
    def __init__(
        self,
        llm_path: str = "",
        peft_path: str = "",
        max_token: int = 1024,
        think_system_prompt: str = "",
        review_system_prompt: str = "",
        refine_system_prompt: str = "",
        user_prompt: str = "",
        memory: Optional[List['Message']] = None
    ):
        self.llm_path = llm_path                                    # llm model path
        self.peft_path = peft_path                                  # LoRA adaptor path
        self.max_token = max_token                                  # llm model max tokens
        self.think_system_prompt = think_system_prompt              # system prompt used to think
        self.review_system_prompt = review_system_prompt            # system prompt used to review
        self.refine_system_prompt = refine_system_prompt            # system prompt used to refine
        self.reflect_system_prompt = REFLECT_SYSTEM_PROMPT          # system prompt used to reflect
        self.gen_symptom_system_prompt = GEN_SYMPTOM_SYSTEM_PROMPT  # system prompt used to generate symptom during thinking process
        self.user_prompt = user_prompt                              # user prompt(feature after dataProcesser)
        self.memory = memory if memory is not None else []          # refined message after cross-review

    def add_message(self, msg: 'Message') -> None:
        self.memory.append(msg)

    def last_message(self) -> Optional['Message']:
        return self.memory[-1] if self.memory else None

    # def clear(self) -> None:
    #     self.memory.clear()

class BaseAgent:
    MAX_RETRY = 3
    def __init__(
        self,
        name: str,
        failure_count: int,
        agent_context: 'AgentContext',
        message_pool: 'MessagePool',
        review_pool: 'ReviewPool',
        vector_db: 'ReflectionVector'
    ):
        self.name = name
        self.failure_count = failure_count
        self.state = 1  # 1: 工作中, 0: 不工作
        self.agent_context = agent_context
        self.message_pool = message_pool
        self.review_pool = review_pool
        self.vector_db = vector_db
        self.agent_context.reflect_system_prompt = self.agent_context.reflect_system_prompt.format(agent_name=self.name)
        self.agent_context.gen_symptom_system_prompt = self.agent_context.gen_symptom_system_prompt.format(agent_name=self.name)
        self.reflect_message = None

        # use adaptor
        if cfg.run_mode == "ppo_logging":
            self._init_model_with_optional_peft(self.agent_context.llm_path, peft_path="")
        else:
            self._init_model_with_optional_peft(self.agent_context.llm_path, self.agent_context.peft_path)

        self.tokenizer = AutoTokenizer.from_pretrained(self.agent_context.llm_path)
        self.tokenizer.pad_token = self.tokenizer.eos_token


    def _init_model_with_optional_peft(self, llm_path: str, peft_path: str):
        #1 base model
        base = AutoModelForCausalLM.from_pretrained(
            llm_path,
            torch_dtype=torch.bfloat16,
            attn_implementation="sdpa",
            device_map="auto",
        )
        if peft_path and os.path.isdir(peft_path):
            print(f"[INFO] Loading agent after fine-tuning... LoRA adapter found at: {peft_path}")
            #2 LoRA adapter
            peft = PeftModel.from_pretrained(base, peft_path, is_trainable=False)
            #3 value head
            model = AutoModelForCausalLMWithValueHead.from_pretrained(peft)
        else:
            print(f"[INFO] {peft_path} not provided or directory not found, using base model instead.")
            model = base

        self.generation_model = getattr(model, "pretrained_model", model)
        self.model = model

        try:
            self.model.device = self.model.current_device
        except Exception:
            try:
                self.model.device = next(self.generation_model.parameters()).device
            except Exception:
                pass


    def observe(self, target_name: str) -> list[Message]:
        new_msg = self.message_pool.get_latest_by_role(target_name)

        # debug
        if new_msg:
            print(f"\n--- {self.name} observed new messages ---")
            print("  " + new_msg.msg2str().replace('\n', '\n  '))
        else:
            print(f"\n--- {self.name} observed: No new messages. ---")

        return new_msg

    def think(self):
        run_mode = cfg.run_mode
        think_user_prompt = self.agent_context.user_prompt

        # ===== RAG + few-shot  =====
        if run_mode == "normal":
            #1. generator symptom by llm
            gen_symptom_user_prompt = f"""
                                        Processed Data: {self.agent_context.user_prompt}
                                        Please generate symptoms based on the above information.
                                        """
            response, _ = self._call_llm(self.agent_context.gen_symptom_system_prompt, gen_symptom_user_prompt)
            print(f"\n--- {self.name} generates symptom before think ---")
            print("symptom is: ", response)
            print("-" * 60)

            symptoms_match = re.search(r"<\s*symptoms\s*>(.*?)<\s*/\s*symptoms\s*>", response, re.DOTALL | re.IGNORECASE)

            cur_symptom = ""
            if symptoms_match:
                cur_symptom = symptoms_match.group(1).strip()


            #2. get experience by RAG
            similar_experiences = []
            if cur_symptom:
                try:
                    similar_experiences = self.vector_db.search_similar_symptoms(cur_symptom, top_k=3)
                except Exception as e:
                    print(f"[Warning] search_similar_symptoms failed: {e}")
                    similar_experiences = []

            #3. generator example as few-shot
            few_shot = ""
            if similar_experiences:
                try:
                    valid_pairs = []
                    for item in similar_experiences:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            s, e = item
                        elif isinstance(item, dict):
                            s = item.get("symptom") or item.get("text") or ""
                            e = item.get("experience") or item.get("content") or ""
                        else:
                            continue
                        if s and e:
                            valid_pairs.append((str(s).strip(), str(e).strip()))

                    few_shot = "\n".join([f"symptom: {s}\nexperience: {e}" for s, e in valid_pairs])

                except Exception as e:
                    print(f"[Warning] building few_shot failed: {e}")
                    few_shot = ""

            # debug
            print(f"\n--- {self.name} get few-shot ---")
            print("few shots: ", few_shot)
            print("-" * 60)

            think_user_prompt += f"""
                                symptom: {cur_symptom}
                                Please use the following similar symptoms and experiences as reference to generate your answer and thinking:
                                {few_shot}
                                """

        response, final_prompt = self._call_llm(self.agent_context.think_system_prompt, think_user_prompt)

        # parsing
        thinking_match = re.search(r"<thinking>(.*?)</thinking>", response, re.DOTALL)
        answer_match = re.search(r"<answer>(.*?)</answer>", response)

        answer = ast.literal_eval(answer_match.group(1).strip()) if answer_match else []
        thinking = thinking_match.group(1).strip() if thinking_match else ""

        new_msg = Message(self.name, answer, thinking, len(self.agent_context.memory) + 1)
        self.message_pool.push(new_msg)
        self.reflect_message = new_msg

        print(f"\n--- {self.name} think new messages ---")
        print("  " + new_msg.msg2str().replace('\n', '\n  '))

        if run_mode == "ppo_logging":
            # ==== prompt ====
            prompt_file = os.path.join(cfg.prompt_file_path, f"{self.name}_prompts.csv")
            prompt_exists = os.path.exists(prompt_file)
            with open(prompt_file, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if not prompt_exists:
                    writer.writerow(["prompt"])
                writer.writerow([final_prompt])

            # ==== answer ====
            answer_file = os.path.join(cfg.answer_file_path, f"{self.name}_answers.csv")
            answer_exists = os.path.exists(answer_file)
            with open(answer_file, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if not answer_exists:
                    writer.writerow(["answer"])
                writer.writerow([answer])

            # ==== thinking ====
            thinking_file = os.path.join(cfg.thinking_file_path, f"{self.name}_thinking.csv")
            thinking_exists = os.path.exists(thinking_file)
            with open(thinking_file, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if not thinking_exists:
                    writer.writerow(["thinking"])
                writer.writerow([thinking])

    def cross_review(self, target_name: str) -> list[Review]:
        new_msg = self.observe(target_name)
        reviews: list[Review] = []

        self_msg = self.agent_context.last_message()
        if self_msg is None:
            # If it's the first round, go to MessagePool and pull the lastest message
            self_msg = self.message_pool.get_latest_by_role(self.name)

        # cross review
        review_user_prompt = f"""
            Please review the following answer and thinking.\n
            The answer from {new_msg.role_name}:
            <answer>{new_msg.answer}</answer>
            <thinking>{new_msg.thinking}</thinking>"""

        response, _ = self._call_llm(self.agent_context.review_system_prompt, review_user_prompt)

        # parsing
        comment_match = re.search(r"<comment>(.*?)</comment>", response, re.DOTALL)
        accept_match = re.search(r"<accept>(.*?)</accept>", response)

        comment = comment_match.group(1).strip() if comment_match else "No comment"
        accept_str = accept_match.group(1).strip() if accept_match else "False"
        accept = accept_str.lower() == "true"

        review = Review(reviewer=self.name,
                        target=new_msg.role_name,
                        comment=comment,
                        accept=accept,
                        round_num=new_msg.round_num
                )
        self.review_pool.push(review)
        reviews.append(review)

        if reviews:
            print(f"\n--- {self.name} generated {len(reviews)} reviews ---")
            for r in reviews:
                print("  " + r.rev2str().replace("\n", "\n  "))
        else:
            print(f"\n--- {self.name} found nothing to review ---")

        return reviews

    def refine(self) -> Message:
        reviews = self.review_pool.get_latest_by_target(self.name)
        last_refined_msg = self.agent_context.last_message()
        if last_refined_msg is None:
            # If it's the first refine, go to MessagePool and pull the lastest message
            last_refined_msg = self.message_pool.get_latest_by_role(self.name)

        if not reviews:
            print(f"\n--- {self.name} has no reviews for round {last_refined_msg.round_num}, answer considered good enough ---")
            return last_refined_msg

        review_text = "\n".join([f"- {r.comment} (accept={r.accept})" for r in reviews])

        refine_user_prompt = self.agent_context.user_prompt +  f""" My last answer is:
        <answer>{last_refined_msg.answer}</answer>
        <thinking>{last_refined_msg.thinking}</thinking>\n
        Below are reviews from other agents:
        {review_text}
        Please improve my answer based on these comments. There may be more than one candidate answer, but please keep the number of final answers consistent with the number of faults **{self.failure_count}**.
        """
        response, _ = self._call_llm(self.agent_context.think_system_prompt, refine_user_prompt)

        # parsing
        thinking_match = re.search(r"<thinking>(.*?)</thinking>", response, re.DOTALL)
        answer_match = re.search(r"<answer>(.*?)</answer>", response)

        thinking = thinking_match.group(1).strip() if thinking_match else ""
        try:
            answer = ast.literal_eval(answer_match.group(1).strip()) if answer_match else []
        except:
            answer = [answer_match.group(1).strip()] if answer_match else []

        new_msg = Message(
            role_name=self.name,
            answer=answer,
            thinking=thinking,
            round_num=len(self.agent_context.memory) + 1
        )

        self.agent_context.add_message(new_msg)
        print(f"\n--- {self.name} refine messages ---")
        print("  " + new_msg.msg2str().replace('\n', '\n  '))
        return new_msg

    def reflect(self, baseline: str):

        # build reflect user prompt
        reflect_user_prompt = f"""
                                Ground truth Result: {baseline}
                                Processed Data: {self.agent_context.user_prompt}
                                Your predicted answer: {self.reflect_message.answer}
                                Your predicted thinking: {self.reflect_message.thinking}
                                Please reflect based on the above information.
                                """

        # generate reflection
        response, _ = self._call_llm(self.agent_context.reflect_system_prompt, reflect_user_prompt)

        symptoms = []
        experience = []

        # parsing
        symptoms_match = re.search(r"<symptoms>(.*?)</symptoms>", response, re.DOTALL)
        experience_match = re.search(r"<experience>(.*?)</experience>", response, re.DOTALL)

        if symptoms_match:
            symptoms = symptoms_match.group(1).strip()
        if experience_match:
            experience = experience_match.group(1).strip()

        # debug
        print(f"\n--- {self.name} reflects ---")
        print("symptoms: " + (symptoms or "None").replace("\n", "\n  "))
        print("experience: " + (experience or "None").replace("\n", "\n  "))
        print("-" * 60)

        # save reflection
        file_path = os.path.join(cfg.reflection_file_path, f"{self.name}_reflect.csv")
        file_exists = os.path.exists(file_path)

        with open(file_path, mode="a", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["symptoms", "experience"])
            writer.writerow([symptoms, experience])

    def _call_llm(self, system_prompt: str, user_prompt: str):
        response, final_prompt = None, None
        for attempt in range(1, self.MAX_RETRY + 1):
            response, final_prompt = llm_invoke(
                self.generation_model,
                self.tokenizer,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_token=self.agent_context.max_token,
            )
            if response:
                break
        clear_inference_memory()
        return response, final_prompt
