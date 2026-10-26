"""故障形态判定(代码判定,LLM 不碰):从已有证据推"这个服务到底是怎么坏的"。

命名直接对齐 AIOpsLab 故障库的故障名(inject_symp.py / inject_app.py / inject_virtual.py),
这样我们的结论能直接和它的 ground truth 对表,不用再转一层。

对齐表(左=我们的 type | AIOpsLab 故障名 | 判据):
  pod_failure      pod_failure / container_kill / scale_pods_to_zero / container_stop
                   ← 遥测静默 / 影子 / 调用量相对基线塌陷(服务整体不可用)
  network_delay    network_delay        ← 只慢不报错,或错误文本是连接超时
  network_loss     network_loss         ← 错误文本:连接被拒 / DNS 解析失败 / 连接重置
  misconfig        misconfig_app / misconfig_k8s / model_misconfig  ← 业务码/参数类错误文本
  auth             revoke_auth / auth_miss_mongodb / storage_user_unregistered
                   ← 证书/TLS、权限/认证类错误文本
  resource_stress  (AIOpsLab 故障库里没有对应项,我们扩展——资源耗尽是真实故障形态)
  code_error       应用报错,机制未定
  unknown          未定性

为什么必须代码判定:结论一直只有"某服务 + 四分语义","什么坏了"是 LLM 在猜的;
而故障形态是评测判分的落点,也是开方能不能落到具体动作的前提。
判据全部来自管线已有数据,零 LLM、可复现。认不出就归 unknown,不硬编。
"""

TYPE_TEXT = {
    "pod_failure": "服务不可用(pod/容器失效)",
    "network_delay": "网络时延",
    "network_loss": "网络不可达/丢包",
    "misconfig": "配置错误",
    "auth": "认证/权限问题",
    "resource_stress": "资源耗尽",
    "code_error": "应用报错(机制未定)",
    "unknown": "未定性",
}

# 错误日志信号 → 故障形态。顺序即优先级,取第一个命中的:
# 不可达类(连接被拒/DNS/重置)最具体;时延次之;认证与配置再次;资源最后。
# 错误文本是唯一不采样、秒级的数据源,最能定性。
_LOG_TO_TYPE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("network_loss", ("连接被拒", "DNS/解析失败", "连接被重置", "依赖不可用")),
    ("network_delay", ("连接超时",)),
    ("auth", ("证书/TLS", "权限/认证")),
    ("misconfig", ("业务码/参数",)),
    ("resource_stress", ("OOM/内存", "磁盘/空间", "限流/资源耗尽")),
)


def _out(ftype: str, basis: list[str]) -> dict:
    return {"type": ftype, "text": TYPE_TEXT.get(ftype, ftype), "basis": basis}


def classify(dev, log_signals: dict | None = None, profile: dict | None = None,
             span_sig: dict | None = None) -> dict:
    """判定故障形态。返回 {"type", "text", "basis"}——basis 是判据人话,进详情页供审计。

    dev 是 ServiceDeviation(可为 None:影子服务没有偏离记录);
    log_signals 是 logsig.classify() 对错误日志的结果;
    span_sig 是 logsig.classify() 对**错误 span 携带属性**(error.type/grpc 状态码/异常
    消息,OTel 标准埋点自带)的结果——与日志同一套标签,但不依赖 Loki 接入,通道更实时;
    profile 是 detect.service_profiles() 的一条(可含 detect.restart_signals 写入的 restart)。"""
    dims = set(getattr(dev, "abnormal_dims", ()) or ()) if dev is not None else set()
    labels = (log_signals or {}).get("labels") or {}
    span_labels = (span_sig or {}).get("labels") or {}
    restart = (profile or {}).get("restart") or {}
    basis: list[str] = []

    # 1) 服务不可用:最具体也最硬的形态,优先——服务整体不出声,别的判据都无从谈起
    if "hang" in dims:
        basis.append("自身遥测静默")
        if profile and profile.get("traffic_collapse"):
            basis.append(f"调用量 {profile['calls']} 次/秒,相对基线 {profile['base_calls']} "
                         "塌陷(服务不可用的实锤)")
        basis.append("对应 AIOpsLab:pod_failure / container_kill / scale_pods_to_zero")
        return _out("pod_failure", basis)
    if "shadow" in dims:
        basis.append("采样窗内零自身 span,仅由调用方的失败请求指认(整体不可用形态)")
        return _out("pod_failure", basis)

    # 2) 资源耗尽:资源维异常由统计判据给出(dims 里已有 cpu/mem,与其余三维同一套)。
    # 这里只负责把它翻成人话,不再拿"超过 N 倍"当准入——判据只有一套(见 deviation)。
    res_hits = [f"{name} 当前为基线的 {ratio:.2f} 倍"
                for name, ratio in (("CPU", getattr(dev, "cpu_ratio", None)),
                                    ("内存", getattr(dev, "mem_ratio", None)))
                if ratio is not None]
    if dims & {"cpu", "mem"}:
        basis.extend(res_hits or ["资源维异常"])
        return _out("resource_stress", basis)

    # 2.5) 窗内重启:容器重启计数增加/OOM——pod_failure 的机制级实锤,比文本推断具体;
    # OOMKilled 被杀且资源维没到阈值时也归 resource_stress(内存耗尽被杀就是资源耗尽)
    if restart.get("restarts"):
        if restart.get("oom"):
            basis.append(f"窗内容器 OOMKilled 被杀(重启 {restart['restarts']:.0f} 次,"
                         "内存耗尽机制被指标直接点名)")
            return _out("resource_stress", basis)
        basis.append(f"窗内容器重启 {restart['restarts']:.0f} 次(重启计数窗内增量,机制级实锤)")
        return _out("pod_failure", basis)

    # 3) 错误信息定性:span 属性通道(不依赖 Loki,更实时)优先,日志通道兜底
    for channel, sig_labels in (("错误 span 携带的错误信息", span_labels),
                                ("错误日志", labels)):
        if not sig_labels:
            continue
        for ftype, names in _LOG_TO_TYPE:
            hit = [n for n in names if sig_labels.get(n)]
            if hit:
                basis.append(f"{channel}:" + "、".join(f"{n} {sig_labels[n]} 条" for n in hit))
                return _out(ftype, basis)

    # 4) 报错 / 时延:有异常,但没有更具体的机制证据——如实说"机制未定",不硬编
    if "error" in dims:
        basis.append("存在错误调用,但错误 span 未携带可定性信息且无日志证据"
                     "(不可达/时延/配置/认证/资源均无法确认)")
        return _out("code_error", basis)
    if "latency" in dims:
        basis.append("时延偏离全图中位数,且没有错误或资源证据")
        return _out("network_delay", basis)
    return _out("unknown", ["证据里没有可定性的异常维"])
