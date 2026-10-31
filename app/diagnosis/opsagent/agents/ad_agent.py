import yaml
from agents.base_agent import BaseAgent, AgentContext
from utils.MessagePool import MessagePool, Message
from utils.ReviewPool import ReviewPool, Review
from typing import List, Optional
from reflection.ReflectionVector import ReflectionVector
from prompts.anomaly_sentinel_prompt import THINK_SYSTEM_PROMPT, USER_PROMPT
from prompts.anomaly_sentinel_review_prompt import REVIEW_SYSTEM_PROMPT
from prompts.anomaly_sentinel_refine_prompt import REFINE_SYSTEM_PROMPT
from box import Box

with open("configs/config.yaml", "r", encoding="utf-8") as f:
    cfg = Box(yaml.safe_load(f))

class ADAgent(BaseAgent):
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
        think_system_prompt = THINK_SYSTEM_PROMPT
        user_prompt = ADAgent._build_user_prompt(time_window, failure_count, data_features)
        agent_context = AgentContext(
            cfg.llm_setting_agent.model_path,
            cfg.llm_setting_agent.ad_peft_path,
            cfg.llm_setting_agent.max_token,
            think_system_prompt=THINK_SYSTEM_PROMPT,
            review_system_prompt=REVIEW_SYSTEM_PROMPT,
            refine_system_prompt=REFINE_SYSTEM_PROMPT,
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
        - Around 18:30:00, the system experienced a simultaneous latency surge at the ingress layer: IG01 and IG02 reported extreme tail latency (MaxLatency ≈ 60001 ms) with more than 100 delayed requests each. Concurrently, Tomcat02 processed about 586 requests with maximum latency reaching 89 s, indicating that frontend requests were heavily queued and timing out.
        - Tomcat02 underwent a sequence of frequent and long garbage collections between 18:30:38 and 18:32:09, with individual GC pauses lasting up to 2.6 s. This caused worker threads to stall and sharply reduced throughput, aligning with the timing of the ingress bottleneck.
        - Storage and local disk I/O metrics spiked on Mysql02 and Tomcat02 at 18:30:00, with high disk busy percentages and read/write throughput peaks. Subsequent CPU “iowait” metrics remained elevated until 19:00, suggesting sustained I/O congestion rather than transient spikes.
        - Between 18:41 and 18:42, latency propagation became system-wide: IG01/IG02, MG01/MG02, and Tomcat01–04 all showed concurrent latency inflation (2–5 s) and request bursts, meaning the slowdown expanded from the ingress to middle-tier services.
        - At 18:53, the slowdown peaked across the full stack—ingress, middleware, and backend. IG01/IG02 each processed over 40 slow requests, MG01/MG02 exceeded 20–40 calls, and dockerB1 reached a 3.8 s maximum latency. The entire request path degraded, not just isolated nodes.
        - Around 18:59, dockerB1 experienced another latency burst (MaxLatency ≈ 7.6 s, 46 calls), coinciding with Redis metrics jumping (used_memory_peak, expired_keys, keyspace_misses). This suggests cache rebuilds and backlog flushing during recovery.
        - The most affected chain was IG → Tomcat02 → MG01/MG02 → dockerB1/Redis/Mysql, covering both read and write paths. The combination of entrance queuing, GC pauses, and backend I/O congestion led to widespread request slowdown and cascading timeouts.
        - Overall, the incident unfolded in three stages: (1) trigger—ingress overload with concurrent GC and I/O contention; (2) propagation—latency spread across middleware and backend; (3) recovery—delayed cache rebuilds and I/O cleanup causing intermittent tail spikes before stabilization.
        </symptoms>
        
        2. 
        <symptoms>
        - At 20:04:00, the disk read latency for MySQL (Mysql01) spiked significantly, which was followed shortly by a dramatic increase in disk activity across several other components including Redis and Tomcat instances. The pattern suggests a significant load on the storage system that began to impact downstream services.
        - In the same time window, logs from Tomcat02 at 20:25:23 indicate frequent garbage collection events, which contributed to a noticeable delay in request processing. This suggests that GC cycles were negatively affecting the throughput of Tomcat services during peak load.
        - Trace data shows a sustained increase in latency for critical services like MG01, MG02, and Tomcat instances, peaking between 20:08:00 and 20:12:00. These increases align with the metric anomalies in both Redis and MySQL, where read/write operations were spiking at similar times.
        - The overall system impact was evident in the increasing latency for both the ingress (IG01, IG02) and gateway services (MG01, MG02), which started experiencing higher request processing delays from 20:04:00 and continued until 20:14:00. The delays in these key services significantly impacted downstream systems like Tomcat and Redis.
        - The 20:12:00 mark showed a notable increase in the number of requests across all affected services, which seems to align with both the increase in disk usage (shown in metrics) and the spike in processing time (indicated in the trace logs). This could signify that the root cause was the resource saturation affecting storage and memory, which propagated through the system.
        - Based on the convergence of metrics, logs, and traces, the root cause seems to have emerged between 20:04:00 and 20:14:00, during which the system's performance was consistently degraded due to resource contention, likely triggered by disk I/O and memory issues.
        </symptoms>
        3. 
        <symptoms>
        - At **10:26:00**, MySQL metrics such as "Sort Range", "Opened Tables", and "Table open cache misses" spiked significantly, indicating a substantial increase in database load. These spikes were accompanied by high deviation values (up to 7.7σ), pointing to potential resource exhaustion in MySQL, which could have led to slower query execution and a backlog in database processing.
        - Around the same time, **MySQL01** experienced a significant increase in transaction lock memory usage at **10:28:00**, highlighting a possible bottleneck in transaction handling. This caused further delays in database operations, compounding the earlier database-related issues.
        - Concurrently, **Tomcat02** experienced frequent garbage collection events, with one occurring at **10:45:02**, lasting for 0.06 seconds. These pauses in garbage collection created additional delays in request processing, especially during high-load periods, suggesting that memory management issues were negatively impacting Tomcat's ability to handle requests efficiently.
        - **Trace data** indicates a significant increase in latency for critical services such as **MG01** and **MG02** around **10:30:00–10:30:30**, with MaxLatency exceeding 10 seconds. This aligns with the observed database slowdowns and suggests that MySQL's performance degradation triggered latency across multiple services, further compounding the issue.
        - At **10:39:00**, the latency for services like **MG01** and **MG02** increased sharply, affecting downstream systems such as **Tomcat** and **Redis**. This systemic increase in latency points to a cascading failure, where issues in the database layer triggered performance problems in both the application layer and caching layer.
        - Overall, the system performance was degraded across all services, with noticeable latency increases in both the ingress (IG01, IG02) and gateway services (MG01, MG02). These delays began around **10:26:00** and persisted until **10:45:00**, indicating a prolonged performance issue. The degradation of these key services had a significant downstream impact, further compounding the system-wide slowdown.
        - Based on the convergence of metrics, logs, and traces, it appears that the root cause of the performance degradation emerged around **10:26:00**, when MySQL's resource contention began to impact downstream services, triggering cascading latency and delays throughout the system.
        </symptoms>
        """
        self.agent_context.reflect_system_prompt += """
        ##Examples: 
        1.
        <symptoms>
        - Around 18:30:00, the system experienced a simultaneous latency surge at the ingress layer: IG01 and IG02 reported extreme tail latency (MaxLatency ≈ 60001 ms) with more than 100 delayed requests each. Concurrently, Tomcat02 processed about 586 requests with maximum latency reaching 89 s, indicating that frontend requests were heavily queued and timing out.
        - Tomcat02 underwent a sequence of frequent and long garbage collections between 18:30:38 and 18:32:09, with individual GC pauses lasting up to 2.6 s. This caused worker threads to stall and sharply reduced throughput, aligning with the timing of the ingress bottleneck.
        - Storage and local disk I/O metrics spiked on Mysql02 and Tomcat02 at 18:30:00, with high disk busy percentages and read/write throughput peaks. Subsequent CPU “iowait” metrics remained elevated until 19:00, suggesting sustained I/O congestion rather than transient spikes.
        - Between 18:41 and 18:42, latency propagation became system-wide: IG01/IG02, MG01/MG02, and Tomcat01–04 all showed concurrent latency inflation (2–5 s) and request bursts, meaning the slowdown expanded from the ingress to middle-tier services.
        - At 18:53, the slowdown peaked across the full stack—ingress, middleware, and backend. IG01/IG02 each processed over 40 slow requests, MG01/MG02 exceeded 20–40 calls, and dockerB1 reached a 3.8 s maximum latency. The entire request path degraded, not just isolated nodes.
        - Around 18:59, dockerB1 experienced another latency burst (MaxLatency ≈ 7.6 s, 46 calls), coinciding with Redis metrics jumping (used_memory_peak, expired_keys, keyspace_misses). This suggests cache rebuilds and backlog flushing during recovery.
        - The most affected chain was IG → Tomcat02 → MG01/MG02 → dockerB1/Redis/Mysql, covering both read and write paths. The combination of entrance queuing, GC pauses, and backend I/O congestion led to widespread request slowdown and cascading timeouts.
        - Overall, the incident unfolded in three stages: (1) trigger—ingress overload with concurrent GC and I/O contention; (2) propagation—latency spread across middleware and backend; (3) recovery—delayed cache rebuilds and I/O cleanup causing intermittent tail spikes before stabilization.
        </symptoms>
        
        2. 
        <symptoms>
        - At 20:04:00, the disk read latency for MySQL (Mysql01) spiked significantly, which was followed shortly by a dramatic increase in disk activity across several other components including Redis and Tomcat instances. The pattern suggests a significant load on the storage system that began to impact downstream services.
        - In the same time window, logs from Tomcat02 at 20:25:23 indicate frequent garbage collection events, which contributed to a noticeable delay in request processing. This suggests that GC cycles were negatively affecting the throughput of Tomcat services during peak load.
        - Trace data shows a sustained increase in latency for critical services like MG01, MG02, and Tomcat instances, peaking between 20:08:00 and 20:12:00. These increases align with the metric anomalies in both Redis and MySQL, where read/write operations were spiking at similar times.
        - The overall system impact was evident in the increasing latency for both the ingress (IG01, IG02) and gateway services (MG01, MG02), which started experiencing higher request processing delays from 20:04:00 and continued until 20:14:00. The delays in these key services significantly impacted downstream systems like Tomcat and Redis.
        - The 20:12:00 mark showed a notable increase in the number of requests across all affected services, which seems to align with both the increase in disk usage (shown in metrics) and the spike in processing time (indicated in the trace logs). This could signify that the root cause was the resource saturation affecting storage and memory, which propagated through the system.
        - Based on the convergence of metrics, logs, and traces, the root cause seems to have emerged between 20:04:00 and 20:14:00, during which the system's performance was consistently degraded due to resource contention, likely triggered by disk I/O and memory issues.
        </symptoms>
        3. 
        <symptoms>
        - At **10:26:00**, MySQL metrics such as "Sort Range", "Opened Tables", and "Table open cache misses" spiked significantly, indicating a substantial increase in database load. These spikes were accompanied by high deviation values (up to 7.7σ), pointing to potential resource exhaustion in MySQL, which could have led to slower query execution and a backlog in database processing.
        - Around the same time, **MySQL01** experienced a significant increase in transaction lock memory usage at **10:28:00**, highlighting a possible bottleneck in transaction handling. This caused further delays in database operations, compounding the earlier database-related issues.
        - Concurrently, **Tomcat02** experienced frequent garbage collection events, with one occurring at **10:45:02**, lasting for 0.06 seconds. These pauses in garbage collection created additional delays in request processing, especially during high-load periods, suggesting that memory management issues were negatively impacting Tomcat's ability to handle requests efficiently.
        - **Trace data** indicates a significant increase in latency for critical services such as **MG01** and **MG02** around **10:30:00–10:30:30**, with MaxLatency exceeding 10 seconds. This aligns with the observed database slowdowns and suggests that MySQL's performance degradation triggered latency across multiple services, further compounding the issue.
        - At **10:39:00**, the latency for services like **MG01** and **MG02** increased sharply, affecting downstream systems such as **Tomcat** and **Redis**. This systemic increase in latency points to a cascading failure, where issues in the database layer triggered performance problems in both the application layer and caching layer.
        - Overall, the system performance was degraded across all services, with noticeable latency increases in both the ingress (IG01, IG02) and gateway services (MG01, MG02). These delays began around **10:26:00** and persisted until **10:45:00**, indicating a prolonged performance issue. The degradation of these key services had a significant downstream impact, further compounding the system-wide slowdown.
        - Based on the convergence of metrics, logs, and traces, it appears that the root cause of the performance degradation emerged around **10:26:00**, when MySQL's resource contention began to impact downstream services, triggering cascading latency and delays throughout the system.
        </symptoms>"""

    @staticmethod
    def _build_user_prompt(
        time_window: str,
        number: int,
        data_features: Optional[dict]
    ) -> str:
        """
        构建 user_prompt
        """
        prompt = USER_PROMPT.format(time_window)
        prompt += f"The number of failure in this time window is {number}.\n"
        data_features = data_features or {}
        if data_features.get("metric"):
            prompt += f"This is the information derived from metric data: {data_features['metric']}\n"
        if data_features.get("log"):
            prompt += f"This is the information derived from log data: {data_features['log']}\n"
        if data_features.get("trace"):
            prompt += f"This is the information derived from trace data: {data_features['trace']}\n"
        prompt += "Please analyze the above information and find the occurence time of the root cause."
        return prompt