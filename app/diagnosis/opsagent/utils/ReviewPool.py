class Review:
    def __init__(self, reviewer: str, target: str, comment: str, accept: bool, round_num: int):
        self.reviewer = reviewer
        self.target = target
        self.comment = comment
        self.accept = accept  # True: accept，False: have comments
        self.round_num = round_num  

    def rev2str(self) -> str:
        return (
            f"[Round {self.round_num}] [Reviewer {self.reviewer}]\n"
            f"To Target: {self.target}\n\n"
            f"Comment: {self.comment}\n\n"
            f"Accept: {self.accept}\n\n"
        )

    
class ReviewPool:
    def __init__(self):
        self.reviews: list[Review] = []

    def push(self, review: Review) -> None:
        self.reviews.append(review)

    def fetch(self) -> list[Review]:
        return self.reviews

    def get_by_target(self, target: str) -> list[Review]:
        return [r for r in self.reviews if r.target == target]

    def get_latest_by_target(self, target: str) -> list[Review]:
        reviews = self.get_by_target(target)
        if not reviews:
            return []
        max_round = max(r.round_num for r in reviews)
        return [r for r in reviews if r.round_num == max_round]

    def get_all_targets(self) -> set[str]:
        return set(r.target for r in self.reviews)
