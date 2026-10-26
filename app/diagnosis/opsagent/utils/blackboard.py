class Blackboard:
    """

    Attributes:
        task_id: id of blackboard
        time_window:
        tasks:
        answers:
        thinkings:

    """
    def __init__(self, task_id):
        self.task_id = task_id
        self.time_window = None
        self.dataset = None
        self.number = 1
        self.tasks = []
        self.data_features = {"metric": None, "log": None, "trace": None}
        self.reduced_data_features = {"metric": None, "log": None, "trace": None}
        self.answers = {"AD": None, "FT": None, "RCL": None}
        self.co_answers = {"AD": None, "FT": None, "RCL": None}
        self.thinkings = {"AD": None, "FT": None, "RCL": None}
        self.final_answer = None
        self.judge_feedback = {"AD": None, "FT": None, "RCL": None}
        self.accuracy_score = {"AD": 0.0, "FT": 0.0, "RCL": 0.0}
        self.collaboration_history = []

    def update_answer(self, agent_type, answer):
        self.answers[agent_type] = answer
        self.collaboration_history.append((agent_type, answer))
