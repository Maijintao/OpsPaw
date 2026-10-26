import os, json, re, yaml, torch
from box import Box
from typing import Any, Dict, List
from transformers import AutoModelForCausalLM, AutoTokenizer

from prompts.judge_prompt import JUDGE_SYSTEM_PROMPT
from utils.public_function import llm_invoke, clear_inference_memory
from eval import normalize, is_time, time_within_one_minute

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

class Judge:
    def __init__(self):
        self.model_path = cfg.llm_setting_agent.model_path
        self.max_token = int(getattr(cfg.llm_setting_agent, "max_token", 1024))
        self.max_retry = int(getattr(cfg.llm_setting_agent, "max_retry", 3))
        self.system_prompt = JUDGE_SYSTEM_PROMPT

        self.alpha = 0.3
        self._model = None
        self._tokenizer = None

    def evaluate(self, answer: Any, thinking: str, baseline_answer: Any) -> Dict[str, Any]:
        pred_list = self._to_list(answer)
        gold_list = self._to_list(baseline_answer)

        bonus = 5 if self._fully_correct(pred_list, gold_list) else 0

        user_prompt = self._build_user_prompt(thinking, pred_list)
        response = self._call_llm(self.system_prompt, user_prompt)
        scores = self._parse_scores(response)

        # scores * (1 - alpha) + alpha * bonus
        avg_four = (scores["consistency"] + scores["clarity"] + scores["relevance"] + scores["rationality"]) / 4.0
        total = avg_four * (1.0 - self.alpha) + self.alpha * bonus

        return {
            "consistency": scores["consistency"],
            "clarity": scores["clarity"],
            "relevance": scores["relevance"],
            "rationality": scores["rationality"],
            "correctness_bonus": bonus,
            "total_reward": total,
            "notes": scores.get("notes", "")
        }

    def _ensure_model(self):
        if self._model is not None and self._tokenizer is not None:
            return
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        try:
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_path, torch_dtype=dtype, attn_implementation="sdpa", device_map="auto"
            )
        except Exception:
            self._model = AutoModelForCausalLM.from_pretrained(self.model_path, device_map="auto")
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path, truncation=True)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        self._ensure_model()
        out = ""
        for _ in range(self.max_retry):
            out, _ = llm_invoke(
                self._model, self._tokenizer,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_token=self.max_token,
            )
            if out:
                break
        clear_inference_memory()
        if isinstance(out, dict) and "content" in out:
            return str(out["content"])
        return str(out)


    @staticmethod
    def _build_user_prompt(chain: str, answers: List[str]) -> str:
        answer = "\n".join(map(str, answers)) if answers else "(none)"
        return (
                f"""Task: Evaluate the following reasoning chain and final answer.

                [Reasoning Chain]
                {chain or "(empty)"}

                [Predicted Answer(s)]
                {answer}

                Return JSON ONLY:
                {{"consistency":0,"clarity":0,"relevance":0,"rationality":0,"notes":""}}"""
                )

    def _parse_scores(self, text: str) -> Dict[str, Any]:
        js = self._safe_json(text)

        def getf(k):
            v = js.get(k, 0)
            try:
                v = float(v)
            except:
                v = 0.0
            return max(0.0, min(5.0, v))

        return {
            "consistency": getf("consistency"),
            "clarity": getf("clarity"),
            "relevance": getf("relevance"),
            "rationality": getf("rationality"),
            "notes": js.get("notes", "")
        }


    @staticmethod
    def _safe_json(text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", str(text))
        if m:
            chunk = m.group(0)
            for cand in (chunk, chunk.replace("'", '"')):
                try:
                    return json.loads(cand)
                except Exception:
                    continue
        return {}

    @staticmethod
    def _fully_correct(pred_list: List[str], gold_list: List[str]) -> bool:
        if not gold_list:
            return False
        matched = []
        for b in gold_list:
            if is_time(b):
                found = any(is_time(f) and time_within_one_minute(b, f) for f in pred_list)
            elif " " in b:
                found = any(normalize(f) == normalize(b) for f in pred_list)   # 错误类型（含空格）
            else:
                found = any(normalize(f) == normalize(b) for f in pred_list)   # 实例（无空格）
            matched.append(found)
        return any(matched)

    @staticmethod
    def _to_list(x: Any) -> List[str]:
        if x is None:
            return []
        if isinstance(x, list):
            return [str(i).strip() for i in x]
        s = str(x).strip()
        if not s:
            return []
        try:
            v = json.loads(s)
            if isinstance(v, list): return [str(i).strip() for i in v]
            if isinstance(v, str):  return [v.strip()]
        except Exception:
            pass
        try:
            import ast as _ast
            v = _ast.literal_eval(s)
            if isinstance(v, list): return [str(i).strip() for i in v]
            if isinstance(v, str):  return [v.strip()]
        except Exception:
            pass
        return [s]
