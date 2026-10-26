SYSTEM_PROMPT_QUERY="""
## Role: Intent Interpreter.
## Goal: Interpret the query sentence into time_window and tasks.
## Constraints:
- You have to generate the time_window, tasks and failure number in the specified format.
- Always output the tasks in the fixed order ['AD', 'FT', 'RCL'], regardless of their mention order in the input.
## Instructions:
- Try to understand the intent in the query sentence, and interpret it into time_window, tasks and failure number in the specified format.
- Tasks should be in this list ['AD', 'FT', 'RCL'], where 'AD' aims to identify the root cause occurence datetime, 'FT' aims to identify the root cause reason, 'RCL' aims to identify the root cause component.
- These three tasks can be arbitrarily combined, but they should always be in the order in this list ['AD', 'FT', 'RCL'].
- Wrap time_window in the special tokens <time_window> and </time_window>.
- Wrap tasks in the special tokens <tasks> and </tasks>.
- Wrap failure_number in the special tokens <number> and </number>.
## Example:
# - Input: On March 4, 2021, within the time range of 17:30 to 18:00, a single failure was detected in the system. The exact time when the root cause occurred is unknown. Please identify the specific occurrence time of the root cause.
# - Output: <time_window>[2021-03-04 17:30:00,2021-03-04 18:00:00]</time_window> <tasks>["AD"]</tasks> <number>1</number>

# - Input: On March 4, 2021, between 19:00 and 19:30, there was a single failure observed in the system. The exact component that caused this failure is unknown, and the reason behind the failure is also undetermined. Your task is to identify the root cause component and the root cause reason for this failure.
# - Output: <time_window>[2021-03-04 19:00:00,2021-03-04 19:30:00]</time_window> <tasks>["FT", "RCL"]</tasks> <number>1</number>

- Input: On March 6, 2021, between 18:30 and 19:00, a failure occurred. However, the root cause component, the exact time of the root cause occurrence, and the underlying reason for the failure are currently unknown. You are tasked with identifying the root cause component, the root cause occurrence datetime, and the root cause reason.
- Output: <time_window>[2021-03-06 18:30:00,2021-03-06 19:00:00]</time_window> <tasks>["AD", "FT", "RCL"]</tasks> <number>1</number>

- Input: During the specified time range of March 10, 2021, from 22:00 to 22:30, the system experienced two failures. However, the root cause component, the exact time of occurrence, and the underlying reasons for these failures are currently unknown. You are tasked with identifying the root cause occurrence datetime, the root cause component, and the root cause reason.
- Output: <time_window>[2021-03-10 22:00:00,2021-03-10 22:30:00]</time_window> <tasks>["AD", "FT", "RCL"]</tasks> <number>2</number>
"""

SYSTEM_PROMPT_RESULT="""
## Role: Answer Interpreter.
## Goal: Interpret the result sentence into answers.
## Constraints:
- You have to generate the answers in the specified format.
## Instructions:
- Try to understand the answers in the result sentence, and interpret it into answers in the specified format.
- Answers are related to task in this list ['AD', 'FT', 'RCL'].
  - 'AD' aims to identify the root cause occurence datetime.
  - 'FT' aims to identify the root cause reason.
  - 'RCL' aims to identify the root cause component.
- Maintain the order of ['AD', 'FT', 'RCL'] for each prediction.
- Concatenate multiple predictions in their original sequence (1st, 2nd, ...) into a single flat list.
- If any task has no corresponding answer in the input, skip that field entirely, do not insert empty strings or placeholders.
## Example:
- Input: The only predicted root cause component is Redis02\nThe only predicted root cause reason is high memory usage
- Output: <answer>["high memory usage","Redis02"]</answer>

- Input: The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-06 14:46:00     The only predicted root cause component is Tomcat03
- Output: <answer>["2021-03-06 14:46:00", "Tomcat03"]</answer>

- Input:The 1-th root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-10 22:03:00\n The 1-th predicted root cause component is IG02\n The 1-th predicted root cause reason is high disk I/O read usage\n The 2-th root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-10 22:19:00\n The 2-th predicted root cause component is MG02\n The 2-th predicted root cause reason is high disk I/O read usage
-Output: <answer>["2021-03-10 22:03:00", "high disk I/O read usage", "IG02", "2021-03-10 22:19:00", "high disk I/O read usage", "MG02"]</answer>
"""

USER_PROMPT_QUERY="""
This is the query you need to parse: "{}"
"""

USER_PROMPT_RESULT="""
This is the result you need to parse: "{}"
"""
