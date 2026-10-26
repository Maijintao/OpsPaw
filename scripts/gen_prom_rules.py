#!/usr/bin/env python3
"""生成时延 p95 预计算(录制规则)文件。

为什么由脚本生成而不是放一份写死的规则文件:规则里必须写直方图指标族名与服务
标签名,这两样因遥测栈而异(粗筛的族发现按特征匹配,同一套提示在这里**复用
detect 的常量**,不另立一份)——对目标 Prometheus 跑一次发现,把发现结果写进
规则;产品换环境时对着新环境重跑本脚本即可,甲方不需要适配任何写死名字。

生成的两段式规则(Prometheus 官方推荐模式,prometheus.io/docs/practices/rules):
  第一段 service:span_latency_bucket_sum:5m = sum by (le, <服务标签>) (rate(<直方图族>[5m]))
  第二段 service:span_latency_p95:5m        = histogram_quantile(0.95, 第一段)
rate 窗 5m 与粗筛 SCAN_WINDOW_S(300s)一致,interval 5m 与 HISTORY_STEP_S 一致
——预计算序列与"每次现算"在刻度上严格同源,读侧换数据源不换尺子。
detect._LAT_P95_RECORDED_HINTS 与这里的第一段名字约定互为镜像,改名两处同步。

用法:
  python3 scripts/gen_prom_rules.py --prom http://<目标>:9090 \
      --out deploy/prometheus/opspaw-span-latency.rules.yml
部署:
  docker cp <out> <prometheus容器>:/etc/prometheus/ 并在 prometheus 配置里加
  rule_files: [/etc/prometheus/opspaw-*.rules.yml](glob 形式:文件缺失时配置仍合法,
  对应"未部署规则→读侧自动退回原始慢路径"的兜底),然后 POST /-/reload。
"""
import argparse
import re
import sys
import urllib.request
import json

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # 仓库根,复用粗筛的族发现常量
from app.diagnosis.detect import (_DUR_FAMILY_HINTS, _DUR_FAMILY_PATTERNS,
                                  _SERVICE_LABELS)

STAGE1 = "service:span_latency_bucket_sum:5m"
STAGE2 = "service:span_latency_p95:5m"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", required=True, help="目标 Prometheus 根 URL")
    ap.add_argument("--out", required=True, help="规则文件输出路径")
    args = ap.parse_args()
    base = args.prom.rstrip("/")

    names = (_get(f"{base}/api/v1/label/__name__/values").get("data") or [])
    dur = next((f for f in _DUR_FAMILY_HINTS if f in names), None)
    if dur is None:
        for pat in _DUR_FAMILY_PATTERNS:
            dur = next((f for f in names if re.search(pat, f, re.I)), None)
            if dur:
                break
    if dur is None:
        print("发现失败:找不到直方图指标族(与粗筛同一套提示)", file=sys.stderr)
        return 1
    sample = _get(f'{base}/api/v1/query?query={{"__name__"="{dur}"}}')
    rows = (sample.get("data") or {}).get("result") or []
    if not rows:
        print(f"发现失败:直方图族 {dur} 无样本", file=sys.stderr)
        return 1
    labels = rows[0].get("metric") or {}
    svc = next((k for k in _SERVICE_LABELS if k in labels), None)
    if svc is None:
        print(f"发现失败:{dur} 的标签里没有服务名(试过 {_SERVICE_LABELS})",
              file=sys.stderr)
        return 1
    if "le" not in labels:
        print(f"发现失败:{dur} 不是直方图(缺 le 标签)", file=sys.stderr)
        return 1

    sel = '{__name__="' + dur + '"}'
    text = f"""# 由 scripts/gen_prom_rules.py 生成(发现自 {base}),勿手改,重跑脚本再生成。
# 两段式:先记每桶速率和(保留 le),再在记录序列上算分位——第二段几乎免费。
# rate 窗与粗筛 SCAN_WINDOW_S=300s 同刻度;interval 与 HISTORY_STEP_S=300s 对齐。
groups:
  - name: opspaw_span_latency
    interval: 5m
    rules:
      - record: {STAGE1}
        expr: sum by (le, {svc}) (rate({sel}[5m]))
      - record: {STAGE2}
        expr: histogram_quantile(0.95, {STAGE1})
"""
    with open(args.out, "w") as f:
        f.write(text)
    print(f"已生成 {args.out}(族={dur}, 服务标签={svc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
