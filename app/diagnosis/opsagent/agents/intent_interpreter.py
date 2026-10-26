import re
import ast
import yaml
from prompts.intent_interpreter_prompt import SYSTEM_PROMPT_QUERY, USER_PROMPT_QUERY, SYSTEM_PROMPT_RESULT, USER_PROMPT_RESULT
from transformers import AutoModelForCausalLM, AutoTokenizer
from utils.public_function import llm_invoke, llm_invoke_batch_vllm
from box import Box

import sys

class Intent_Interpreter:
    def interprete_query(query, model_path="XXX"): # update to one path of a small model in your environment
        """
        Args:
            queries: from dataset 'instruction' column. eg. On March 4,2021,within the time range of 14:30 to 15:00,a single failure was detected in the system.The exact time when the root cause occurred is unknown.Please identify the specific occurrence time of the root cause.
            model_path:

        Returns:
            time_window: duration time in this query.  eg. [start_time, end_time], [2021-03-10 22:00:00,2021-03-10 22:30:00]
            tasks: types of task in this query. eg. ['AD', 'FT', 'RCL']
            number: number of tasks in this query. eg. 1 or more
        """

        system_prompt = SYSTEM_PROMPT_QUERY
        user_prompt = USER_PROMPT_QUERY.format(query)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype='auto',
            device_map='auto'
        )
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        response, _ = llm_invoke(
            model, tokenizer,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_token=512
        )
        time_window = None
        tasks = []
        pattern = r"<time_window>\[([^\]]+),([^\]]+)\]</time_window>\s*<tasks>(\[[^\]]+\])</tasks>\s*<number>(\d+)</number>"
        match = re.search(pattern, response)

        if match:
            time_start = match.group(1).strip()
            time_end = match.group(2).strip()
            time_window = [time_start, time_end]

            tasks_str = match.group(3)
            tasks = ast.literal_eval(tasks_str)

            number = int(match.group(4))

        return time_window, tasks, number


    # base function fixed
    def interprete_result(result, model_path="XXX"): # update to one path of a small model in your environment
        """
        Args:
            result: from dataset 'scoring_points' column. eg. The only root cause occurrence time is within 1 minutes (i.e., <=1min) of 2021-03-0414:57:00
            model_path:

        Returns:
            answers: list of extracted answer elements from result and LLM's output. eg. ["2021-03-10 22:03:00", "high disk I/O read usage", "IG02", "2021-03-10 22:19:00", "high disk I/O read usage", "MG02"]
        """

        system_prompt = SYSTEM_PROMPT_RESULT
        user_prompt = USER_PROMPT_RESULT.format(result)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype='auto',
            device_map='auto'
        )
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        tokenizer.pad_token = tokenizer.eos_token

        response, _ = llm_invoke(
            model, tokenizer,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_token=512
        )
        pattern = r"<answer>(\[[^\]]*\])</answer>"

        match = re.search(pattern, response)

        answers = []
        if match:
            answer_str = match.group(1)
            answers = ast.literal_eval(answer_str)

        return answers
