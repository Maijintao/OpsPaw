"""粗筛单维查询失败的降级:一条维瞎了,整场粗筛不许跟着瞎。

## 为什么单测这个(2026-10-18 实测事故)

靶机忙时,时延维那条 24h 直方图 range 查询超过 90 秒超时,异常从 _profile_all
一路冒到 scan_platforms,整个平台被判"粗筛查询失败"→ 整单 inconclusive——
adHighCpu 连续两轮连 OpsAgent 阶段都没进,0/1 不是模型判错,是根本没开始诊断。

修复口径:
- 单维(错误率/时延/CPU/内存)查询失败 → 该维记缺口、本轮不可见,其余维照常判定;
- **查询失败绝不冒充健康**(该维无数据,如实在 note/gaps 里说"对应故障类型可能漏报"),
  也不冒充异常;
- 调用量维不降级:服务清单与画像全集都靠它,它挂了整场没有意义,异常继续向上冒,
  由 scan_platforms 记"粗筛查询失败"→ no_data(诚实出口)。
"""
import httpx
import pytest
from types import SimpleNamespace

from app.diagnosis import detect
from app.diagnosis.stats import Distribution

_INFO = {"family": "calls_total", "service_label": "service_name",
         "status_label": "status_code", "kind_label": "span_kind",
         "dur_family": "duration_bucket"}
_TIMEOUT = httpx.ReadTimeout("timed out")


def _hist(value: float, n: int = 120) -> Distribution:
    """n 个相同值的分布(够 MIN_HISTORY_SAMPLES=100,分位可判)。"""
    return Distribution.build([value] * n)


def _patch_dims(monkeypatch, *, calls_cur, calls_hist, err_cur, err_hist,
                lat_exc: Exception | None):
    """把 _profile_all 的取数层换成桩:按表达式特征分流四个维。
    时延维抛 lat_exc(模拟超时),其余按给定数据返回。"""

    async def _cur(prom, expr, label, now=None):
        if "histogram_quantile" in expr:
            if lat_exc:
                raise lat_exc
            return {}
        if "status_code" in expr:
            return err_cur
        return calls_cur

    async def _hist(prom, expr, now, label, hours=None, exclude=None):
        if "histogram_quantile" in expr:
            if lat_exc:
                raise lat_exc
            return {}
        if "status_code" in expr:
            return err_hist
        return calls_hist

    async def _res(prom, known, *, metric="cpu", now=None, exclude=None, hours=None):
        return {}, {}, None

    async def _discover(prom):
        return dict(_INFO)

    async def _incidents(now):
        return []

    monkeypatch.setattr(detect, "_cur_by_service", _cur)
    monkeypatch.setattr(detect, "_history_dist", _hist)
    monkeypatch.setattr(detect, "_res_profile", _res)
    monkeypatch.setattr(detect, "_discover", _discover)
    monkeypatch.setattr(detect, "_incident_windows", _incidents)


def _patch_scan_extras(monkeypatch):
    """scan_one 的三个旁路信号(错误指认/重启/调用图)全部给空。"""

    async def _no_refs(prom, info, known, now=None):
        return {}

    async def _no_restarts(prom, known, ws, we):
        return {}

    async def _no_edges(prom, info, known, now=None):
        return {}

    monkeypatch.setattr(detect, "_error_refs", _no_refs)
    monkeypatch.setattr(detect, "restart_signals", _no_restarts)
    monkeypatch.setattr(detect, "_call_edges", _no_edges)


async def test_latency_failure_other_dims_still_work(monkeypatch):
    """时延维超时:错误/调用量/资源维照常判定,时延维记缺口。"""
    _patch_dims(monkeypatch,
                calls_cur={"svc": 1.0}, calls_hist={"svc": _hist(1.0)},
                err_cur={"svc": 0.5}, err_hist={"svc": _hist(0.0)},
                lat_exc=_TIMEOUT)
    profiles, gaps = await detect._profile_all({"url": "http://p"}, _INFO,
                                               known={"svc"}, now=1000.0)
    assert "svc" in profiles                       # 画像没有被一条超时清空
    assert profiles["svc"]["dev"]["error"].abnormal  # 错误维照常工作(首次出错即异常)
    assert profiles["svc"]["dev"]["calls"].abnormal is False
    # 时延维:无数据 → 不当健康也不当异常,如实"不做判断"
    assert profiles["svc"]["dev"]["latency"].position is None
    assert any("时延" in g for g in gaps)


