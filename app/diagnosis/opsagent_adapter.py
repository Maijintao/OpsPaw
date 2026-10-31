"""OpsAgent 适配层:OpsPaw 活数据 → OpsAgent 三 agent 协作 → 根因结论。

OpsAgent(ASE Industry 2026,AIOpsLab 排行榜 78.75)的源码在 app/diagnosis/opsagent/
原样保留,本适配层不修改其文件(唯一例外:utils/public_function.py 的 llm_invoke
函数体从 transformers 本地推理改为项目统一的 OpenAI 兼容端点——权重不随仓库分发,
这一处不换整条链跑不起来,签名与返回值保持不变)。

适配职责:
1. 活数据 → OpsAgent 期望的 DataFrame 形态:
   - metrics: {服务: pivot_df(index=秒级时间戳, columns=指标名)}(调用量/错误率/
     时延/CPU/内存五条,表达式与 detect.scan 同一套,口径不分叉);
   - logs: 错误日志 DataFrame(timestamp, cmdb_id, log_name, value)(Loki 未接入
     则空表,extract_log_features 自带"无日志特征"降级);
   - traces: span 级 DataFrame(timestamp_ms, duration, type, span_id, parent_id,
     cmdb_id)。
2. 按 OpsAgent 自己的编排序列(process_task)驱动:AD/FT/RCL think → 两两
   cross_review → refine → 汇总 final_answer。agents/prompts/utils 文件零改动。
3. 绕开 process_data(它只认 OPENRCA 的 CSV 目录),data_features 由本层用
   DataProcessor 的特征函数对活数据现算——这正是它"training-free 数据处理器"
   的设计意图:换系统不换处理器。
4. cwd 纪律:OpsAgent 各模块顶层用相对路径读 configs/config.yaml,运行期间
   chdir 到 opsagent 目录,结束切回(跑批单线程顺序执行,进程级 chdir 可接受)。
"""
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("app")

_OPSAGENT_DIR = Path(__file__).resolve().parent / "opsagent"


def _epoch_seconds(ts, default: float) -> float:
    """日志行的 ts 统一成 unix 秒。观测工具(query_platform_logs)吐的是 RFC3339 字符串
    (2026-10-16T06:54:12Z),这里直接 float() 会抛 ValueError 把整条归因打断
    (真事故:cartFailure 那轮归因阶段崩在 'could not convert string to float')。
    数字原样收,字符串按 ISO 解析,解析不了退回 default——日志格式不该弄死诊断。"""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts.strip():
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return default
    return default


@contextmanager
def _opsagent_cwd():
    """OpsAgent 模块顶层与运行期都以相对路径读 configs/ 与写 reflection/,
    期间统一 chdir 到其目录。sys.path 必须同时插入其目录:服务进程(uvicorn)
    的 sys.path 没有 cwd 动态条目,只 chdir 的话 `from agents...` 会 ImportError
    (实测 2026-10-16 跑批第 1 组)。模块加载后 sys.path 条目保留(重复导入无害)。"""
    prev = os.getcwd()
    os.chdir(_OPSAGENT_DIR)
    sp_dirty = str(_OPSAGENT_DIR) not in sys.path
    if sp_dirty:
        sys.path.insert(0, str(_OPSAGENT_DIR))
    try:
        yield
    finally:
        os.chdir(prev)


def _bootstrap_imports():
    """cwd 就位后才能 import(OpsAgent 模块顶层读 config.yaml)。"""
    with _opsagent_cwd():
        from agents.orchestrator import CoreOrchestrator  # noqa: F401
        from agents.ad_agent import ADAgent
        from agents.ft_agent import FTAgent
        from agents.rcl_agent import RCLAgent
        from utils.MessagePool import MessagePool
        from utils.ReviewPool import ReviewPool
        from utils.blackboard import Blackboard
        from utils.data_processer import DataProcessor
        from reflection.ReflectionVector import ReflectionVector
    return (ADAgent, FTAgent, RCLAgent, MessagePool, ReviewPool,
            Blackboard, DataProcessor, ReflectionVector)


