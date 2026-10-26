import os, yaml, torch, argparse
from typing import Iterable, List, Tuple, Optional, Set
from tqdm import tqdm

from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import PPOConfig, PPOTrainer, AutoModelForCausalLMWithValueHead
from peft import LoraConfig, PeftModel
from box import Box
from utils.PPODataset import PPODataset
import packaging.version as V
import trl


with open(args.config, "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

iteration = int(cfg.iteration)

agent_name = "Anomaly Sentinel"
agent_tag = "AD"


def to_query_tensors(tokenizer, texts, device):
    toks = tokenizer(texts, return_tensors=None, padding=False, truncation=True)
    out = []
    for ids in toks["input_ids"]:
        out.append(torch.tensor(ids, dtype=torch.long, device=device))
    return out

def to_response_tensors(tokenizer, texts, device):
    toks = tokenizer(
        texts,
        return_tensors=None,
        padding=False,
        truncation=True,
        add_special_tokens=False,
    )
    out = []
    bos_id = getattr(tokenizer, "bos_token_id", None)
    eos_id = tokenizer.eos_token_id
    for ids in toks["input_ids"]:
        if bos_id is not None and len(ids) > 0 and ids[0] == bos_id:
            ids = ids[1:]
        if len(ids) > 0 and ids[-1] == eos_id:
            ids = ids[:-1]
        if len(ids) == 0:
            ids = [eos_id]
        out.append(torch.tensor(ids, dtype=torch.long, device=device))
    return out

def get_policy_first_device(vh_model: AutoModelForCausalLMWithValueHead):
    base = vh_model.pretrained_model
    prefer_keys = ["embed_tokens", "wte", "word_embeddings", "tok_embeddings", "embeddings"]
    for name, module in base.named_modules():
        w = getattr(module, "weight", None)
        if w is not None and any(k in name for k in prefer_keys):
            dev = w.device
            print(f"[Device] Found embedding-like module '{name}' on {dev}")
            return dev

    for p in base.parameters():
        dev = p.device
        print(f"[Device] Fallback to first parameter device: {dev}")
        return dev

    dev = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print(f"[Device] Ultimate fallback device: {dev}")
    return dev

def detect_lora_targets(model) -> List[str]:
    names: Set[str] = set(n.split(".")[-1] for n, _ in model.named_modules())

    cand_attn = ["q_proj", "k_proj", "v_proj", "o_proj", "qkv_proj", "c_attn", "W_pack"]
    cand_mlp  = ["gate_proj", "up_proj", "down_proj", "fc_in", "fc_out"]

    targets = [n for n in cand_attn + cand_mlp if n in names]

    if any(x in targets for x in ["q_proj","k_proj","v_proj","o_proj"]):
        for m in ["gate_proj","up_proj","down_proj"]:
            if m in names and m not in targets:
                targets.append(m)

    if not targets and "W_pack" in names:
        targets = ["W_pack"]

    if not targets:
        fallback = [x for x in ["q_proj","k_proj","v_proj"] if x in names]
        if fallback:
            targets = fallback

    if not targets:
        raise ValueError(
            "Failed to detect LoRA target_modules. Inspect model.named_modules() "
            "and choose from ['q_proj','k_proj','v_proj','o_proj','gate_proj','up_proj','down_proj'] or ['W_pack']."
        )
    return sorted(set(targets))

def build_peft_config(model):
    targets = detect_lora_targets(model)
    print(f"[LoRA] target_modules = {targets}")
    return LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=targets,
    )

