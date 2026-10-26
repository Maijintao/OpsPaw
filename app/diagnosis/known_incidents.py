"""已知故障窗登记(基线洗刷的第二条通道)。

背景(实测教训):detect 粗筛的历史分布只剔除「已确诊」的故障时段——诊断判
inconclusive 的故障,其指标值留在 24h 历史分布里,同款故障第二次发生时当前值
落回被污染的"正常范围",粗筛失明(2026-10-14 跑批实测:中午 5 例未确诊故障的
数据,把晚上同款故障的分位判定全部稀释,localization 从 45.5% 掉到 2/11)。

这个模块是补丁:任何人(跑批脚本、运维登记的维护窗、告警系统)明确**知道**
"某时段在出故障"时,把窗登记进来,基线洗刷一并剔除。登记是声明不是判定——
不进诊断结论,只让"正常范围"的估计不被故障期污染。

存储:data/known_incidents.json,自动清理超出历史窗长度的旧条目。
失败语义:读不到/写不进都如实降级(基线洗刷是增强,不拖垮粗筛与调用方)。
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("app")

STORE = Path(__file__).resolve().parents[2] / "data" / "known_incidents.json"
def retention_s() -> float:
    """登记窗的保留时长 = 粗筛历史窗 + 1 小时。

    必须跟着 HISTORY_HOURS 走,不能写死:基线窗从 24h 改成 7 天后,25h 的保留期会把
    2~7 天前的故障窗提前丢掉,它们又回到基线里污染分布(2026-10-22 改基线长度时
    一并修正)。懒导入避免 detect ↔ known_incidents 的循环依赖。"""
    from app.diagnosis.detect import HISTORY_HOURS
    return (HISTORY_HOURS + 1) * 3600


def register_window(start: float, end: float) -> bool:
    """登记一个已知故障窗 [start, end](unix 秒)。end<=start 拒收。"""
    if not (end > start > 0):
        return False
    try:
        wins = _load()
        wins.append([float(start), float(end)])
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(wins))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("已知故障窗登记失败:%s", exc)
        return False


def windows(now: float | None = None) -> list[tuple[float, float]]:
    """当前有效的已知故障窗(已清理过期)。读失败返回空——洗刷是增强。"""
    now = time.time() if now is None else now
    out = []
    for ws, we in _load():
        if we >= now - retention_s():
            out.append((float(ws), float(we)))
    return out


def _load() -> list[list[float]]:
    try:
        raw = json.loads(STORE.read_text())
        return raw if isinstance(raw, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("已知故障窗读取失败:%s", exc)
        return []
