import os
import csv
import re
import json
import pandas as pd

dataset_list = ['Bank', 'Market/cloudbed-1/', 'Market/cloudbed-2', 'Telecom']

def extract_reasons(text):
    reasons = []
    lines = str(text).split('\n')
    for line in lines:

        m = re.search(r"(?:The \d+-th\s+)?(?:predicted )?root cause reason is\s+(.+)", line)
        if m:

            reason = m.group(1).strip()
            reasons.append(re.sub(r'\s+', ' ', reason))
    return reasons

def extract_components(text):
    components = []
    lines = str(text).split('\n')
    for line in lines:
        m = re.search(r"(?:The (?:\d+-th|only)\s+)?(?:predicted )?root cause component is\s+([A-Za-z0-9_-]+)", line)
        if m:
            components.append(m.group(1).strip())
    return components

def extract_times(text):

    return re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(text))

for idx, dataset in enumerate(dataset_list):
    query_path = os.path.join("/data/luoyu6/OpsAgent_jm/OpsAgent/dataset", dataset, "query.csv")
    df = pd.read_csv(query_path)

    baseline_path = os.path.join("/data/luoyu6/OpsAgent_jm/OpsAgent/dataset", dataset, "baseline_result.csv")
    with open(baseline_path, 'w', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['task_index', 'scoring_points'])
        for i, row in df.iterrows():
            task_index = row['task_index']
            single_scoring_points = row['scoring_points']
            times = extract_times(single_scoring_points)
            components = extract_components(single_scoring_points)
            reasons = extract_reasons(single_scoring_points)
            result = times + components + reasons
            result_str = json.dumps(result, ensure_ascii=False)
            writer.writerow([task_index, result_str])
