from sklearn.feature_extraction.text import TfidfVectorizer
from drain3 import TemplateMiner
import pytz
import pandas as pd
from datetime import datetime
from collections import defaultdict


shanghai_tz = pytz.timezone('Asia/Shanghai')

class DataProcessor:
    def extract_metric_features(metrics):
        """
        Args:
            metrics: a dict mapping each instance name to its pivoted metric DataFrame
                Each DataFrme uses timestamp as the index, kpi_name as columns, metric value as the table content
                which Nan filled by 0.
                e.g. metric["Tomcat04"]:
                timestamp    kpi_name       value
                             OSLinux-OSLinux_FILE_-tmp-zabbix_agentd.log_FileSizeMB  | OSLinux-CPU_CPU_CPUCpuUtil
                1614788220   1.0106                                                  |
                1614778200                                                           |  26.2957

        """
        filtered_metrics = {}
        anomaly_timestamps = {}
        deviation_scores = defaultdict(list)  # key -> list of deviations

        # 1. 3-sigma anomaly detection with deviation score
        for key, df in metrics.items():
            retained_cols = []
            col_anomalies = {}

            # iterate each column(kpi_name)
            for col in df.columns:
                series = df[col]
                mean = series.mean()
                std = series.std()
                if std == 0 or np.isnan(std):
                    continue

                # |series - mean| > 3*std
                z_scores = ((series - mean).abs()) / std
                outliers = z_scores > 3

                """
                catch outliers,
                1. append to retrained_cols
                2. get the DataFrame index(e.g. timestamp) and store to anomaly_times
                3. store outliers'z-scores to deviations
                4. zip all outliers to pair {timestamp, z-scores}
                5. form {key = [instance], value = ['kpi_name', 'timestamp', 'z-scores']} to deviation_scores
                """
                if outliers.any():
                    retained_cols.append(col)
                    anomaly_times = series.index[outliers].tolist()
                    deviations = z_scores[outliers].tolist()

                    col_anomalies[col] = list(zip(anomaly_times, deviations))
                    for t, d in zip(anomaly_times, deviations):
                        deviation_scores[key].append((col, t, d))
            """
            filtered_metrics: key = {instance}, {'bad kpi_name1', 'bad kpi_name2', ...}
            anomaly_timestamps: key = {instance}, { outliers1{timestamp, scores}, outliers2{timestamp, scores}, ...}
            """
            filtered_metrics[key] = df[retained_cols]
            anomaly_timestamps[key] = col_anomalies

        classifier = CNNClassifier()
        classifier.load_model("patterncla.pt")

        aggregated_descriptions = defaultdict(list)
        pattern_deviation = defaultdict(list)  # (key, col, pattern) -> list of deviation values

        for key, df in filtered_metrics.items():
            if key not in anomaly_timestamps:
                continue

            for col, t_dev_list in anomaly_timestamps[key].items():
                for t, dev in t_dev_list:
                    try:
                        all_timestamps = df.index.tolist()
                        idx = all_timestamps.index(t)
                        pre_len = 20
                        post_len = 10

                        start_idx = max(0, idx - pre_len)
                        end_idx = min(len(all_timestamps), idx + post_len)
                        if end_idx - start_idx < 30:
                            pad = 30 - (end_idx - start_idx)
                            if start_idx > pad:
                                start_idx -= pad
                            else:
                                end_idx = min(len(all_timestamps), end_idx + pad)

                        if end_idx - start_idx < 30:
                            continue

                        window_idx = all_timestamps[start_idx:end_idx]
                        x = df.loc[window_idx, col].values

                        feature = classifier.predict(x)[0]
                        anomaly_type = ['Level shift up','Level shift down','Steady increase','Steady decrease','Single spike','Single dip','Transient level shift up','Transient level shift down','Multiple spikes','Multiple dips','Fluctuations']
                        anomaly_type_str = anomaly_type[feature]

                        group_key = (key, col, anomaly_type_str)
                        aggregated_descriptions[group_key].append(timestamp_to_shanghai_str(t))
                        pattern_deviation[group_key].append(dev)

                    except Exception as e:
                        print(f"Error processing {key}:{col} at {t}: {e}")
                        continue

        """
        top pods ==> top metric group by pods
        """
        pod_score = defaultdict(float)
        metric_score = defaultdict(float)

        for key, dev_list in deviation_scores.items():
            pod_score[key] = sum([dev for (_, _, dev) in dev_list])

        top_pods = sorted(pod_score.items(), key=lambda x: x[1], reverse=True)[:5]
        top_pod_names = [x[0] for x in top_pods]

        for pod in top_pod_names:
            for col, _, dev in deviation_scores[pod]:
                metric_score[col] += dev

        top_metrics = sorted(metric_score.items(), key=lambda x: x[1], reverse=True)[:5]
        top_metric_names = [x[0] for x in top_metrics]
        descriptions = []
        descriptions.append("Top-5 Pods with largest deviations:")
        descriptions.extend(top_pod_names)
        descriptions.append("")
        descriptions.append("Top-5 Metrics with largest deviations:")
        descriptions.extend(top_metric_names)
        descriptions.append("")
        num_peaks = 10
        num_dev = 6
        sorted_items = sorted(
            aggregated_descriptions.items(),
            key=lambda item: max(pattern_deviation[item[0]]),
            reverse=True
        )

        for (host, metric, anomaly_type), peaks in sorted_items[0:24]:
            max_dev = max(pattern_deviation[(host, metric, anomaly_type)])
            if len(peaks) > num_peaks and max_dev < num_dev:
                continue

            if len(peaks) >= 3:
                time_summary = f"{peaks[0]} ~ {peaks[-1]} ({len(peaks)} times)"
            else:
                time_summary = ", ".join([p.split()[1] for p in peaks])
            """
            store in metric_features.txt
            e.g.
                Mysql02 | OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKBps | Single spike: 14:15:00, max deviation ≈ 7.7σ
                IG01 | OSLinux-OSLinux_MEMORY_MEMORY_MEMTotalMem | Single spike: 14:38:00, max deviation ≈ 7.7σ
            """

            description = f"{host} | {metric} | {anomaly_type}: {time_summary}, max deviation ≈ {max_dev:.1f}σ"
            descriptions.append(description)

        return {"metric_features": descriptions}

    def extract_log_features(logs,keywords=["failure", "fail","failed", "error", "fatal", "crash"],priority_quantile=0.8):
        if logs.empty:
            print("In\n\n")
            return {"log_features": "No useful log features."}
        template_miner = TemplateMiner()

        logs = logs.copy()

        def extract_template(log_message):
            try:
                result = template_miner.add_log_message(log_message)
                return result.get("log_template", log_message)
            except Exception:
                return log_message
        logs["template"] = logs["value"].fillna("").apply(extract_template)

        pattern = '|'.join(map(re.escape, keywords))
        keyword_mask = logs['value'].str.contains(pattern, case=False, na=False)
        filtered_by_keywords = logs[keyword_mask]

        if filtered_by_keywords.empty:
            print("In\n\n")
            return {"log_features": "No useful log features."}

        corpus = logs['template'].tolist()
        vectorizer = TfidfVectorizer(
            use_idf=True,
            binary=True,
            smooth_idf=True,
            norm=None,
            stop_words='english'
        )
        vectorizer.fit(corpus)

        filtered_corpus = filtered_by_keywords['template'].tolist()
        tfidf_matrix = vectorizer.transform(filtered_corpus)

        priority_scores = tfidf_matrix.mean(axis=1).A1
        filtered_by_keywords = filtered_by_keywords.copy()
        filtered_by_keywords['priority'] = priority_scores

        threshold = filtered_by_keywords['priority'].quantile(priority_quantile)
        trimmed_logs = filtered_by_keywords[filtered_by_keywords['priority'] >= threshold]

        trimmed_logs['minute'] = (trimmed_logs['timestamp'] // 60).astype(int)
        trimmed_logs = trimmed_logs.sort_values('timestamp')
        trimmed_logs = trimmed_logs.drop_duplicates(subset=['minute', 'template'])

        descriptions = [
            f"{timestamp_to_shanghai_str(row['timestamp'])} <{row['cmdb_id']}> | {row['log_name']}, {row['template']}"
            for _, row in trimmed_logs.iterrows()
        ]

        return {"log_features": descriptions}

    def extract_trace_features(traces):
        descriptions = generate_trace_descriptions(traces)
        return {"trace_features": descriptions}







############################################################
#               PatternMatcher for Metrics                 #
############################################################
import numpy as np
from sklearn.metrics import precision_recall_fscore_support
from sklearn.model_selection import train_test_split
import torch
import torch.nn as nn
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
import time
import sys

import logging
import sys
import os
import pandas as pd

import warnings
import math
import numpy as np
import pytz
import csv
from datetime import datetime

def get_logger(name):
    is_debug = True if sys.gettrace() else False
    logger=logging.getLogger(name)


    if is_debug:
        log_level=logging.DEBUG
        # logging.basicConfig(level=logging.DEBUG)
    else:
        log_level=logging.INFO
        # logging.basicConfig(level=logging.INFO)

    logger.propagate = False

    if not logger.hasHandlers():
        console = logging.StreamHandler()
        # formatter=logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        formatter=logging.Formatter("%(asctime)s - %(levelname)s - %(funcName)s - %(message)s")
        console.setFormatter(formatter)
        console.setLevel(log_level)
        logger.addHandler(console)
        logger.setLevel(log_level)
    return logger

BATCH_SIZE = 32
INIT_LR = 1e-4
logger = get_logger("classification")

def min_max_normalization(x: np.ndarray):
    if not isinstance(x, np.ndarray):
        logger.error("min_max_normalization: input must be np.ndarray")
    if x.ndim == 1:
        return (x-x.min())/(x.max()-x.min())
    else:
        x = x.T
        x = (x-x.min(axis=0))/(x.max(axis=0)-x.min(axis=0))
        return x.T

class DataSetCNN(Dataset):
    def __init__(self, data, label, length=30, eval=False):
        if data.ndim == 1:
            data = data.reshape(1, -1)
        if not eval:
            assert(data.shape[0] == label.shape[0])
            self.datas = data
            self.label = label
        else:
            self.datas = data
        self.length = length
        self.eval = eval

    def __len__(self):
        return self.datas.shape[0]

    def __getitem__(self, i):
        if self.eval:
            return torch.tensor(self.datas[i].reshape(-1, self.length), dtype=torch.float)
        else:
            return torch.tensor(self.datas[i].reshape(-1, self.length), dtype=torch.float), torch.tensor(self.label[i], dtype=torch.long)

class CNN(nn.Module):
    def __init__(self, classes=50):
        super(CNN, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv1d(1, 64, 5, 1, 1),
            # nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.MaxPool1d(kernel_size=2),
            # nn.AdaptiveAvgPool1d(1)
        )
        self.conv2 = nn.Sequential(
            nn.Conv1d(64, 128, 5, 1, 1, bias=False),
            # nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.MaxPool1d(2),
        )
        self.conv3 = nn.Sequential(
            nn.Conv1d(128, 256, 5, 1, 1, bias=False),
            # nn.BatchNorm1d(256),
            nn.ReLU(),
            nn.MaxPool1d(2),
        )
        self.flatten = nn.Sequential(
            nn.Linear(512, 64), nn.BatchNorm1d(64), nn.ReLU())
        # self.out = nn.Linear(256, 64)
        self.out = nn.Linear(64, classes)
        self.dropout = nn.Dropout(0.5)
        self.avgpool = nn.AdaptiveAvgPool1d(1)

    def forward(self, x):
        # print(x)
        x = self.conv1(x)

        x = self.conv2(x)

        x = self.conv3(x)

        x = x.view(x.size(0), -1)
        x = self.dropout(x)
        x = self.flatten(x)
        # x=torch.cat((x,y),1)
        # x=self.avgpool(x)
        x = self.out(x)
        # x=self.out1(x)
        x = nn.LogSoftmax(dim=-1)(x)
        # x=nn.Softmax(dim=-1)(x)
        return x


class CNNClassifier(object):
    def __init__(self, model=None, class_num=11, length=30):
        if model is None:
            self.model = CNN(class_num)
        else:
            self.model = model
        self.class_num = class_num
        self.length = length

    def fit(self, X: np.ndarray, y: np.array, max_epoch=100, stop_epoch=10):
        logger.info("start training")
        # ------init--------
        initial_lr = INIT_LR
        batch_size = BATCH_SIZE
        criterion = nn.CrossEntropyLoss()
        count = 0
        train_X, valid_X, train_y, valid_y = train_test_split(
            X, y, test_size=0.3)
        train_data, valid_data = DataSetCNN(train_X, train_y, length=self.length), DataSetCNN(
            valid_X, valid_y, length=self.length)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=initial_lr)
        train_loader = DataLoader(
            train_data, batch_size=batch_size, shuffle=True, drop_last=True)
        valid_loader = DataLoader(
            valid_data, batch_size=batch_size, shuffle=True, drop_last=True)
        is_gpu = torch.cuda.is_available()
        if is_gpu:
            self.model = self.model.cuda()
            criterion = criterion.cuda()
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=50, gamma=0.8)
        max_f, best_p, best_r = 0, 0, 0
        best_model = None

        # ------train------
        for epoch in range(max_epoch):
            y_true, y_pred = np.array(
                [], dtype=np.int32), np.array([], dtype=np.int32)
            self.model.train()
            # for b in tqdm(train_loader):
            for _, b in enumerate(train_loader):
                optimizer.zero_grad()
                if is_gpu:
                    b[0] = b[0].cuda()
                    b[1] = b[1].cuda()
                out = self.model(b[0])
                batch_pred = out.data.max(1)[1]
                loss = criterion(out, b[1])
                loss.backward()
                optimizer.step()
                if is_gpu:
                    b[1] = b[1].cpu()
                    batch_pred = batch_pred.cpu()
                y_true = np.append(y_true, b[1].numpy())
                y_pred = np.append(y_pred, batch_pred.numpy())
            p1, r1, f1, _ = precision_recall_fscore_support(
                y_true, y_pred, labels=np.unique(y_pred), average="macro")
            scheduler.step()

        # ----------validation-------------
            with torch.no_grad():
                self.model.eval()
                y_true, y_pred = np.array(
                    [], dtype=np.int32), np.array([], dtype=np.int32)
                for _, b in enumerate(valid_loader):
                    if is_gpu:
                        b[0] = b[0].cuda()
                        b[1] = b[1].cuda()
                    out = self.model(b[0])
                    batch_pred = out.data.max(1)[1]
                    if is_gpu:
                        b[1] = b[1].cpu()
                        batch_pred = batch_pred.cpu()
                    y_true = np.append(y_true, b[1].numpy())
                    y_pred = np.append(y_pred, batch_pred.numpy())
                p2, r2, f2, _ = precision_recall_fscore_support(
                    y_true, y_pred, labels=np.unique(y_pred), average="macro")
                if f2 > max_f:
                    best_p = p2
                    best_r = r2
                    max_f = f2
                    best_model = self.model
                    count = 0
                else:
                    count += 1
                    if count >= stop_epoch:
                        break
            logger.info("Epoch:{}, Loss:{:.5f}\ntrain_p:{:.5f}, train_r:{:.5f}, train_f:{:.5f},valid_p:{:.5f}, valid_r:{:.5f}, valid_f:{:.5f},best_p:{:.5f}, best_r:{:.5f}, best_f:{:.5f}".format(
                epoch+1, loss.item(), p1, r1, f1, p2, r2, f2, best_p, best_r, max_f))
        self.model = best_model

    def predict(self, X: np.ndarray) -> np.array:

        if self.model is None:
            raise ValueError("please load or fit model first")

        test_data = DataSetCNN(min_max_normalization(
            X), None, eval=True, length=self.length)
        test_loader = DataLoader(test_data, batch_size=BATCH_SIZE)
        self.model.eval()
        pred = np.array([], dtype=np.int32)
        for _, b in enumerate(test_loader):
            out = self.model(b)
            batch_pred = out.data.max(1)[1]
            pred = np.append(pred, batch_pred.numpy())
        # if pred.shape[0] == 1:
        #     pred = pred[0]
        return pred

    def save_model(self, path):

        if ".pt" not in path:
            path += ".pt"
        torch.save(self.model.state_dict(), path)
        logger.info("save model successfully")

    def load_model(self, path):

        if ".pt" not in path:
            path += ".pt"
        self.model.load_state_dict(torch.load(path))
        logger.info("load model successfully")


