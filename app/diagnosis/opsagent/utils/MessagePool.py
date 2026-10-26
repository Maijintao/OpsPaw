from typing import Optional, Union
class Message:
    """message from agent think"""
    def __init__(
        self,
        role_name: str = "",
        answer: str = "",
        thinking: str = "",
        round_num: int = 0
    ):
        self.role_name = role_name
        self.answer = answer
        self.thinking = thinking
        self.round_num = round_num

    def msg2str(self) -> str:
        return (
            f"[Round {self.round_num}]\n"
            f"Role: {self.role_name}\n\n"
            f"Answer: {self.answer}\n\n"
            f"Thinking: {self.thinking}\n\n"
        )


class MessagePool:
    def __init__(self):
        self.messages: list[Message] = []

    def fetch(self) -> list[Message]:
        return self.messages

    def push(self, msg: Message) -> None:
        self.messages.append(msg)

    def get_by_role(self, role: str) -> list[Message]:
        return [msg for msg in self.messages if msg.role_name == role]

    def get_by_round(self, round_num: int) -> list[Message]:
        return [msg for msg in self.messages if msg.round_num == round_num]

    def get_latest_by_role(
        self,
        role: str | list[str]
    ) -> Optional[Message] | dict[str, Optional[Message]]:
        if isinstance(role, str):
            msgs = [msg for msg in self.messages if msg.role_name == role]
            return max(msgs, key=lambda m: m.round_num) if msgs else None
        elif isinstance(role, list):
            result: dict[str, Optional[Message]] = {}
            for r in role:
                msgs = [msg for msg in self.messages if msg.role_name == r]
                result[r] = max(msgs, key=lambda m: m.round_num) if msgs else None
            return result
        else:
            raise ValueError("role must be str or list[str]")

    def get_all_roles(self) -> set[str]:
        return set(msg.role_name for msg in self.messages)

    def get_others_latest(self, role: str) -> list[Message]:
        other_roles = self.get_all_roles() - {role}
        result = []
        for r in other_roles:
            msg = self.get_latest_by_role(r)
            if msg:
                result.append(msg)
        return result
