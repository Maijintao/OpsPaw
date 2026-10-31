import yaml
from agents.base_agent import BaseAgent, AgentContext
from utils.MessagePool import MessagePool, Message
from utils.ReviewPool import ReviewPool, Review
from typing import List, Optional
from reflection.ReflectionVector import ReflectionVector
from prompts.failure_diagnoser_prompt import THINK_SYSTEM_PROMPT, USER_PROMPT
from prompts.failure_diagnoser_review_prompt import REVIEW_SYSTEM_PROMPT
from prompts.failure_diagnoser_refine_prompt import REFINE_SYSTEM_PROMPT
from box import Box

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))
DATASET = cfg.dataset

class FTAgent(BaseAgent):
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
                             f"Expected one of {list(dataset_to_path)}"
            )
        with open(file_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        failure_types = config.get("failure_types", [])

        think_system_prompt = THINK_SYSTEM_PROMPT.format(failure_types=failure_types)
        refine_system_prompt = REFINE_SYSTEM_PROMPT.format(failure_types=failure_types)
        user_prompt = self._build_user_prompt(time_window, failure_count, data_features)
        agent_context = AgentContext(
            cfg.llm_setting_agent.model_path,
            cfg.llm_setting_agent.ft_peft_path,
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
        - Around **15:38:00**, the system began to experience significant performance degradation originating from the **Tomcat application layer**. Metrics show a spike in **CPU utilization** and **thread blocking**, coinciding with frequent **Full GC events** lasting up to several seconds. These GC pauses indicate heavy memory pressure and inefficient object recycling, reducing Tomcat’s ability to process requests in real time.  
        - Concurrently, **MySQL (Mysql01)** exhibited abnormal behavior with elevated **disk I/O latency** and an increase in **lock wait time**, pointing to contention within the storage subsystem. The combination of GC-induced stalls and slow database I/O created a compounded delay in request processing.  
        - Between **15:40:00–15:42:00**, the **MG01** and **MG02** gateway services reported sharp increases in **average and maximum latency**, suggesting that backend response times were deteriorating under cumulative resource contention. Trace data reveals expanding request queues and timeouts propagating from Tomcat to the gateway layer.  
        - **Redis metrics** during this interval show spikes in **keyspace misses** and **memory usage**, implying cache eviction or memory pressure events likely caused by delayed updates from backend services. This degraded cache performance further amplified overall response delays.  
        - By **15:45:00**, the ingress nodes (**IG01**, **IG02**) exhibited queue saturation, with multiple requests exceeding 10 seconds in latency, indicating that the slowdown had propagated to the system’s entry point. No external traffic surge was detected, confirming that the bottleneck originated internally.  
        - The co-occurrence of **high CPU utilization**, **frequent GC events**, and **disk I/O contention** suggests a **resource exhaustion failure mode** — most likely driven by excessive memory allocation in Tomcat and concurrent I/O bottlenecks in MySQL. These resource-level degradations collectively led to cascading slowdowns across all dependent services.
        </symptoms>
        2. 
        <symptoms>
        - At **23:31:00**, there was a significant spike in **Mysql01** metrics, specifically **TCP-FIN-WAIT**, indicating a potential network connection issue or slow connection release. This anomaly was followed by **Mysql02** showing multiple spikes in **MySQL Handler Read** and **Sort Range** at **23:26:00**, suggesting that MySQL query performance was degraded, possibly due to inefficient index usage or lock contention.  
        - At **23:33:43**, **Tomcat02** recorded a **GC pause** (Allocation Failure), which lasted around 0.06 seconds, pointing to **memory pressure** in the Tomcat application. Frequent garbage collection suggests that JVM memory was not being efficiently utilized, potentially slowing down request processing during high load periods.  
        - **Disk I/O metrics** from multiple services, including **Mysql02** and **Tomcat02**, showed spikes in **Disk Reads/Writes** at **23:55:00**, indicating a heavy load on storage. These spikes likely exacerbated delays in data retrieval and application performance, which can also suggest underlying **disk I/O bottlenecks**.  
        - Trace data indicates that **MG01** and **MG02** latency increased sharply between **23:30:00** and **23:35:00**, peaking at around **2299ms** and **2291ms** respectively. This spike in latency reflects a cascading issue from backend storage and application layers, leading to overall system slowdowns.  
        - At **23:35:00**, **IG01** and **IG02** showed a **level shift up** in **memory utilization**, further confirming the growing **memory pressure** and potential out-of-memory issues on the ingress layer.  
        - Overall, the pattern of simultaneous spikes in **CPU usage**, **disk I/O**, **memory usage**, and **latency** suggests a **resource exhaustion failure mode**, likely stemming from **high disk I/O** affecting database performance, which was compounded by **memory pressure** in Tomcat, ultimately leading to the widespread slowdown in the system.
        </symptoms>
        3. 
        <symptoms>
        - At **20:00:00**, there was a significant spike in **Mysql01** and **Mysql02** disk metrics, including **disk read** and **disk write** operations, with deviations of approximately 7.7σ. This indicates a heavy load on the storage system, likely leading to delayed data access and causing performance degradation in downstream systems.
        - Around **20:20:58**, **Tomcat03** recorded a **GC pause** (Allocation Failure), which lasted approximately 0.02 seconds, suggesting that **memory pressure** in the Tomcat JVM was preventing efficient memory management, slowing down request processing during high traffic.
        - **Redis01** and **Redis02** both experienced significant memory-related spikes in metrics like **used memory** and **maxmemory**, with deviations around 7.7σ, highlighting possible **memory exhaustion**. This memory pressure likely caused cache eviction and performance degradation in read/write operations.
        - Between **20:00:00 and 20:04:00**, there were multiple latency spikes observed across **IG01**, **IG02**, **MG01**, and **MG02**, with max latencies peaking at over 4000ms. This shows that the performance degradation propagated from backend database systems to frontend services, resulting in significant slowdowns.
        - At **20:11:00**, there was an increase in **disk I/O** activity across **Tomcat02**, showing spikes in **disk writes** and **read operations**, indicating **disk I/O bottlenecks** affecting both application and database layers, contributing to overall system delays.
        - The **high CPU utilization** spikes observed across **MySQL**, **Redis**, and **Tomcat** instances, combined with **GC pauses** and **disk I/O anomalies**, suggest a **resource exhaustion failure mode**, likely stemming from high disk I/O causing bottlenecks in MySQL and Redis, further compounded by **memory pressure** in Tomcat and Redis, leading to a system-wide slowdown.
        </symptoms>
        """
        
        self.agent_context.reflect_system_prompt += """
        ##Examples: 
        1. 
        <symptoms>
        - Around **15:38:00**, the system began to experience significant performance degradation originating from the **Tomcat application layer**. Metrics show a spike in **CPU utilization** and **thread blocking**, coinciding with frequent **Full GC events** lasting up to several seconds. These GC pauses indicate heavy memory pressure and inefficient object recycling, reducing Tomcat’s ability to process requests in real time.  
        - Concurrently, **MySQL (Mysql01)** exhibited abnormal behavior with elevated **disk I/O latency** and an increase in **lock wait time**, pointing to contention within the storage subsystem. The combination of GC-induced stalls and slow database I/O created a compounded delay in request processing.  
        - Between **15:40:00–15:42:00**, the **MG01** and **MG02** gateway services reported sharp increases in **average and maximum latency**, suggesting that backend response times were deteriorating under cumulative resource contention. Trace data reveals expanding request queues and timeouts propagating from Tomcat to the gateway layer.  
        - **Redis metrics** during this interval show spikes in **keyspace misses** and **memory usage**, implying cache eviction or memory pressure events likely caused by delayed updates from backend services. This degraded cache performance further amplified overall response delays.  
        - By **15:45:00**, the ingress nodes (**IG01**, **IG02**) exhibited queue saturation, with multiple requests exceeding 10 seconds in latency, indicating that the slowdown had propagated to the system’s entry point. No external traffic surge was detected, confirming that the bottleneck originated internally.  
        - The co-occurrence of **high CPU utilization**, **frequent GC events**, and **disk I/O contention** suggests a **resource exhaustion failure mode** — most likely driven by excessive memory allocation in Tomcat and concurrent I/O bottlenecks in MySQL. These resource-level degradations collectively led to cascading slowdowns across all dependent services.
        </symptoms>
        2. 
        <symptoms>
        - At **23:31:00**, there was a significant spike in **Mysql01** metrics, specifically **TCP-FIN-WAIT**, indicating a potential network connection issue or slow connection release. This anomaly was followed by **Mysql02** showing multiple spikes in **MySQL Handler Read** and **Sort Range** at **23:26:00**, suggesting that MySQL query performance was degraded, possibly due to inefficient index usage or lock contention.  
        - At **23:33:43**, **Tomcat02** recorded a **GC pause** (Allocation Failure), which lasted around 0.06 seconds, pointing to **memory pressure** in the Tomcat application. Frequent garbage collection suggests that JVM memory was not being efficiently utilized, potentially slowing down request processing during high load periods.  
        - **Disk I/O metrics** from multiple services, including **Mysql02** and **Tomcat02**, showed spikes in **Disk Reads/Writes** at **23:55:00**, indicating a heavy load on storage. These spikes likely exacerbated delays in data retrieval and application performance, which can also suggest underlying **disk I/O bottlenecks**.  
        - Trace data indicates that **MG01** and **MG02** latency increased sharply between **23:30:00** and **23:35:00**, peaking at around **2299ms** and **2291ms** respectively. This spike in latency reflects a cascading issue from backend storage and application layers, leading to overall system slowdowns.  
        - At **23:35:00**, **IG01** and **IG02** showed a **level shift up** in **memory utilization**, further confirming the growing **memory pressure** and potential out-of-memory issues on the ingress layer.  
        - Overall, the pattern of simultaneous spikes in **CPU usage**, **disk I/O**, **memory usage**, and **latency** suggests a **resource exhaustion failure mode**, likely stemming from **high disk I/O** affecting database performance, which was compounded by **memory pressure** in Tomcat, ultimately leading to the widespread slowdown in the system.
        </symptoms>
        3. 
        <symptoms>
        - At **20:00:00**, there was a significant spike in **Mysql01** and **Mysql02** disk metrics, including **disk read** and **disk write** operations, with deviations of approximately 7.7σ. This indicates a heavy load on the storage system, likely leading to delayed data access and causing performance degradation in downstream systems.
        - Around **20:20:58**, **Tomcat03** recorded a **GC pause** (Allocation Failure), which lasted approximately 0.02 seconds, suggesting that **memory pressure** in the Tomcat JVM was preventing efficient memory management, slowing down request processing during high traffic.
        - **Redis01** and **Redis02** both experienced significant memory-related spikes in metrics like **used memory** and **maxmemory**, with deviations around 7.7σ, highlighting possible **memory exhaustion**. This memory pressure likely caused cache eviction and performance degradation in read/write operations.
        - Between **20:00:00 and 20:04:00**, there were multiple latency spikes observed across **IG01**, **IG02**, **MG01**, and **MG02**, with max latencies peaking at over 4000ms. This shows that the performance degradation propagated from backend database systems to frontend services, resulting in significant slowdowns.
        - At **20:11:00**, there was an increase in **disk I/O** activity across **Tomcat02**, showing spikes in **disk writes** and **read operations**, indicating **disk I/O bottlenecks** affecting both application and database layers, contributing to overall system delays.
        - The **high CPU utilization** spikes observed across **MySQL**, **Redis**, and **Tomcat** instances, combined with **GC pauses** and **disk I/O anomalies**, suggest a **resource exhaustion failure mode**, likely stemming from high disk I/O causing bottlenecks in MySQL and Redis, further compounded by **memory pressure** in Tomcat and Redis, leading to a system-wide slowdown.
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
        prompt += "Please analyze the above information and find the root cause reason."
        return prompt