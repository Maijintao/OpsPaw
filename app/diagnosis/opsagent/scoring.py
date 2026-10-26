
import os, yaml, csv, time
import pandas as pd
from box import Box
from agents.judge import Judge
from concurrent.futures import ThreadPoolExecutor

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))


os.environ["CUDA_VISIBLE_DEVICES"] = "3, 4"

baseline_path = os.path.join("/data/luoyu6/OpsAgent_jm/OpsAgent/dataset", cfg.query_dataset, "train_answers.csv")
baselines = pd.read_csv(baseline_path)

ad_answers   = pd.read_csv(os.path.join(cfg.answer_file_path,   "Anomaly Sentinel_answers.csv"))
ad_thinkings = pd.read_csv(os.path.join(cfg.thinking_file_path, "Anomaly Sentinel_thinking.csv"))

ft_answers   = pd.read_csv(os.path.join(cfg.answer_file_path,   "Failure Diagnoser_answers.csv"))
ft_thinkings = pd.read_csv(os.path.join(cfg.thinking_file_path, "Failure Diagnoser_thinking.csv"))

rcl_answers   = pd.read_csv(os.path.join(cfg.answer_file_path,   "Root Detective_answers.csv"))
rcl_thinkings = pd.read_csv(os.path.join(cfg.thinking_file_path, "Root Detective_thinking.csv"))

os.makedirs(cfg.scoring_file_path, exist_ok=True)
ad_scoring_file_path  = os.path.join(cfg.scoring_file_path, "Anomaly Sentinel_scorings.csv")
ft_scoring_file_path  = os.path.join(cfg.scoring_file_path, "Failure Diagnoser_scorings.csv")
rcl_scoring_file_path = os.path.join(cfg.scoring_file_path, "Root Detective_scorings.csv")

def score_agent(agent_name, answers_df, thinkings_df, out_path):

    judge = Judge()

    n = min(len(baselines), len(answers_df), len(thinkings_df))
    if n == 0:
        return

    need_header = not os.path.exists(out_path)

    with open(out_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if need_header:
            writer.writerow(["score"])

        for i in range(n):
            pred     = answers_df.iloc[i]["answer"]
            thinking = thinkings_df.iloc[i]["thinking"]
            baseline = baselines.iloc[i]["scoring_points"]

            score = judge.evaluate(pred, thinking, baseline)

            print(f"[{agent_name} #{i}] total={score['total_reward']}, bonus={score['correctness_bonus']}, "
                    f"C={score['consistency']}, Cl={score['clarity']}, R={score['relevance']}, S={score['rationality']}")

            writer.writerow([score["total_reward"]])

start_time = time.time()
score_agent("Anomaly Sentinel", ad_answers,  ad_thinkings,  ad_scoring_file_path)
score_agent("Failure Diagnoser", ft_answers,  ft_thinkings,  ft_scoring_file_path)
score_agent("Root Detective",   rcl_answers, rcl_thinkings, rcl_scoring_file_path)
end_time = time.time()
exit(0)
