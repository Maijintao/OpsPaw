"""判据数字的出处登记表(见《裁判层整改方案》§2.2 / §2.3)。

纪律:`app/diagnosis/` 下每个模块级数值常量都必须在这里登记一行,写明类别与出处。
未登记的常量由 `tests/test_diagnosis_threshold_registry.py` 拦下——**新的硬编码阈值
进不来**,这是防止"改完又长回去"的闸。

类别(只允许前三类长期存在):
- A 可推导:由数据、物理规律或统计算出。代码里应保留成计算式,不留裸字面量。
- B 有保证:由目标误报率 / 置信度反解出来的阈值,语义是"我们接受多少误报"。
- C 策略:产品决策量(查询预算、超时、并发、采样节奏)。进配置,带单位与依据。
- PENDING 待整改:当前仍是拍值,已列入《裁判层整改方案》,必须限期消除。
  每个 PENDING 必须带 plan(怎么改),由测试强制非空。

本表只登记**出处**,不重复数值——数值以各模块里的定义为准,避免两处漂移。

已整改(2026-10-15,阶段一):原先 12 项拍值里 10 项已改为可推导/有保证,详见下表
对应条目的 source 与《裁判层整改方案》执行进度。门禁退役后其否决能力下沉到打分,
原来的两个门槛随之消失;但其中"指认强度占比"以 REF_STRONG_RATIO 的形式留在了
detect.scan_one 函数体内(2026-10-16 补登记为 PENDING,1 项),待与其余四维同源化。
"""
from typing import Literal

Category = Literal["A", "B", "C", "PENDING"]