############################################################
#                    Filter for Logs                       #
############################################################
import re

def log_filter(logs, keywords):
    logs = logs.copy()
    pattern = '|'.join(map(re.escape, keywords))
    keyword_mask = logs['value'].str.contains(pattern, case=False, na=False)
    filtered_by_keywords = logs[keyword_mask]

    if filtered_by_keywords.empty:
        return pd.DataFrame(columns=logs.columns)

    return filtered_by_keywords.reset_index(drop=True)




############################################################
#                   Filter for Traces                      #
############################################################
from collections import defaultdict

def generate_trace_descriptions(trace_data: pd.DataFrame, q=0.98) -> list:

    p95_series = trace_data.groupby("type")["duration"].quantile(q)
    p95_dict = p95_series.to_dict()
    trace_data["is_high_latency"] = trace_data["duration"] > trace_data["type"].map(p95_dict)

    high_latency_data = trace_data[trace_data["is_high_latency"]]
    if high_latency_data.empty:
        return []
    span_to_cmdb_map = dict(zip(trace_data["span_id"], trace_data["cmdb_id"]))
    parent_map = dict(zip(trace_data["parent_id"], trace_data["cmdb_id"]))
    span_parent_map = dict(zip(trace_data["span_id"], trace_data["parent_id"]))

    window_groups = defaultdict(list)
    path_counter = defaultdict(int)

    for _, row in high_latency_data.iterrows():
        ts = int(row["timestamp"] / 1000)
        time_bucket = ts - (ts % 60)

        if pd.isnull(row["cmdb_id"]):
            continue

        callee = row["cmdb_id"]
        duration = row["duration"]
        span_id = row["span_id"]
        parent_id = row["parent_id"]
        caller = span_to_cmdb_map.get(parent_id, callee)

        key = (time_bucket, callee)
        window_groups[key].append((caller, duration))

        grandparent_id = span_parent_map.get(parent_id, None)
        grandparent = span_to_cmdb_map.get(grandparent_id, "Unknown")
        if grandparent != "Unknown":
            path_key = f"{grandparent} → {caller} → {callee}"
            path_counter[path_key] += 1

    desc_records = []

    for (time_bucket, callee), entries in window_groups.items():
        caller_counts = defaultdict(int)
        max_latency = 0
        for caller, duration in entries:
            caller_counts[caller] += 1
            max_latency = max(max_latency, duration)

        callers_str = ", ".join(f"{caller}×{count}" for caller, count in sorted(caller_counts.items()))
        start_str = timestamp_to_shanghai_str(time_bucket)
        end_str = timestamp_to_shanghai_str(time_bucket + 30)
        count = len(entries)
        desc = f"<{start_str}~{end_str}, Callee={callee}, Count={count}, MaxLatency={max_latency:.0f}ms, Callers=[{callers_str}]>"
        desc_records.append((desc, count, max_latency))

    desc_records.sort(key=lambda x: (-x[1], -x[2]))

    descriptions = [desc for desc, _, _ in desc_records[0:24]]

    path_stats = [
        f"“{path}” has occured {count} times in the time window"
        for path, count in sorted(path_counter.items(), key=lambda x: -x[1])
        if count >= 2
    ]

    return descriptions + path_stats[0:24]

def timestamp_to_shanghai_str(timestamp):
    utc_dt = datetime.utcfromtimestamp(timestamp).replace(tzinfo=pytz.utc)

    sh_dt = utc_dt.astimezone(shanghai_tz)

    return sh_dt.strftime('%Y-%m-%d %H:%M:%S')
