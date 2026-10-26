"""ReflectionVector 的最小补齐实现(上游仓库未分发此文件,接口按调用方还原)。

OpsAgent 的 agent 在 think 阶段调用 search_similar_symptoms 检索历史经验
(经验库 RAG)。本 stub 提供"空经验库"语义:检索恒返回空列表,agent 走
无经验注入的推理路径——行为正确,只是没有跨会话记忆。写经验(reflect)
落到本地 CSV,格式与上游 base_agent._save_reflection 的读取约定一致。
"""
import csv
import os
from datetime import datetime


class ReflectionVector:
    def __init__(self, persist_dir: str = "", name: str = "agent", *args, **kwargs):
        self.persist_dir = persist_dir or "./reflection_data"
        self.name = name
        self._experiences: list[dict] = []
        os.makedirs(self.persist_dir, exist_ok=True)
        self._csv_path = os.path.join(self.persist_dir, f"{self.name}_reflect.csv")
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._csv_path):
            return
        try:
            with open(self._csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self._experiences.append(dict(row))
        except Exception:
            self._experiences = []

    def search_similar_symptoms(self, symptom: str, top_k: int = 3) -> list[str]:
        """朴素包含匹配:历史经验里与当前症状共享关键词的排前面。空库返回空。"""
        if not symptom or not self._experiences:
            return []
        words = {w for w in symptom.lower().split() if len(w) > 3}

        def _score(exp: dict) -> int:
            text = f"{exp.get('symptoms', '')} {exp.get('experience', '')}".lower()
            return len(words & {w for w in text.split() if len(w) > 3})
        ranked = sorted(self._experiences, key=_score, reverse=True)
        return [e.get("experience", "") for e in ranked[:top_k]
                if _score(e) > 0]

    def add_experience(self, symptoms: str, experience: str) -> None:
        entry = {"symptoms": symptoms, "experience": experience,
                 "ts": datetime.now().isoformat(timespec="seconds")}
        self._experiences.append(entry)
        try:
            new_file = not os.path.exists(self._csv_path)
            with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(["symptoms", "experience"])
                writer.writerow([symptoms, experience])
        except Exception:
            pass
