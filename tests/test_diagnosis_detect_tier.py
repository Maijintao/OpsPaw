"""粗筛分层:资源偏离被负载解释时不单独成锚。

## 为什么(2026-10-17 对照组实测)

压测机被洪峰组反复折腾后,内存 841MB 涨到自身历史 p99.2,在**健康平台**上被报成
根因、置信 high——它不是坏了,是活干多了(吞吐同步上涨)。

判据与"时延被下游解释就不算它的可疑度""错误全部由下游引起就是中继"同源:
**能被别人解释的异常,不单独成锚**。这里"别人"是它自己的负载。
"""
from app.diagnosis.detect import TIER_ERROR, TIER_RESOURCE, _load_explained, _tier
from app.diagnosis.stats import POSITION_MIN, Deviation


def _dev(**kw) -> dict:
    """构造四维偏离。默认全正常,按需打开。"""
    base = {k: Deviation(position=0.5, ratio=1.0, basis="")
            for k in ("error", "latency", "calls", "cpu", "memory")}
    base.update(kw)
    return base


def _ab(ratio: float) -> Deviation:
    return Deviation(position=min(0.999, POSITION_MIN + 0.006), ratio=ratio, basis="test")


def test_resource_rise_with_rising_traffic_is_not_an_anchor():
    """CPU 涨 + 吞吐也涨 → 负载推的,不立锚(对照组误报的形态)。"""
    dev = _dev(cpu=_ab(1.19), calls=Deviation(position=0.9, ratio=1.35, basis=""))
    assert _tier(False, False, dev, None, load_explained=True) is None


def test_resource_rise_with_flat_traffic_still_anchors():
    """CPU 涨、吞吐平 → 它自己有问题,照常立资源层(adHighCpu / adManualGc 的形态)。"""
    dev = _dev(cpu=_ab(58.0), calls=Deviation(position=0.5, ratio=1.0, basis=""))
    assert _tier(False, False, dev, None, load_explained=False) == TIER_RESOURCE


def test_error_outranks_the_load_explanation():
    """资源被负载解释,但错误维异常 → 仍按报错层立锚,不被这条规则吞掉。"""
    dev = _dev(error=_ab(5.0), cpu=_ab(1.2))
    assert _tier(False, False, dev, None, load_explained=True) == TIER_ERROR


def test_load_explained_requires_proportional_rise():
    """负载解释必须是**成比例**:资源涨得没比流量多。"""
    # 压测机形态:内存 ×1.19,吞吐 ×1.35 → 解释得掉
    loadgen = _dev(memory=_ab(1.19),
                   calls=Deviation(position=0.9, ratio=1.35, basis=""))
    assert _load_explained(loadgen) is True
    # adHighCpu 形态:CPU ×58,吞吐 ×1.05 → 解释不掉(2026-10-18 实吃过的回归)
    ad = _dev(cpu=_ab(58.0),
              calls=Deviation(position=0.5, ratio=1.05, basis=""))
    assert _load_explained(ad) is False
    # 吞吐在跌(塌陷)→ 不是负载
    dropping = _dev(cpu=_ab(1.2),
                    calls=Deviation(position=0.001, ratio=0.3, basis=""))
    assert _load_explained(dropping) is False
