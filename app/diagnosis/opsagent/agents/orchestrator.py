from datetime import datetime
import pytz
import os
import yaml
import time
import pandas as pd
from agents.judge import Judge
from agents.ad_agent import ADAgent
from agents.ft_agent import FTAgent
from agents.rcl_agent import RCLAgent
from utils.MessagePool import MessagePool, Message
from utils.ReviewPool import ReviewPool, Review
from reflection.ReflectionVector import ReflectionVector
from utils.blackboard import Blackboard
from utils.public_function import generate_random_string
from utils.data_processer import DataProcessor
from transformers import AutoModelForCausalLM, AutoTokenizer
from utils.public_function import llm_invoke
from box import Box
from utils.public_function import extract_between_tags

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

class CoreOrchestrator:
    """
    core manager class
    Attributes:
        blackboard:
        judge:
        anomaly_sentinel:
        failure_diagnoser:
        root_detective:
    """
    def __init__(self, model_path, dataset):
        self.blackboard = Blackboard(task_id=generate_random_string(10))
        self.model_path = model_path
        self.blackboard.dataset = dataset

    def process_task(self, time_window, tasks, number, case_number, vdbs, baseline):
        """
        main function to process one task published main.py.execute

        Args:
            time_window: duration time in one query.  e.g. [start_time, end_time], [2021-03-10 22:00:00,2021-03-10 22:30:00]
            tasks: types of task in one query. e.g. ['AD', 'FT', 'RCL']
            number: number of tasks in one query. e.g. 1 or more
            case_number: -th query case

        Returns:
            final_answer:
            final_thinkings:

        """
        self.blackboard.time_window = time_window
        self.blackboard.tasks = tasks
        self.blackboard.number = number

        self.process_data(case_number)
        print("Finish data process\n\n")

        msgPool = MessagePool()
        reviewPool = ReviewPool()
        ad_agent = ADAgent("Anomaly Sentinel",
                           time_window, number,
                           self.blackboard.data_features,
                           msgPool, reviewPool, vdbs[0])
        ft_agent = FTAgent("Failure Diagnoser",
                           time_window, number,
                           self.blackboard.data_features,
                           msgPool, reviewPool, vdbs[1])
        rcl_agent = RCLAgent("Root Detective",
                            time_window, number,
                            self.blackboard.data_features,
                            msgPool, reviewPool, vdbs[2])
        ad_agent.think()
        ft_agent.think()
        rcl_agent.think()
        if "AD" in self.blackboard.tasks:
            ft_agent.cross_review("Anomaly Sentinel")
            rcl_agent.cross_review("Anomaly Sentinel")
            ad_agent.refine()
        if "FT" in self.blackboard.tasks:
            ad_agent.cross_review("Failure Diagnoser")
            rcl_agent.cross_review("Failure Diagnoser")
            ft_agent.refine()
        if "RCL" in self.blackboard.tasks:
            ad_agent.cross_review("Root Detective")
            ft_agent.cross_review("Root Detective")
            rcl_agent.refine()

        # ===== reflect  =====
        if cfg.run_mode == "ppo_logging":
            ad_agent.reflect(baseline)
            ft_agent.reflect(baseline)
            rcl_agent.reflect(baseline)

        final_answer = []
        final_thinkings = []
        if "AD" in self.blackboard.tasks:
            final_answer.extend(ad_agent.agent_context.last_message().answer)
            final_thinkings.append(ad_agent.agent_context.last_message().thinking)
        if "FT" in self.blackboard.tasks:
            final_answer.extend(ft_agent.agent_context.last_message().answer)
            final_thinkings.append(ft_agent.agent_context.last_message().thinking)
        if "RCL" in self.blackboard.tasks:
            final_answer.extend(rcl_agent.agent_context.last_message().answer)
            final_thinkings.append(rcl_agent.agent_context.last_message().thinking)

        print("=== Finnal Answers ===")
        for i, (ans, think) in enumerate(zip(final_answer, final_thinkings), start=1):
            print(f"[{i}] Answer: {ans}")
            print(f"[{i}] Thinking:\n{think}\n")


        return final_answer, final_thinkings

    def process_data(self, case_number):
        metrics = {}
        logs = None
        traces = None

        shanghai_tz = pytz.timezone('Asia/Shanghai')
        start_dt = shanghai_tz.localize(datetime.strptime(self.blackboard.time_window[0], "%Y-%m-%d %H:%M:%S"))
        end_dt = shanghai_tz.localize(datetime.strptime(self.blackboard.time_window[1], "%Y-%m-%d %H:%M:%S"))

        date = start_dt.strftime("%Y_%m_%d")

        start_timestamp = int(start_dt.timestamp())
        end_timestamp = start_timestamp + 1800

        if self.blackboard.dataset == "Bank":
            file_path = 'configs/Bank.yaml'
            with open(file_path, 'r', encoding='utf-8') as file:
                bank_config = yaml.safe_load(file)
            bank_instances = bank_config.get('instances', [])
            pod_metric_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "metric", "metric_container.csv")
            pod_data = pd.read_csv(pod_metric_path)

            for pod in bank_instances:
                pod_df = pod_data[pod_data['cmdb_id'] == pod]
                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot(index='timestamp', columns='kpi_name', values='value')
                pivot_df = pivot_df.fillna(0)

                metrics[pod] = pivot_df

            log_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "log", "log_service.csv")
            log_data = pd.read_csv(log_path)
            logs = log_data[(log_data['timestamp'] >= start_timestamp) & (log_data['timestamp'] <= end_timestamp)]

            trace_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "trace", "trace_span.csv")
            trace_data = pd.read_csv(trace_path)
            traces = trace_data[(trace_data['timestamp'] / 1000 >= start_timestamp) & (trace_data['timestamp'] / 1000 <= end_timestamp)]
            traces['type'] = 'http'


        elif self.blackboard.dataset == "Market":
            file_path = 'configs/Market.yaml'
            with open(file_path, 'r', encoding='utf-8') as file:
                market_config = yaml.safe_load(file)
            market_instances = market_config.get('instances', [])
            pod_metric_path = os.path.join("dataset", self.blackboard.dataset, "cloudbed-2", "telemetry", date, "metric", "metric_container.csv")
            node_metric_path = os.path.join("dataset", self.blackboard.dataset, "cloudbed-2", "telemetry", date, "metric", "metric_node.csv")
            pod_data = pd.read_csv(pod_metric_path)
            node_data = pd.read_csv(node_metric_path)

            pod_data['cmdb_id'] = pod_data['cmdb_id'].str.split('.').str[-1]


            for pod in market_instances:
                pod_df = pod_data[pod_data['cmdb_id'] == pod]
                if pod_df.empty:
                    continue

                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot_table(index='timestamp', columns='kpi_name', values='value', aggfunc='mean')
                pivot_df = pivot_df.fillna(0)
                metrics[pod] = pivot_df

            for pod in market_instances:
                pod_df = node_data[node_data['cmdb_id'] == pod]
                if pod_df.empty:
                    continue

                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot(index='timestamp', columns='kpi_name', values='value')
                pivot_df = pivot_df.fillna(0)
                metrics[pod] = pivot_df

            log_path = os.path.join("dataset", self.blackboard.dataset, "cloudbed-2", "telemetry", date, "log", "log_service.csv")
            log_data = pd.read_csv(log_path)
            logs = log_data[(log_data['timestamp'] >= start_timestamp) & (log_data['timestamp'] <= end_timestamp)]

            trace_path = os.path.join("dataset", self.blackboard.dataset, "cloudbed-2", "telemetry", date, "trace", "trace_span.csv")
            trace_data = pd.read_csv(trace_path)
            traces = trace_data[(trace_data['timestamp'] / 1000 >= start_timestamp) & (trace_data['timestamp'] / 1000 <= end_timestamp)]
            traces = traces.rename(columns={'parent_span': 'parent_id'})

        elif self.blackboard.dataset == "Telecom":
            file_path = 'configs/Telecom.yaml'
            with open(file_path, 'r', encoding='utf-8') as file:
                telecom_config = yaml.safe_load(file)
            telecom_instances = telecom_config.get('instances', [])
            pod_metric_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "metric", "metric_container.csv")
            node_metric_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "metric", "metric_node.csv")
            service_metric_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "metric", "metric_service.csv")
            pod_data = pd.read_csv(pod_metric_path)
            node_data = pd.read_csv(node_metric_path)
            service_data = pd.read_csv(service_metric_path)
            pod_data["timestamp"] = pod_data["timestamp"] / 1000
            node_data["timestamp"] = node_data["timestamp"] / 1000
            service_data["timestamp"] = service_data["timestamp"] / 1000

            for pod in telecom_instances:
                pod_df = pod_data[pod_data['cmdb_id'] == pod]
                if pod_df.empty:
                    continue

                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot_table(index='timestamp', columns='name', values='value', aggfunc='mean')
                pivot_df = pivot_df.fillna(0)
                metrics[pod] = pivot_df

            for pod in telecom_instances:
                pod_df = node_data[node_data['cmdb_id'] == pod]
                if pod_df.empty:
                    continue

                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot_table(index='timestamp', columns='name', values='value', aggfunc='mean')
                pivot_df = pivot_df.fillna(0)
                metrics[pod] = pivot_df

            for pod in telecom_instances:

                pod_df = service_data[service_data['cmdb_id'] == pod]
                if pod_df.empty:
                    continue

                pod_df = pod_df[
                    (pod_df['timestamp'] >= start_timestamp - 1200) &
                    (pod_df['timestamp'] <= end_timestamp + 600)
                ]

                pivot_df = pod_df.pivot_table(index='timestamp', columns='name', values='value', aggfunc='mean')
                pivot_df = pivot_df.fillna(0)
                metrics[pod] = pivot_df

            logs = pd.DataFrame()
            trace_path = os.path.join("dataset", self.blackboard.dataset, "telemetry", date, "trace", "trace_span.csv")
            trace_data = pd.read_csv(trace_path)
            trace_data.columns = ['type', 'timestamp', 'duration', 'success', 'trace_id', 'span_id', 'parent_id', 'cmdb_id', 'dsName', 'ServiceName']
            trace_data = trace_data[['type', 'timestamp', 'duration', 'span_id', 'parent_id', 'cmdb_id']]
            traces = trace_data[(trace_data['timestamp'] / 1000 >= start_timestamp) & (trace_data['timestamp'] / 1000 <= end_timestamp)]

        self.blackboard.data_features["metric"] = DataProcessor.extract_metric_features(metrics)["metric_features"]
        self.blackboard.data_features["log"] = DataProcessor.extract_log_features(logs, priority_quantile=0.98)["log_features"]
        self.blackboard.data_features["trace"] = DataProcessor.extract_trace_features(traces)["trace_features"]

        def write_feature_to_file(data, filename):
            with open(filename, "w", encoding="utf-8") as f:
                if isinstance(data, list):
                    for item in data:
                        f.write(f"{item}\n")
                else:
                    f.write(str(data) + "\n")