# ========= PPO 训练循环 =========
def fine_tune_offline_ppo(
    model: AutoModelForCausalLMWithValueHead,
    ref_model: AutoModelForCausalLMWithValueHead,
    tokenizer,
    batch_iter: Iterable[List[Tuple[str, str, float]]],
    save_path: str,
    learning_rate: float = 8e-6,
    accumulation_steps: int = 1,
    mini_batch_size: int = 1,
):

    first_device = get_policy_first_device(model)

    ppo_config = PPOConfig(
        learning_rate=learning_rate,
        batch_size=accumulation_steps * mini_batch_size,
        gradient_accumulation_steps=accumulation_steps,
        mini_batch_size=mini_batch_size,
        cliprange=0.1,
        cliprange_value=0.2,
        max_grad_norm=0.3,
        gamma=1.0,
        lam=0.95,
        adap_kl_ctrl=True,
        init_kl_coef=0.2,
        target_kl=4.0,
        horizon=10000,
        accelerator_kwargs={"device_placement": False},
    )

    ppo_trainer = PPOTrainer(
        ppo_config,
        model=model,
        ref_model=ref_model,
        tokenizer=tokenizer,
    )

    last_res = None
    for batch in tqdm(batch_iter, desc="Offline PPO"):
        if len(batch) < accumulation_steps * mini_batch_size:
            continue

        prompts, answers, scores = zip(*batch)

        queries   = to_query_tensors(tokenizer, list(prompts),  first_device)
        responses = to_response_tensors(tokenizer, list(answers), first_device)

        scores_list = [torch.tensor(float(s), dtype=torch.float32, device=first_device) for s in scores]

        n = min(len(queries), len(responses), len(scores_list))
        if n == 0:
            continue
        queries, responses, scores_list = queries[:n], responses[:n], scores_list[:n]
        last_res = ppo_trainer.step(queries=queries, responses=responses, scores=scores_list)


    if last_res is not None:
        print("Last PPO step stats:", last_res)

    os.makedirs(save_path, exist_ok=True)
    ppo_trainer.save_pretrained(save_path)
    ppo_trainer.model.pretrained_model.save_pretrained(save_path)
    print("PPO adapter saved to:", save_path)


print("\n" + "=" * 80)
print(f"[{agent_name}] Building PPODataset ...")

paths = {
    "prompts": os.path.join(cfg.prompt_file_path,  f"{agent_name}_prompts.csv"),
    "answers": os.path.join(cfg.answer_file_path,  f"{agent_name}_answers.csv"),
    "scores":  os.path.join(cfg.scoring_file_path, f"{agent_name}_scorings.csv"),
}

ds = PPODataset(paths)
df = ds.load()
if len(df) == 0:
    print(f"[{agent_name}] No samples, skip.")
    exit(0)

# ---- loding base model and ref model ----
print(f"loading base model/tokenizer ...")
base_model = AutoModelForCausalLM.from_pretrained(
    cfg.llm_setting_agent.model_path,
    torch_dtype=torch.bfloat16,
    attn_implementation="sdpa",
    device_map="auto",
)
tokenizer = AutoTokenizer.from_pretrained(cfg.llm_setting_agent.model_path)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token
tokenizer.padding_side = "right"

# ref_model must be a PreTrainedModelWrapper
print(f"loading ref model ...")
ref_model = AutoModelForCausalLMWithValueHead.from_pretrained(
    cfg.llm_setting_agent.model_path,
    torch_dtype=torch.bfloat16,
    attn_implementation="sdpa",
    device_map="auto",
)
ref_model.pretrained_model.resize_token_embeddings(len(tokenizer))
ref_model.pretrained_model.config.pad_token_id = tokenizer.eos_token_id

ref_model.eval()
for p in ref_model.parameters():
    p.requires_grad_(False)

# ---- batch ----
acc_steps = 4
mini_bs   = 1
batch_iter = ds.iter_ppo_batches(batch_size=acc_steps * mini_bs, shuffle=True)

# ---- LoRA / Value Head（policy）----
prev_peft_path = os.path.join(cfg.peft_file_path, agent_tag, f"iter_{iteration-1}") if iteration > 0 else None # 0
save_peft_path = os.path.join(cfg.peft_file_path, agent_tag, f"iter_{iteration}") #0
print(f"[{agent_name}] Prev adapter: {prev_peft_path or 'None'}")

if iteration > 1:
    # iteration > 1
    print(f"[{agent_name}] get prev adapter: {prev_peft_path}")
    model = PeftModel.from_pretrained(base_model, prev_peft_path, is_trainable=True)
    model = AutoModelForCausalLMWithValueHead.from_pretrained(model)
else:
    # iteration == 1
    init_dir = os.path.join(cfg.peft_file_path, agent_tag, "iter_0")
    peft_cfg = build_peft_config(base_model)
    model = AutoModelForCausalLMWithValueHead.from_pretrained(
        base_model,
        peft_config=peft_cfg,
    )
    model.pretrained_model.save_pretrained(init_dir)
    print(f"[{agent_name}] Init adapter saved to {init_dir}")

model.pretrained_model.resize_token_embeddings(len(tokenizer))

# ---- PPO training ----
print(f"[{agent_name}] Start PPO fine-tuning ...")
fine_tune_offline_ppo(
    model=model,
    ref_model=ref_model,
    tokenizer=tokenizer,
    batch_iter=batch_iter,
    save_path=save_peft_path,
    learning_rate=8e-6,
    accumulation_steps=acc_steps,
    mini_batch_size=mini_bs,
)
