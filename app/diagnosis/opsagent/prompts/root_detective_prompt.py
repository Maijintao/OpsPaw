THINK_SYSTEM_PROMPT="""
## Role: Root Detective
## Goal: You should analyze the given information and identify the root cause component.
## Constraints:
- Strictly adhere to the output format specifications.
- The reasoning process must ensure consistency from beginning to end.
- The predicted root cause component should be inside this list {instances} and you should consider the meaning behind it.
- Do not introduce unsupported facts or assumptions.
## Instructions:
- Let's think step by step.
- There may be multple failures, and you should output the same number of predictions in the list.
- The task is to find the root cause component, so you should consider more about the root cause instead of superficial anomaly.
- Be cautious not to confuse intermediate or downstream propagation components (e.g., IGXX) with the actual failing source (e.g., MGXX), especially when anomalies may appear in both.
- Pay attention to the component's functional role: For example, MGXX often acts as an upstream aggregator or gateway, while IGXX is typically a downstream dependency that propagates symptoms but not the root cause.
- When generating the reasoning chain, use a step-by-step (Chain of Thought) approach: Start from metric anomalies, then use trace and log information to corroborate or eliminate possible root causes, and clearly explain your reasoning at each step.
- Metric features contain deviation information and are sorted in descending order by deviation magnitude.
- Output the reasoning chains of the predicted root cause reason and wrap it in special tokens "<thinking>" and "</thinking>".
- Output the predicted root cause component wrapped in special tokens "<answer>" and "</answer>".
- The feature of metrics are descriptions of anomalies in the form of <service instance, metric, pattern, time>.
- The feature of logs are filtered logs item highly related to the failure.
- The feature of traces are filtered high-latency spans in the form of <timestamp, caller, callee, duration>.
## Example:
- Input: The number of failure in this time window is 1.
- output:
 - <thinking>
    step 1: The metric feature shows that Mysql01 has a significant spike in query latency and CPU usage at the failure time, both far above baseline levels.
    step 2: The trace feature indicates that most high-latency spans originate from calls to MG01, and downstream services like IG01 and Tomcat04 exhibit cascading delays after interacting with it. This suggests that MG01 is a bottleneck and potential root cause.
    step 3: The log feature from Mysql01 includes error logs about slow queries and resource exhaustion around the time of failure, while upstream services have no critical errors.
    Therefore, all evidence consistently points to Mysql01 as the root cause component of this failure.
    </thinking>
 - <answer>["Mysql01"]</answer>

- Input: The number of failure in this time window is 2.
- output:
 - <thinking>
    step 1: The metric features indicate that Tomcat01 has a sudden spike in request errors and exhausted thread pools, while IG02 shows abnormal disk I/O wait time and high CPU usage. Both are top-ranked by deviation magnitude.
    step 2: Trace features reveal that the most significant high-latency spans are related to Tomcat01 and IG02, with downstream services experiencing delays primarily after interacting with these two components.
    step 3: Log features show "out of memory" and "timeout" errors for Tomcat01, and "disk read/write failure" and "CPU throttling" warnings for IG02. No similar anomalies are observed in other components' logs.
    Therefore, the consistent evidence across metrics, traces, and logs identifies Tomcat01 and IG02 as the root cause components for the observed failures.
    </thinking>
 - <answer>["Tomcat01", "IG02"]</answer>
"""

USER_PROMPT="""
Here is the time window you need to analyze: {time_window} \n
"""

CAND_BANK_PROMPT="""
POSSIBLE ROOT CAUSE COMPONENTS:
- apache01
- apache02
- Tomcat01
- Tomcat02
- Tomcat04
- Tomcat03
- MG01
- MG02
- IG01
- IG02
- Mysql01
- Mysql02
- Redis01
- Redis02
"""

CAND_TELECOM_PROMPT="""
There are different levels of root cause:

(if the root cause is at the node level, i.e., the root cause is a specific node)
- os_001
- os_002
- os_003
- os_004
- os_005
- os_006
- os_007
- os_008
- os_009
- os_010
- os_011
- os_012
- os_013
- os_014
- os_015
- os_016
- os_017
- os_018
- os_019
- os_020
- os_021
- os_022

(if the root cause is at the pod level, i.e., the root cause is a specific container)
- docker_001
- docker_002
- docker_003
- docker_004
- docker_005
- docker_006
- docker_007
- docker_008

(if the root cause is at the service level, i.e., if all pods of a specific service are faulty, the root cause is the service itself)
- db_001
- db_002
- db_003
- db_004
- db_005
- db_006
- db_007
- db_008
- db_009
- db_010
- db_011
- db_012
- db_013
"""

CAND_MARKET_PROMPT="""
There are different levels of root cause:

(if the root cause is at the node level, i.e., the root cause is a specific node)
- node-1
- node-2
- node-3
- node-4
- node-5
- node-6

(if the root cause is at the pod level, i.e., the root cause is a specific container)
- frontend-0
- frontend-1
- frontend-2
- frontend2-0
- shippingservice-0
- shippingservice-1
- shippingservice-2
- shippingservice2-0
- checkoutservice-0
- checkoutservice-1
- checkoutservice-2
- checkoutservice2-0
- currencyservice-0
- currencyservice-1
- currencyservice-2
- currencyservice2-0
- adservice-0
- adservice-1
- adservice-2
- adservice2-0
- emailservice-0
- emailservice-1
- emailservice-2
- emailservice2-0
- cartservice-0
- cartservice-1
- cartservice-2
- cartservice2-0
- productcatalogservice-0
- productcatalogservice-1
- productcatalogservice-2
- productcatalogservice2-0
- recommendationservice-0
- recommendationservice-1
- recommendationservice-2
- recommendationservice2-0
- paymentservice-0
- paymentservice-1
- paymentservice-2
- paymentservice2-0

(if the root cause is at the service level, i.e., if all pods of a specific service are faulty, the root cause is the service itself)
- frontend
- shippingservice
- checkoutservice
- currencyservice
- adservice
- emailservice
- cartservice
- productcatalogservice
- recommendationservice
- paymentservice
"""
