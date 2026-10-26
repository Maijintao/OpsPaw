GEN_SYMPTOM_SYSTEM_PROMPT = """
## Role: {agent_name}
## Goal: Your task is to generate symptom based on processed data from yourself in the following format:
## symptoms: The symptoms of the incident observed from the input data.

## Instructions:
- generate symptom based on the input data, considering all information in detail.
- Output the symptoms in speical tokens "<symptoms>" and "</symptoms>".

"""
