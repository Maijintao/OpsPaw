"""时延历史读取的预计算序列选择(录制规则优先,原始表达式兜底)。

## 为什么有这条路径(2026-10-25 实测)

粗筛时延维的历史读取是"每次现算多日直方图分位":48h 跨度 120s,已超 90s 超时
(当夜跑批"时延维查询失败"整维失明);7 天数据线性外推单条 5min+,必死。
治理:部署录制规则把同一表达式变成每 5 分钟一次的增量预计算,历史读取改读
预计算序列(毫秒级);规则未部署的环境/旧录制件回放自动退回原始慢路径,
功能不缺只是慢——退化由数据发现决定,不靠特殊分支。

当前窗与发病时刻检测不走预计算(新鲜度/分钟粒度),仍用原始即时查询。
"""
from app.diagnosis.detect import _latency_hist_expr


def test_recorded_series_preferred_when_present():
    info = {"lat_p95_recorded": "service:span_latency_p95:5m"}
    raw = 'histogram_quantile(0.95, sum by (le, service_name) (rate(x[300s])))'
    assert _latency_hist_expr(info, raw) == "service:span_latency_p95:5m"


def test_falls_back_to_raw_when_rules_absent():
    """规则未部署(或旧录制件回放)→ 原始表达式,功能不缺。"""
    assert _latency_hist_expr({"lat_p95_recorded": None}, "raw_expr") == "raw_expr"
    assert _latency_hist_expr({}, "raw_expr") == "raw_expr"