async def test_scan_one_anchors_on_error_dim_when_latency_blind(monkeypatch):
    """时延维瞎了,错误维的异常照样出锚点——单维失败不拦诊断。"""
    _patch_dims(monkeypatch,
                calls_cur={"svc": 1.0}, calls_hist={"svc": _hist(1.0)},
                err_cur={"svc": 0.5}, err_hist={"svc": _hist(0.0)},
                lat_exc=_TIMEOUT)
    _patch_scan_extras(monkeypatch)
    res = await detect.scan_one({"url": "http://p"}, platform="t")
    assert res["observed"] is True
    assert [a["service"] for a in res["anchors"]] == ["svc"]
    assert res["anchors"][0]["tier"] == detect.TIER_ERROR
    assert any("时延" in g for g in res["gaps"])


async def test_clear_note_admits_blind_dim(monkeypatch):
    """其余维全正常 + 时延维瞎了:不许说纯健康,note 必须写明缺口与漏报风险。"""
    _patch_dims(monkeypatch,
                calls_cur={"svc": 1.0}, calls_hist={"svc": _hist(1.0)},
                err_cur={"svc": 0.0}, err_hist={"svc": _hist(0.0)},
                lat_exc=_TIMEOUT)
    _patch_scan_extras(monkeypatch)
    res = await detect.scan_one({"url": "http://p"}, platform="t")
    assert res["observed"] is True
    assert res["anchors"] == []
    assert "时延" in res["note"] and "漏报" in res["note"]


async def test_calls_dim_failure_keeps_whole_scan_blind(monkeypatch):
    """调用量维挂了(服务清单没了)= 整场没有意义 → 继续抛,由上层记失败。
    这是设计决策的锚:不许把"核心维挂了"降级成"部分可见"。"""

    async def _cur(prom, expr, label, now=None):
        raise _TIMEOUT

    async def _discover(prom):
        return dict(_INFO)

    async def _incidents(now):
        return []

    monkeypatch.setattr(detect, "_cur_by_service", _cur)
    monkeypatch.setattr(detect, "_discover", _discover)
    monkeypatch.setattr(detect, "_incident_windows", _incidents)
    with pytest.raises(httpx.ReadTimeout):
        await detect.scan_one({"url": "http://p"}, platform="t")


async def test_scan_platforms_surfaces_gaps(monkeypatch):
    """平台层把单维缺口如实汇总进 note 与 gaps,状态仍算 observed(其余维看见了)。"""
    rows = [SimpleNamespace(enabled=True, name="t",
                            sources=[{"type": "prometheus", "url": "http://p"}])]

    async def _list_targets():
        return rows

    async def _scan_one(prom, *, platform, cluster="", now=None):
        return {"anchors": [], "observed": True,
                "gaps": ["时延维查询失败(ReadTimeout),该维度本轮不可见"],
                "note": "各服务与自身历史分布比未见异常(仅限看得见的维度);"
                        "时延维查询失败(ReadTimeout),该维度本轮不可见——"
                        "对应故障类型本轮可能漏报"}

    monkeypatch.setattr(detect.repository, "list_targets", _list_targets)
    monkeypatch.setattr(detect, "scan_one", _scan_one)
    out = await detect.scan_platforms()
    assert out["status"] == "clear"
    assert out["gaps"] and "时延" in out["gaps"][0]
    assert "漏报" in out["note"]


async def test_scan_platforms_total_failure_still_no_data(monkeypatch):
    """整场粗筛失败(如调用量维挂了)仍然 no_data——不许当健康放行(原行为保留)。"""
    rows = [SimpleNamespace(enabled=True, name="t",
                            sources=[{"type": "prometheus", "url": "http://p"}])]

    async def _list_targets():
        return rows

    async def _scan_one(prom, *, platform, cluster="", now=None):
        raise _TIMEOUT

    monkeypatch.setattr(detect.repository, "list_targets", _list_targets)
    monkeypatch.setattr(detect, "scan_one", _scan_one)
    out = await detect.scan_platforms()
    assert out["status"] == "no_data"
    assert "粗筛查询失败" in out["note"]
