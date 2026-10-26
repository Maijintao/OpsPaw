import os
import csv
import time
import torch
import pandas as pd
import yaml
from tqdm import tqdm
from agents.orchestrator import CoreOrchestrator
from agents.intent_interpreter import Intent_Interpreter
from reflection.ReflectionVector import ReflectionVector
from utils.public_function import Eval_MAS, Eval_MAS_single
from box import Box

# loading configuration
with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

def execute(queries: list, case_number: int, orchestrator: CoreOrchestrator,
            vdbs: list, baseline: str=None):
    """
    Args:
        query: from dataset 'instruction' column. e.g. On March 4,2021,within the time range of 14:30 to 15:00,a single failure was detected in the system.The exact time when the root cause occurred is unknown.Please identify the specific occurrence time of the root cause.
        result: from dataset 'scoring_points' column. e.g. The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-0414:57:00

    Returns:
        answer: exp answer
        thinkings: LLM thinking output
    """
    time_window, task, number = Intent_Interpreter.interprete_query(queries)
    print(time_window, task, number)
    answer, thinkings = orchestrator.process_task(time_window, task, number, case_number, vdbs, baseline)

    return answer, thinkings

def exp_no_finetune():
    orchestrator = CoreOrchestrator(model_path, dataset)

    ad_reflection_file = os.path.join(cfg.reflection_file_path, f"Anomaly Sentinel_reflect.csv")
    ft_reflection_file = os.path.join(cfg.reflection_file_path, f"Failure Diagnoser_reflect.csv")
    rcl_reflection_file = os.path.join(cfg.reflection_file_path, f"Root Detective_reflect.csv")
    ad_vdb = ReflectionVector(ad_reflection_file)
    ft_vdb = ReflectionVector(ft_reflection_file)
    rcl_vdb = ReflectionVector(rcl_reflection_file)

    answers = []
    final_results = []

    query_path = os.path.join("XXX/OpsAgent/dataset", query_dataset,  "40_query.csv") # please update to your local path
    query_data = pd.read_csv(query_path)
    queries = query_data['instruction']
    baselines = query_data['scoring_points']

    if not os.path.exists("results/exp/PPO/60/Market/cloudbed-2/answers_cloudbed-2.csv"):
        with open("results/exp/PPO/60/Market/cloudbed-2/answers_cloudbed-2.csv", "w", newline='', encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["query_id", "answer"])

    if not os.path.exists("results/exp/PPO/60/Market/cloudbed-2/final_thinkings_cloudbed-2.csv"):
        with open("results/exp/PPO/60/Market/cloudbed-2/final_thinkings_cloudbed-2.csv", "w", newline='', encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["query_id", "final_thinking"])

    start_time = time.time()

    for i in tqdm(range(len(queries)),desc="Processing queries: "):
        if i < 25:
            continue
        query = queries[i]
        baseline = baselines[i]
        answer, thinking = execute(query, i, orchestrator, [ad_vdb, ft_vdb, rcl_vdb], baseline)
        answers.append(answer)

        with open("results/exp/PPO/60/Market/cloudbed-2/answers_cloudbed-2.csv", "a", newline='', encoding="utf-8") as f_ans:
            writer = csv.writer(f_ans)
            writer.writerow([i, answer])

        with open("results/exp/PPO/60/Market/cloudbed-2/final_thinkings_cloudbed-2.csv", "a", newline='', encoding="utf-8") as f_res:
            writer = csv.writer(f_res)
            writer.writerow([i, thinking])

    end_time = time.time()

if __name__ == "__main__":
    model_path = cfg.llm_setting_agent.model_path
    dataset = cfg.dataset
    query_dataset = cfg.query_dataset

    exp_no_finetune()
