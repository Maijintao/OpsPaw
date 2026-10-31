"""判据数字的出处闸:app/diagnosis 下每个数值常量(任意作用域)都必须登记在 thresholds.REGISTRY。

这是《裁判层整改方案》§2.3 的"出处纪律"落地:新的硬编码阈值进不来——包括写在函数体
里的那种(只扫模块级会漏,见 _numeric_constants 的说明)。
同时把 PENDING(待整改的拍值)钉成一个只降不升的预算——整改一个就少一个。
"""
import ast
import math
from pathlib import Path

from app.diagnosis import thresholds

_DIAG_DIR = Path(__file__).resolve().parents[1] / "app" / "diagnosis"

# 允许出现在数值常量里的"推导函数"——math.ceil(...) 这种由公式算出来的常量要登记,
# asyncio.Semaphore(...) 这种对象不是阈值,自动排除。
_NUMERIC_FUNCS = {"ceil", "floor", "round", "abs", "max", "min", "int", "float", "sqrt", "log"}

# PENDING 预算(只降不升):整改完一个就从这里减一,新增拍值会直接撞线。
# 阶段一整改后只剩 2 项(门禁的指认强度门槛,属阶段二"裁判收口")。
PENDING_BUDGET = 2


def _is_numeric_value(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, (ast.BinOp, ast.UnaryOp)):
        return True
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (
            f.id if isinstance(f, ast.Name) else "")
        return name in _NUMERIC_FUNCS
    return False


def _numeric_constants(path: Path) -> list[str]:
    """**任意作用域**(模块级 + 函数/类体内)的 UPPER_CASE 数值常量。

    必须覆盖函数体:detect.scan_one 曾把 REF_STRONG_RATIO 写死在函数内部,只扫模块级
    的话它整条漏过去——那正是这个闸要拦的东西(2026-10-16 补)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not _is_numeric_value(node.value):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.upper() == t.id and any(c.isalpha() for c in t.id):
                out.append(t.id)
    return sorted(set(out))


def _all_constant_names() -> set[str]:
    names: set[str] = set()
    for p in sorted(_DIAG_DIR.glob("*.py")):
        names.update(_numeric_constants(p))
    return names


def test_every_constant_is_registered():
    """任意作用域的判据常量都必须登记出处——没登记的当场拦下。"""
    missing = []
    for p in sorted(_DIAG_DIR.glob("*.py")):
        for name in _numeric_constants(p):
            if name not in thresholds.REGISTRY:
                missing.append(f"{p.name}:{name}")
    assert not missing, (
        "以下判据常量未登记出处,请写进 app/diagnosis/thresholds.REGISTRY:"
        + "、".join(missing))


def test_registry_has_no_stale_entries():
    """登记表里不能留已经不存在的常量(防止表与代码漂移)。"""
    existing = _all_constant_names()
    stale = sorted(set(thresholds.REGISTRY) - existing)
    assert not stale, f"登记表里的这些常量已不存在:{stale}"


def test_registry_entries_are_complete():
    """每条登记都要有类别、单位、出处;PENDING 还必须有整改方案。"""
    valid = {"A", "B", "C", "PENDING"}
    for name, meta in thresholds.REGISTRY.items():
        assert meta.get("category") in valid, f"{name} 类别非法:{meta.get('category')}"
        assert meta.get("unit"), f"{name} 缺单位"
        assert meta.get("source"), f"{name} 缺出处"
        if meta["category"] == "PENDING":
            assert meta.get("plan"), f"{name} 是待整改项但没写整改方案(plan)"


def test_pending_budget_only_shrinks():
    """待整改的拍值数量只降不升——新增一个拍值就会撞线。"""
    pend = thresholds.pending()
    assert len(pend) <= PENDING_BUDGET, (
        f"待整改拍值 {len(pend)} 个,超过预算 {PENDING_BUDGET}:{pend}")


def test_derived_constants_match_their_formula():
    """A/B 类里"由公式反解"的常量必须与公式一致,防止有人改回裸字面量。"""
    from app.diagnosis import deviation, stats
    assert stats.POSITION_MIN == 1.0 - stats.FALSE_ALARM_PER_TEST
    assert stats.MIN_HISTORY_SAMPLES == math.ceil(1.0 / stats.FALSE_ALARM_PER_TEST)
    assert deviation.TRACE_MIN_SAMPLES == math.ceil(1.0 / (1.0 - deviation.LATENCY_QUANTILE))