async def _build_frames(prom: dict | None, jaeger: dict | None, loki: dict | None,
                        target_name: str | None, services: list[str], spans,
                        window_start: float, window_end: float,
                        log_minutes: int):
    """活数据 → (metrics_map, logs_df, traces_df)。指标表达式与 detect.scan 同一套。"""
    import pandas as pd
    from app.diagnosis import detect

    info = await detect._discover(prom) if prom else None
    metrics_map: dict[str, "object"] = {}
    if info is not None:
        label, fam, status = info["service_label"], info["family"], info.get("status_label")
        win_s = max(1, int(window_end - window_start))
        step = min(detect.SCAN_WINDOW_S, max(15, win_s // 20))
        exprs = {
            "call_rate": f'sum by ({label}) (rate({{__name__="{fam}"}}[{detect.SCAN_WINDOW_S}s]))',
        }
        if status:
            num = (f'sum by ({label}) (rate({{__name__="{fam}",'
                   f'{status}=~"{detect._ERROR_STATUS_RE}"}}[{detect.SCAN_WINDOW_S}s]))')
            den = f'sum by ({label}) (rate({{__name__="{fam}"}}[{detect.SCAN_WINDOW_S}s]))'
            exprs["error_rate"] = f"({num} / {den}) or (0 * {den})"
        if info.get("dur_family"):
            exprs["latency_p95"] = (
                f'histogram_quantile(0.95, sum by (le, {label}) '
                f'(rate({{__name__="{info["dur_family"]}"}}[{detect.SCAN_WINDOW_S}s])))')
        # 逐指标 range 查询,按服务逐点拼 DataFrame(index=秒级时间戳, columns=指标名)
        frames: dict[str, dict[float, dict[str, float]]] = {}
        for name, expr in exprs.items():
            rows = await detect._range(prom, expr, window_start, window_end, step)
            for s in rows:
                svc = (s.get("metric") or {}).get(label)
                if svc not in services:
                    continue
                for ts, v in (s.get("values") or []):
                    if v in ("NaN", "+Inf", "-Inf"):
                        continue
                    frames.setdefault(svc, {}).setdefault(float(ts), {})[name] = float(v)
        for svc, by_ts in frames.items():
            df = pd.DataFrame.from_dict(by_ts, orient="index").sort_index()
            df.index.name = "timestamp"
            metrics_map[svc] = df

        # CPU/内存:与 collect.metric_avg 同源(cadvisor/docker_stats 两套命名都收),
        # 一次 range 拉全平台序列,按服务标签切分(OpsAgent 需要 3-sigma 时间序列,
        # 不能只给均值)。序列 label 里容器名可能带前缀,匹配口径与 metric_avg 一致。
        try:
            from app.tools.service_catalog import _CPU_FAMILIES, _MEM_FAMILIES
            import re as _re
            from app.diagnosis.collect import _client
            async with _client() as c:
                _r = await c.get(f"{prom['url']}/api/v1/label/__name__/values",
                                 headers=prom.get("headers"))
                _r.raise_for_status()
                _families = (_r.json() or {}).get("data") or []
            for metric, pref, pattern in (("cpu", _CPU_FAMILIES, "cpu"),
                                          ("mem", _MEM_FAMILIES, "memory|rss|working_set")):
                picked = [f for f in pref if f in _families] \
                    or [f for f in _families
                        if _re.search(pattern, f) and _re.search(r"container|process|node|system", f)] \
                    or [f for f in _families if _re.search(pattern, f)]
                if not picked:
                    continue
                family = picked[0]
                expr = (f'topk(100, rate({{__name__="{family}"}}[2m]))'
                        if family.endswith("_total")
                        else f'topk(100, {{__name__="{family}"}})')
                rows = await detect._range(prom, expr, window_start, window_end, 60)
                for s in rows:
                    svc = next((sv for sv in services
                                if any(sv.lower() == str(v).lower()
                                       or sv.lower() in str(v).lower().split("/")
                                       for v in (s.get("metric") or {}).values())), None)
                    if svc is None:
                        continue
                    vals = [(float(ts), float(v) / 1e9 if "nanoseconds" in family else float(v))
                            for ts, v in (s.get("values") or [])
                            if v not in ("NaN", "+Inf", "-Inf")]
                    if not vals:
                        continue
                    df = metrics_map.setdefault(svc, pd.DataFrame())
                    mdf = pd.DataFrame(vals, columns=["timestamp", metric]).set_index("timestamp")
                    df = df.join(mdf, how="outer") if not df.empty else mdf
                    metrics_map[svc] = df
        except Exception as exc:  # noqa: BLE001  资源序列失败不拖垮特征构造
            logger.warning("CPU/内存序列构造失败:%s", exc)

    logs_df = pd.DataFrame(columns=["timestamp", "cmdb_id", "log_name", "value"])
    if loki:
        from app.tools.builtin.observability import query_platform_logs
        rows = []
        for svc in services[:10]:
            try:
                raw = await query_platform_logs.ainvoke({
                    "service": svc, "minutes": log_minutes,
                    "target_name": target_name, "filter_text": "error"})
            except Exception:  # noqa: BLE001  日志查询失败不拖垮特征构造
                continue
            if not isinstance(raw, dict) or raw.get("error"):
                continue
            for item in (raw.get("lines") or [])[:50]:
                rows.append({"timestamp": _epoch_seconds(item.get("ts"), window_end),
                             "cmdb_id": svc, "log_name": svc,
                             "value": str(item.get("line", ""))[:300]})
        logs_df = pd.DataFrame(rows)

    traces_df = pd.DataFrame(columns=["timestamp", "duration", "type",
                                      "span_id", "parent_id", "cmdb_id"])
    if spans:
        traces_df = pd.DataFrame([{
            "timestamp": sp.start_us / 1000,          # OpsAgent 约定毫秒
            "duration": sp.duration_us,               # p95 是组内相对比较,单位一致即可
            "type": "http",
            "span_id": sp.span_id,
            "parent_id": sp.parent_span_id or "",
            "cmdb_id": sp.service,
        } for sp in spans])
    return metrics_map, logs_df, traces_df


def attr_from_answers(answer_text: str, answers: list[str],
                      cands: list[dict], service: str,
                      services_all: set[str] | None = None,
                      abnormal: set[str] | None = None) -> "Attribution":
    """OpsAgent 自由文本结论 → OpsPaw 的 Attribution 合同。
    服务名识别范围 = 候选集 + 全平台已知服务(spans 出现过的):OpsAgent 的数据
    特征覆盖全平台,它的结论可能指向候选集外的真因——这正是它比固定候选集强的
    地方,不该被旧合同卡死。多个命中取出现次数最多者;找不到或不承认有根因 →
    inconclusive(诚实优先,不猜)。chain 存推理链原文——它有自己的 Judge 终裁,
    [E#] 引用契约是旧单次归因的合同,不适用本路径。

    abnormal:有异常信号的服务集合(devs 里 abnormal 的那些)。用于**证据支撑检查**:
    OpsAgent 的候选清单是 prompt 里的全平台服务表(instances),它可能挑一个跟本案
    无关的服务——实测 cartFailure 的候选只有 cart/checkout,它却答 frontend,而
    frontend 在整张调用图上没有任何异常维。旧单次归因有"引用完整性校验"做同一件事,
    换 OpsAgent 后这条契约丢了,这里补回窄版:**结论指向候选集外、且自身无异常信号
    的服务时,退回证据支撑的首选候选**(有候选)或 inconclusive(没候选)。
    第二条同源检查:**LLM 只能往证据更强的方向改判**(因果层级更小);同层横向换人或
    跨层降级一律退回首选——同层先后是确定性层已经算过的账(时间/跳数/层内偏离),让 LLM
    重排等于拿证据换猜测(实测 adHighCpu 答了更弱的 checkout;paymentFailure 答了候选第一
    的调用方 checkout,正好停在症状上)。层级定义见 localize.rank_key。"""
    import re
    from app.diagnosis.attr_contract import Attribution
    text = answer_text or " ".join(answers)
    low = text.lower()
    named = ({c["service"] for c in cands} | {service}) | (services_all or set())

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _matches(svc: str) -> list[tuple[int, int]]:
        """Find service-name mentions without treating arbitrary substrings as names.

        The pattern permits punctuation and whitespace between name characters, so
        ``product-catalog`` also matches ``ProductCatalogService``.  A match inside
        another word is rejected unless the remaining suffix is a conventional
        service suffix.  This keeps ``cart`` from winning merely because
        ``cart-service`` or ``cartographer`` was discussed.
        """
        canonical = _norm(svc)
        if not canonical:
            return []
        pattern = re.compile(r"[^a-z0-9]*".join(re.escape(ch) for ch in canonical),
                             re.IGNORECASE)
        found: list[tuple[int, int]] = []
        for match in pattern.finditer(text):
            before = text[match.start() - 1] if match.start() else ""
            after = text[match.end()] if match.end() < len(text) else ""
            if before.isalnum() or after.isalnum():
                suffix = _norm(text[match.end():])
                if not (after.isalnum() and suffix.startswith(("service", "svc", "component"))):
                    continue
            found.append(match.span())
        return found

    matches = {svc: _matches(svc) for svc in named}
    # When names overlap, retain the longest service name for that occurrence.
    # Otherwise a short name (for example ``cart``) can outvote its real service
    # name (for example ``cart-service``) solely because it is a substring.
    occupied: list[tuple[int, int, str]] = []
    for svc, spans in matches.items():
        for start, end in spans:
            occupied.append((start, end, svc))
    counts: dict[str, int] = {svc: 0 for svc in named}
    for start, end, svc in sorted(
            occupied, key=lambda item: (item[0], -len(_norm(item[2])), item[2])):
        overlap = any(start >= old_start and end <= old_end
                      for old_start, old_end, old_svc in occupied
                      if old_svc != svc and len(_norm(old_svc)) > len(_norm(svc)))
        if not overlap:
            counts[svc] += 1
    counts = {svc: count for svc, count in counts.items() if count}
    no_root = any(k in low for k in ("no root cause", "无法确定", "证据不足",
                                     "inconclusive", "cannot determine"))

    # ===== 定案权归确定性层,三个 agent 不再改判(2026-10-30) =====
    # 依据是一次回顾性消融,不用重跑:跑批记录里同时存着"确定性首选"(candidates[0])
    # 和"最终结论"(root_cause_service),直接对比就是消融结果。
    #   完整批次 67 题:只用确定性首选 30/67,多 agent 最终结论 29/67;
    #     它一共改判 2 次,2 次全错——cartFailure 把本来答对的 cart 改成了"无根因",
    #     productCatalogFailure 把 product-catalog 改成了集外的 load-generator。
    #   全部历史 132 题:改判 3 次,3 次全错。
    # 也就是说,让 LLM 推翻确定性层的排序是净负收益。所以这里只留它的"解释"价值:
    # 推理链照旧返回,结论锁死在 cands[0];协商意见不一致时写进 confidence_note 并
    # 提示人工复核,不静默吞掉。
    # 安全前提:平台粗筛"未见异常"的题在 detect 就短路了(事件只有 2 条,24 秒),
    # 根本走不到这里——所以锁死结论不会把无故障题变成误报。
    if not cands or cands[0].get("service") is None:
        return Attribution(verdict="inconclusive", root_cause_service=None, chain=[],
                           confidence_note="确定性层没有给出候选,不猜")

    top = cands[0]["service"]
    chain = [a for a in answers if a][:5]

    if no_root:
        return Attribution(verdict="root_cause", root_cause_service=top, chain=chain,
                           confidence_note=f"三个 agent 协商后未定位到根因,"
                                           f"按确定性层首选定案({top})",
                           missing=["协商未给出根因,结论只依据确定性定位"])

    if counts:
        best = max(counts.values())
        winners = [svc for svc, count in counts.items() if count == best]
        # A tie is genuinely ambiguous; do not let set iteration order choose a
        # service and make repeated diagnoses disagree.
        root = winners[0] if len(winners) == 1 else None
    else:
        root = None

    if root == top:
        return Attribution(verdict="root_cause", root_cause_service=top, chain=chain,
                           confidence_note=f"三个 agent 协商结论与确定性首选一致({top})")
    note = (f"三个 agent 原答 {root},与确定性首选 {top} 不符,按确定性层定案"
            if root else
            f"三个 agent 未给出唯一结论,按确定性层首选定案({top})")
    return Attribution(verdict="root_cause", root_cause_service=top, chain=chain,
                       confidence_note=note,
                       missing=[f"协商结论({root or '未给出'})与确定性首选({top})不一致,"
                                f"建议人工复核归因报告"])


@contextmanager
def _stub_hf_model_loads():
    """OpsAgent 的 agent 初始化会 from_pretrained 本地 14B 权重(config 里是占位符
    "XXX",权重不随仓库分发),而 LLM 调用已桥接到 DeepSeek——model/tokenizer 参数
    不会被消费。运行期把 HF 的两个加载入口 stub 成轻量桩,退出恢复;OpsAgent 文件零改动。"""
    import transformers
    from types import SimpleNamespace
    orig_model = transformers.AutoModelForCausalLM.from_pretrained
    orig_tok = transformers.AutoTokenizer.from_pretrained

    def _fake_model(path, *a, **k):
        return SimpleNamespace(device="cpu")
    def _fake_tok(path, *a, **k):
        tok = SimpleNamespace(eos_token="</s>")
        tok.pad_token = tok.eos_token
        return tok
    transformers.AutoModelForCausalLM.from_pretrained = _fake_model
    transformers.AutoTokenizer.from_pretrained = _fake_tok
    try:
        yield
    finally:
        transformers.AutoModelForCausalLM.from_pretrained = orig_model
        transformers.AutoTokenizer.from_pretrained = orig_tok


async def run_opsagent(symptom: str, service: str, cands: list[dict],
                       spans, *, prom: dict | None, jaeger: dict | None,
                       loki: dict | None, target_name: str | None,
                       window_start: float, window_end: float,
                       log_minutes: int = 60) -> tuple[str, list[str], list[str]]:
    """跑一轮 OpsAgent 三 agent 协作,返回 (final_answer_text, answer_list, thinking_list)。
    结论服务名的解析与 Attribution 组装在调用方(runner)完成——归因合同仍归 OpsPaw。"""
    (ADAgent, FTAgent, RCLAgent, MessagePool, ReviewPool,
     Blackboard, DataProcessor, ReflectionVector) = _bootstrap_imports()

    # 权重适配:OpsAgent 的 agent 初始化会 from_pretrained 本地 14B 权重(config 里
    # 是占位符"XXX"),而 LLM 调用已桥接到 DeepSeek(model/tokenizer 参数不会被消费)。
    # 运行时替换加载逻辑为 no-op——不改 OpsAgent 文件。
    def _noop_init_model(self, *args, **kwargs):
        self.generation_model = None
        self.tokenizer = None
        return None
    ADAgent._init_model_with_optional_peft = _noop_init_model
    FTAgent._init_model_with_optional_peft = _noop_init_model
    RCLAgent._init_model_with_optional_peft = _noop_init_model

    services = sorted({sp.service for sp in spans} |
                      {c["service"] for c in cands} | {service})
    metrics_map, logs_df, traces_df = await _build_frames(
        prom, jaeger, loki, target_name, services, spans,
        window_start, window_end, log_minutes)

    with _opsagent_cwd(), _stub_hf_model_loads():
        processor = DataProcessor()
        data_features: dict = {}
        # ⚠️ 键名必须与 agent 的 _build_user_prompt 对齐:agent 读的是 data_features["metric"] /
        # ["log"] / ["trace"],而 extract_* 返回的是 {"metric_features": ...} / ... ——直接
        # update() 会让 agent 一个特征都读不到,三个 agent 全程"没有数据",只能按架构常识
        # 瞎猜或弃权(2026-10-16 实测:三个 agent 的推理里反复出现 "no metric/trace/log
        # features are provided")。OpsAgent 原版 process_data 就是手动重映射的,适配层漏了。
        # 注意:上游这三个方法定义没写 self(伪静态),必须类调用而不是实例调用;
        # extract_metric_features 吃整个 {实例: pivot_df} dict,内部自循环
        data_features["metric"] = DataProcessor.extract_metric_features(metrics_map)["metric_features"]
        data_features["log"] = DataProcessor.extract_log_features(logs_df)["log_features"]
        data_features["trace"] = DataProcessor.extract_trace_features(traces_df)["trace_features"]

        blackboard = Blackboard(task_id=f"opspaw-{int(time.time())}")
        blackboard.dataset = "Otel"
        blackboard.data_features = data_features
        blackboard.time_window = [
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(window_start)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(window_end))]

        msg_pool = MessagePool()
        review_pool = ReviewPool()
        vdbs = [ReflectionVector(persist_dir="./reflection_data", name=n)
                for n in ("ad", "ft", "rcl")]

        ad = ADAgent("Anomaly Sentinel", blackboard.time_window, 1,
                     data_features, msg_pool, review_pool, vdbs[0])
        ft = FTAgent("Failure Diagnoser", blackboard.time_window, 1,
                     data_features, msg_pool, review_pool, vdbs[1])
        # 候选清单只给排序后的这些,不再把全平台服务表塞给模型
        _cand_names = [c["service"] for c in cands] or None
        rcl = RCLAgent("Root Detective", blackboard.time_window, 1,
                       data_features, msg_pool, review_pool, vdbs[2],
                       instances=_cand_names)

        # 编排:只保留三 agent 各一次 think(3 次 LLM 调用)。上游的 cross_review/refine
        # 每例要 9-15 次调用,大 prompt + DeepSeek 长推理下单例 10 分钟+(实测),砍掉。
        # 三 agent 的独立结论由调用方合成,评分主体(RCL)不受影响。
        ad.think()
        ft.think()
        # 把前两个角色的结论交给根因侦探。三个角色并行跑、互相看不见时,异常分布和
        # 故障形态对最终判定没有任何影响,那两个角色等于白跑。
        _ad_msg = ad.agent_context.last_message() or msg_pool.get_latest_by_role(ad.name)
        _ft_msg = ft.agent_context.last_message() or msg_pool.get_latest_by_role(ft.name)
        _peer_lines = []
        if _ad_msg is not None and _ad_msg.answer:
            _peer_lines.append("异常哨兵判定的异常服务:" +
                               "、".join(str(a) for a in _ad_msg.answer))
        if _ft_msg is not None and _ft_msg.answer:
            _peer_lines.append("故障诊断判定的故障类型:" +
                               "、".join(str(a) for a in _ft_msg.answer))
        if _peer_lines:
            rcl.agent_context.user_prompt = (
                rcl.agent_context.user_prompt
                + "\n\n另外两个角色独立给出的结论(供参考;你可以不同意,不同意要写明理由):\n"
                + "\n".join(f"- {line}" for line in _peer_lines))
        rcl.think()

        # think() 只把消息推进 message_pool;**agent_context.memory 只有 refine() 才写**。
        # 本适配层只跑 think(交叉评审/refine 为成本砍掉),所以必须从 message_pool 取,
        # 否则 last_message() 恒为 None(真事故:AttributeError 'NoneType' ... 'answer')。
        # 这与 OpsAgent 自己的 cross_review/refine 取消息的兜底口径一致。
        def _last_msg(agent):
            return agent.agent_context.last_message() or msg_pool.get_latest_by_role(agent.name)

        ad_msg, ft_msg, rcl_msg = _last_msg(ad), _last_msg(ft), _last_msg(rcl)
        last = rcl_msg or ft_msg
        answers = [str(a) for a in ((last.answer if last is not None else []) or [])]
        thinkings = [str(m.thinking) if m is not None else "" for m in (rcl_msg, ft_msg, ad_msg)]

        # 故障识别校验:模型先分「源头/传导/无关」,再选根因。答案落在传导里就是自相矛盾
        # ——它自己都说"这个异常是下游传上来的",却把它当根因。这种时候退回证据最强的候选。
        # 只读模型自己写的分类行,不额外调模型,零成本。
        if rcl_msg is not None and answers and cands:
            import re as _re
            _th = str(getattr(rcl_msg, "thinking", "") or "")
            _m = _re.search(r"源头\s*[:：]\s*\[([^\]]*)\]", _th)
            if _m:
                _sources = {x.strip().strip('"').strip("'")
                            for x in _m.group(1).split(",") if x.strip()}
                if _sources and answers[0] not in _sources:
                    _top = cands[0]["service"]
                    if _top in _sources:
                        # 模型自己把首选判成了源头,却答了别的 → 用首选
                        answers = [_top] + [a for a in answers if a != _top]
                    else:
                        # 首选也不在源头里:不自作主张,保留模型答案,但记一笔
                        thinkings.append(
                            f"[校验]模型把根因 {answers[0]} 判在「传导/无关」里,"
                            f"自述源头是 {sorted(_sources)}")
    return "\n".join(answers), answers, thinkings
