import yaml
from agents.base_agent import BaseAgent, AgentContext
from utils.MessagePool import MessagePool, Message
from utils.ReviewPool import ReviewPool, Review
from typing import List, Optional
from reflection.ReflectionVector import ReflectionVector
from prompts.root_detective_prompt import THINK_SYSTEM_PROMPT, USER_PROMPT
from prompts.root_detective_review_prompt import REVIEW_SYSTEM_PROMPT
from prompts.root_detective_refine_prompt import REFINE_SYSTEM_PROMPT
from box import Box

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))
DATASET = cfg.dataset

class RCLAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        time_window: str,
        failure_count: int,
        data_features: Optional[dict[str, str]] = None,
        message_pool: 'MessagePool' = None,
        review_pool: 'ReviewPool' = None,
        vector_db: 'ReflectionVector' = None
    ):
        dataset_to_path = {
            "Bank": "configs/Bank.yaml",
            "Market": "configs/Market.yaml",
            "Telecom": "configs/Telecom.yaml",
            "Otel": "configs/Otel.yaml",
        }
        file_path = dataset_to_path.get(DATASET)
        if not file_path:
            raise ValueError(f"Unsupported dataset '{DATASET}'. "
                             f"Expected one of {list(dataset_to_path)}")
        with open(file_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        instances = config.get("instances", [])

        think_system_prompt = THINK_SYSTEM_PROMPT.format(instances=instances)
        refine_system_prompt = REFINE_SYSTEM_PROMPT.format(instances=instances)
        user_prompt = self._build_user_prompt(time_window, failure_count, data_features)
        agent_context = AgentContext(
            cfg.llm_setting_agent.model_path,
            cfg.llm_setting_agent.rcl_peft_path,
            cfg.llm_setting_agent.max_token,
            think_system_prompt=think_system_prompt,
            review_system_prompt=REVIEW_SYSTEM_PROMPT,
            refine_system_prompt=refine_system_prompt,
            user_prompt=user_prompt,
            memory=None
        )
        super().__init__(
            name=name,
            failure_count=failure_count,
            agent_context=agent_context,
            message_pool=message_pool,
            review_pool=review_pool,
            vector_db=vector_db
        )
        self.agent_context.gen_symptom_system_prompt += """
        ##Examples:
        1.
        <symptoms>
        - At **18:30:00**, **Mysql02** showed significant spikes in **disk I/O** metrics, including **Disk Percent Busy** and **Disk Read/Write**. This indicates a potential **disk I/O bottleneck** that likely caused delays in database operations, affecting downstream services.
        - Around **18:38:00**, **Mysql02** exhibited high **memory utilization**, suggesting **memory pressure** in the MySQL instance, which likely impacted its ability to handle database queries efficiently.
        - **Tomcat02** experienced frequent **GC pauses** and **Full GC events** between **18:30:38 and 18:32:00**, with pauses lasting up to several seconds. This indicates **memory exhaustion** in Tomcat, leading to delays in request processing.
        - At **18:30:00**, **IG01** and **IG02** showed **latency spikes** with **max latencies** reaching over 60 seconds, pointing to a **bottleneck in the ingress layer**. These delays were likely caused by upstream service failures, primarily stemming from MySQL and Tomcat.
        - **Redis02** also showed **memory pressure** around **19:00:00**, with spikes in **used memory** and **max memory**, suggesting potential **cache eviction** or memory exhaustion, possibly triggered by slow backend responses.
        - The pattern of **disk I/O saturation**, **memory exhaustion**, and **latency spikes** across **Mysql02**, **Tomcat02**, and **Redis02** suggests a cascading failure, where MySQL’s resource exhaustion led to delays in request processing across other services.
        </symptoms>
        2.
        <symptoms>
        - At **23:31:00**, **Mysql01** showed a significant spike in **TCP-FIN-WAIT**, indicating a potential network connection issue or delayed connection termination, which could have impacted downstream services such as **MG01** and **MG02**.
        - **Mysql02** exhibited multiple spikes in **Handler Read** and **Sort Range** at **23:26:00**, signaling a potential **database query bottleneck** due to inefficient queries or resource contention, which likely slowed down data retrieval and affected system performance.
        - At **23:33:43**, **Tomcat02** experienced a **GC pause** (Allocation Failure) with a duration of 0.06 seconds, suggesting **memory pressure** that could have led to delays in processing requests during high traffic.
        - **IG01** and **IG02** showed high **latency spikes** between **23:30:00–23:31:30**, with **max latencies** peaking over 2 seconds. This points to significant delays at the **ingress layer**, possibly caused by upstream bottlenecks like **Mysql02** and **Tomcat02**.
        - At **23:55:00**, **Tomcat02** and **Mysql01** both showed spikes in **disk read/write metrics** and **disk percent busy**, suggesting **I/O congestion**, likely impacting the overall system response time.
        - **Redis01** showed increased **disk write** activity at **23:55:00**, indicating potential **memory or I/O pressure**, which could have affected its ability to serve requests efficiently and contributed to the overall system slowdown.
        - Based on these metric and trace anomalies, the root cause appears to be a combination of **disk I/O issues in Mysql02** and **memory pressure in Tomcat02**, which propagated delays through the system, impacting ingress services and causing significant performance degradation across multiple layers.
        </symptoms>
        3.
        <symptoms>
        - At **20:30:00**, **Mysql02** exhibited significant spikes in **disk read/write operations** and **memory usage**, indicating potential **disk I/O saturation** and **memory pressure** in the database, which likely caused delays in query processing.
        - **Tomcat03** and **Tomcat04** showed frequent **GC pauses** due to **allocation failures** at **20:20:58** and **20:25:23**, suggesting **memory exhaustion** in these Tomcat instances, which would have hindered their ability to serve requests efficiently.
        - **MG01** and **MG02** experienced **latency spikes** between **20:00:00 and 20:30:00**, with **max latencies** exceeding **4000ms**, likely due to **upstream delays** caused by issues in **Mysql02** and **Tomcat** services.
        - At **20:32:00**, **Redis02** showed signs of **memory pressure**, with spikes in **used memory** and **keyspace misses**, indicating potential **cache eviction** or a delayed response from backend services.
        - The **disk I/O** saturation in **Mysql02** and the **memory pressure** in **Tomcat03** and **Tomcat04** suggest a **resource exhaustion failure**, with **Mysql02** being the likely primary cause, affecting the overall performance of multiple downstream services.
        </symptoms>
        """
        self.agent_context.reflect_system_prompt += """
        ##Examples:
        1.
        <symptoms>
        - At **18:30:00**, **Mysql02** showed significant spikes in **disk I/O** metrics, including **Disk Percent Busy** and **Disk Read/Write**. This indicates a potential **disk I/O bottleneck** that likely caused delays in database operations, affecting downstream services.
        - Around **18:38:00**, **Mysql02** exhibited high **memory utilization**, suggesting **memory pressure** in the MySQL instance, which likely impacted its ability to handle database queries efficiently.
        - **Tomcat02** experienced frequent **GC pauses** and **Full GC events** between **18:30:38 and 18:32:00**, with pauses lasting up to several seconds. This indicates **memory exhaustion** in Tomcat, leading to delays in request processing.
        - At **18:30:00**, **IG01** and **IG02** showed **latency spikes** with **max latencies** reaching over 60 seconds, pointing to a **bottleneck in the ingress layer**. These delays were likely caused by upstream service failures, primarily stemming from MySQL and Tomcat.
        - **Redis02** also showed **memory pressure** around **19:00:00**, with spikes in **used memory** and **max memory**, suggesting potential **cache eviction** or memory exhaustion, possibly triggered by slow backend responses.
        - The pattern of **disk I/O saturation**, **memory exhaustion**, and **latency spikes** across **Mysql02**, **Tomcat02**, and **Redis02** suggests a cascading failure, where MySQL’s resource exhaustion led to delays in request processing across other services.
        </symptoms>
        2.
        <symptoms>
        - At **23:31:00**, **Mysql01** showed a significant spike in **TCP-FIN-WAIT**, indicating a potential network connection issue or delayed connection termination, which could have impacted downstream services such as **MG01** and **MG02**.
        - **Mysql02** exhibited multiple spikes in **Handler Read** and **Sort Range** at **23:26:00**, signaling a potential **database query bottleneck** due to inefficient queries or resource contention, which likely slowed down data retrieval and affected system performance.
        - At **23:33:43**, **Tomcat02** experienced a **GC pause** (Allocation Failure) with a duration of 0.06 seconds, suggesting **memory pressure** that could have led to delays in processing requests during high traffic.
        - **IG01** and **IG02** showed high **latency spikes** between **23:30:00–23:31:30**, with **max latencies** peaking over 2 seconds. This points to significant delays at the **ingress layer**, possibly caused by upstream bottlenecks like **Mysql02** and **Tomcat02**.
        - At **23:55:00**, **Tomcat02** and **Mysql01** both showed spikes in **disk read/write metrics** and **disk percent busy**, suggesting **I/O congestion**, likely impacting the overall system response time.
        - **Redis01** showed increased **disk write** activity at **23:55:00**, indicating potential **memory or I/O pressure**, which could have affected its ability to serve requests efficiently and contributed to the overall system slowdown.
        - Based on these metric and trace anomalies, the root cause appears to be a combination of **disk I/O issues in Mysql02** and **memory pressure in Tomcat02**, which propagated delays through the system, impacting ingress services and causing significant performance degradation across multiple layers.
        </symptoms>
        3.
        <symptoms>
        - At **20:30:00**, **Mysql02** exhibited significant spikes in **disk read/write operations** and **memory usage**, indicating potential **disk I/O saturation** and **memory pressure** in the database, which likely caused delays in query processing.
        - **Tomcat03** and **Tomcat04** showed frequent **GC pauses** due to **allocation failures** at **20:20:58** and **20:25:23**, suggesting **memory exhaustion** in these Tomcat instances, which would have hindered their ability to serve requests efficiently.
        - **MG01** and **MG02** experienced **latency spikes** between **20:00:00 and 20:30:00**, with **max latencies** exceeding **4000ms**, likely due to **upstream delays** caused by issues in **Mysql02** and **Tomcat** services.
        - At **20:32:00**, **Redis02** showed signs of **memory pressure**, with spikes in **used memory** and **keyspace misses**, indicating potential **cache eviction** or a delayed response from backend services.
        - The **disk I/O** saturation in **Mysql02** and the **memory pressure** in **Tomcat03** and **Tomcat04** suggest a **resource exhaustion failure**, with **Mysql02** being the likely primary cause, affecting the overall performance of multiple downstream services.
        </symptoms>
        """

    @staticmethod
    def _build_user_prompt(
        time_window: str,
        number: int,
        data_features: Optional[dict]
    ) -> str:
        prompt = USER_PROMPT.format(time_window=time_window)
        prompt += f"The number of failure in this time window is {number}.\n"
        data_features = data_features or {}
        if data_features.get("metric"):
            prompt += f"This is the information derived from metric data: {data_features['metric']}\n"
        if data_features.get("log"):
            prompt += f"This is the information derived from log data: {data_features['log']}\n"
        if data_features.get("trace"):
            prompt += f"This is the information derived from trace data: {data_features['trace']}\n"
        prompt += "Please analyze the above information and identify the root cause component."
        return prompt