# name -> {category, unit, source(出处/推导), plan(PENDING 专用:整改方案)}
REGISTRY: dict[str, dict] = {
    # ---------- stats.py:异常判定的地基 ----------
    "FALSE_ALARM_PER_TEST": {
        "category": "C", "unit": "概率",
        "source": "策略:单次'某服务某维异常'判定允许的目标误报率(全系统唯一的误报旋钮)",
    },
    "POSITION_MIN": {
        "category": "B", "unit": "分位",
        "source": "由 FALSE_ALARM_PER_TEST 反解:POSITION_MIN = 1 − α。"
                  "粗筛按 Bonferroni 摊到 m 次判断上(见 stats.position_min),"
                  "因为一次扫描同时判 14 服务 × 6 维 = 84 次,不摊就等于每次扫描过半概率误报",
    },
    "POSITION_BEYOND_MAX": {
        "category": "A", "unit": "分位",
        "source": "定义:位置 1.0 的含义就是'大于历史全部样本',不是阈值",
    },
    "MIN_HISTORY_SAMPLES": {
        "category": "B", "unit": "样本数",
        "source": "由 α 反解:ceil(1/α)。n 个点最多分辨 1/n 的分位,低于它门槛不可分辨",
    },
    "ZERO_BASELINE_EVENTS": {
        "category": "A", "unit": "次数/窗口",
        "source": "三倍法则:0/n 观测的发生率 95% 置信上界 = 3/n(泊松)。"
                  "用在历史全 0 的退化分布上,把'一两个偶发失败'挡在异常之外",
    },
    "BAND_SENSITIVITY": {
        "category": "C", "unit": "倍 IQR",
        "source": "灵敏度档位:判据边界 = p99 + n×IQR(n 取 4)。边界值由数据算"
                  "(IQR 是该服务自身波动),这里只定'几倍波动才算异常'。"
                  "取 4 与 Dynatrace 自适应基线的默认灵敏度同档,不是为本靶子调的",
    },
    "ROBUST_MAD_K": {
        "category": "A", "unit": "倍 MAD",
        "source": "清洗基线的离群点界限:偏离中位数超过 k×MAD 的窗口判为故障期,"
                  "不进基线。取 6 是统计惯例(MAD≈0.6745σ,6×MAD≈4σ,正常数据"
                  "几乎不会被误剔);不剔的话 p99 会落在故障水平上——2026-10-23 实测"
                  "2% 未登记故障窗就把 frontend 的边界撑到 15000ms / 28% 错误率。"
                  "清洗的**总量**上限见 stats.CONTAMINATION_MAX(直接取 FALSE_ALARM_PER_TEST,"
                  "不是新拍的数:超过 α 的偏离是这类指标本来的样子,不是故障留下的痕迹)",
    },

    # ---------- deviation.py:四维偏离 ----------
    "ERROR_SMOOTH": {
        "category": "A", "unit": "无量纲",
        "source": "数值稳定性:全服务零错误时中位数为 0,靠它防 0 除。不是判据",
    },
    "ERROR_DEV_LOG_BASE": {
        "category": "C", "unit": "对数底",
        "source": "展示刻度约定(错误维取对数防高错误率区饱和)",
    },
    "LATENCY_QUANTILE": {
        "category": "C", "unit": "分位",
        "source": "策略:'慢'的定义分位(我们说的 p95 就是这个分位)",
    },
    "TRACE_MIN_SAMPLES": {
        "category": "A", "unit": "样本数",
        "source": "由 LATENCY_QUANTILE 反解:ceil(1/(1−q))。p95 之上平均至少 1 个观测",
    },

    # ---------- detect.py:粗筛 ----------
    "SCAN_TIMEOUT": {"category": "C", "unit": "秒", "source": "策略:粗筛查询超时"},
    "RANGE_TIMEOUT": {"category": "C", "unit": "秒",
                      "source": "实测:24h histogram_quantile 需 41s(靶机 2 核),取一倍余量"},
    "SCAN_WINDOW_S": {"category": "C", "unit": "秒", "source": "策略:粗筛当前窗长度"},
    "RECENT_WINDOWS": {
        "category": "C", "unit": "窗数",
        "source": "策略:判'这次新出的变化'时用的近期尺度(12 窗 = 1 小时,不含当前窗)。"
                  "为什么是它:诊断窗与 settle 各 5 分钟,单个窗的抖动压不住 12 个窗的典型"
                  "水平;而 1 小时又短到能反映'今天这会儿平台是什么样'。判据公式与长期"
                  "基线完全同源,只换时间尺度,不引入新判据",
    },
    "HISTORY_HOURS": {"category": "C", "unit": "小时",
                      "source": "策略:历史窗长度(与 HISTORY_STEP_S 共同决定样本量)"},
    "HISTORY_STEP_S": {"category": "C", "unit": "秒",
                       "source": "策略:历史采样步长(与 Prometheus 抓取节奏对齐)"},
    "TIER_STATE_CHANGE": {"category": "A", "unit": "序数",
                          "source": "可疑度层级定义(因果优先级),不是阈值"},
    "TIER_BLAMED": {"category": "A", "unit": "序数", "source": "同上"},
    "TIER_TRAFFIC": {"category": "A", "unit": "序数", "source": "同上"},
    "TIER_TRAFFIC_SURGE": {"category": "A", "unit": "序数",
                           "source": "同上:调用量暴涨与塌陷同层,都在错误/时延之前"},
    "TIER_ERROR": {"category": "A", "unit": "序数", "source": "同上"},
    "TIER_LATENCY_SELF": {"category": "A", "unit": "序数", "source": "同上"},
    "TIER_RESOURCE": {"category": "A", "unit": "序数", "source": "同上"},
    "TIER_LATENCY_RELAY": {"category": "A", "unit": "序数", "source": "同上"},
    "REF_STRONG_RATIO": {
        "category": "PENDING", "unit": "占比",
        "source": "策略:强指认的占比门槛——区分'调用方正在整条操作上失败'(实测 AddItem 100%)"
                  "与'低频操作的慢性背景错误'(实测 checkout EmptyCart 18%)。"
                  "原属已退役的 attribution 模块,退役后本地落在 detect.scan_one 函数体内,"
                  "2026-10-16 补登记。",
        "plan": "与其余四维同源化:改用'操作级错误率相对自身历史分位'判强指认,"
                "或由目标误报率反解门槛,消除裸 0.5。",
    },

    # ---------- collect.py ----------
    "UPSTREAM_TIMEOUT": {"category": "C", "unit": "秒", "source": "策略:上游查询超时"},
    "TRACE_FETCH_TIMEOUT": {"category": "C", "unit": "秒",
                            "source": "策略:trace 拉取超时(带宽实测依据见模块注释)"},

    # ---------- attribution.py:门禁与证伪 ----------
    # ---------- localize.py ----------
    "MAX_CANDIDATES": {"category": "C", "unit": "个数",
                       "source": "策略:候选上限(prompt 预算保险丝)"},

    # ---------- edge_anomaly.py:调用边时延检测(架构移植自 MicroRCA-Agent,检测核 MAD 判线) ----------
    "WIN_SIZE_S": {
        "category": "C", "unit": "秒",
        "source": "移植参数:MicroRCA-Agent 原方案原值(CCF AIOps 2025 决赛 Top5 方案,"
                  "滑窗窗长,决定每边特征样本量)",
    },
    "MIN_TRAIN_WINDOWS": {
        "category": "C", "unit": "窗数",
        "source": "本地化策略:单边最少训练窗数(画像可用性下限)——正常期几乎没有"
                  "流量的边不建画像,推理时如实报无画像,不用小样本硬猜",
    },
    "MIN_ANOMALY_WINDOWS": {
        "category": "C", "unit": "窗数",
        "source": "本地化策略:单边最少异常窗数(误报控制)——单窗超界在目标误报率下"
                  "是预期内噪声(α=1% 时 20 窗期望 0.2 窗误标)",
    },
    "MODEL_DIR": {
        "category": "C", "unit": "路径",
        "source": "策略:调用边画像文件存放位置(data/iforest),训练脚本与推理共用",
    },
    "EVENT_CAP": {
        "category": "C", "unit": "条数",
        "source": "策略:证据包里调用边事件行上限(prompt 预算保险丝,同 MAX_CANDIDATES 定位)",
    },

    # ---------- investigate.py:归因调查循环(架构移植自 AIOpsLab REACT) ----------
    # ---------- known_incidents.py:已知故障窗登记(基线洗刷第二通道) ----------
    "STORE": {
        "category": "C", "unit": "路径",
        "source": "策略:登记文件存放位置(data/known_incidents.json)",
    },
    "_OPSAGENT_DIR": {
        "category": "C", "unit": "路径",
        "source": "策略:OpsAgent 源码目录(opsagent/),适配层运行期 chdir 的锚点",
    },

    # ---------- refill.py ----------
    # ---------- runner.py ----------
    "MAX_RUNS": {"category": "C", "unit": "个数", "source": "策略:内存诊断任务上限"},
    "DIAG_MAX_QUERIES": {"category": "C", "unit": "次数",
                         "source": "策略:单次诊断上游查询预算"},
    "DIAG_MAX_SECONDS": {"category": "C", "unit": "秒",
                         "source": "策略:单次诊断时长预算"},
    "MAX_CONCURRENT_DIAGNOSES": {"category": "C", "unit": "个数",
                                 "source": "策略:并发诊断数上限"},
}


def pending() -> list[str]:
    """尚未整改的拍值清单(供 CI / 文档引用)。"""
    return sorted(n for n, m in REGISTRY.items() if m["category"] == "PENDING")
