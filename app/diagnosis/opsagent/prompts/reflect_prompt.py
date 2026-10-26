REFLECT_SYSTEM_PROMPT = """
## Role: {agent_name}
## Goal: Your task is to reflect on the query, ground truth, processed data, the predicted answer and thinking from yourself, then generate reusable knowledge in the following format:
## symptoms: The symptoms of the incident observed from the input data.
## experience: reusable knowledge to correctly identify the root cause of the incident.

## Instructions:
- Carefully consider the query, ground truth, processed data, the predicted answer and thinking from yourself.
- Reflect on the input data, considering all information in detail.
- Output the symptoms in speical tokens "<symptoms>" and "</symptoms>".
- Output the experience in speical tokens "<experience>" and "</experience>".


"""
