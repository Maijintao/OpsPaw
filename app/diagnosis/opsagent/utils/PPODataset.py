import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Iterable

class PPODataset:
    def __init__(self, paths: Dict[str, str]):
        self.paths = paths
        self.df = None

    def load(self) -> pd.DataFrame:
        p = pd.read_csv(self.paths["prompts"], usecols=["prompt"])
        a = pd.read_csv(self.paths["answers"], usecols=["answer"])
        s = pd.read_csv(self.paths["scores"],  usecols=["score"])
        n = len(p)
        if not (len(a) == n and len(s) == n):
            raise ValueError(f"Row count mismatch: prompts={len(p)}, answers={len(a)}, scores={len(s)}")
        self.df = pd.DataFrame({
            "prompt":  p["prompt"].values,
            "answer":  a["answer"].values,
            "score":   s["score"].values,
        }).reset_index(drop=True)
        return self.df

    def to_ppo_tuples(self, use_columns: Tuple[str, str, str] = ("prompt", "answer", "score")) -> List[Tuple[str, str, float]]:
        if self.df is None:
            raise RuntimeError("Call .load() first.")
        p_col, a_col, s_col = use_columns
        return list(zip(
            self.df[p_col].astype(str).tolist(),
            self.df[a_col].astype(str).tolist(),
            self.df[s_col].astype(float).tolist()
        ))

    def iter_ppo_batches(
        self,
        batch_size: int,
        shuffle: bool = True,
        use_columns: Tuple[str, str, str] = ("prompt", "answer", "score")
    ) -> Iterable[List[Tuple[str, str, float]]]:
        data = self.to_ppo_tuples(use_columns=use_columns)
        idx = np.arange(len(data))
        if shuffle:
            np.random.shuffle(idx)
        for i in range(0, len(idx), batch_size):
            sel = idx[i:i + batch_size]
            yield [data[j] for j in sel]