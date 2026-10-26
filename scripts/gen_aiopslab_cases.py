"""从 AIOpsLab 源码生成 Astronomy Shop 考题清单(评测飞轮的题库地基)。

为什么要有这个脚本、而不是手抄一份清单:
- 手抄的清单会和上游漂移。AIOpsLab 改一道题的故障服务名,我们手里的副本不会跟着变,
  等到跑批才发现"题目和判分口径对不上"——那是评测自欺的经典死法。
- 所以题库**从上游源码生成**:题目 ID 取自它的 problem registry(权威),故障开关 /
  ground truth 服务取自各题目包,判分口径照它 evaluators 的实现。生成物提交进 git
  当冻结资产,上游更新时重跑一次即可,人工只审 diff。

顺手做一件上游没做的事:**校验开关变体**。AIOpsLab 的 inject_otel.py 用的是通用规则
(只有 paymentFailure 给 100%、imageSlowLoad 给 10sec,其余一律 "on"),但 OTel Demo
的 flagd 配置在演进——当前版本里 cartFailure 的变体是百分比档、kafkaQueueProblems 是
秒数档,**都没有 "on" 这个值**。照抄上游的通用规则,这两道题会静默注入失败(flagd 认不出
变体,开关等于没开),而跑批结果看起来只是"这题没抓到"——查起来极其费劲。
所以这里拿目标靶子的 flagd 快照当基准,逐题核对变体是否存在,不合法就如实标出来。

用法:
  uv run python scripts/gen_aiopslab_cases.py \\
      --aiopslab-root /Users/Mai/Desktop/实习项目/AIOpsLab \\
      --flagd-json data/aiopslab/flagd_snapshot.json \\
      --out data/aiopslab/cases.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_AIOPSLAB = Path("/Users/Mai/Desktop/实习项目/AIOpsLab")
DEFAULT_FLAGD = Path(__file__).resolve().parents[1] / "data" / "aiopslab" / "flagd_snapshot.json"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "aiopslab" / "cases.json"

# AIOpsLab inject_otel.py 的通用变体规则(原样搬过来,便于和上游对表):
# 只有这两个开关有专属档位,其余一律 "on"。
_UPSTREAM_VARIANT = {"paymentFailure": "100%", "imageSlowLoad": "10sec"}
_UPSTREAM_DEFAULT_VARIANT = "on"

# 上游规则在**我们这版靶子**上不成立的两处,人工定档(不改上游源码,只在本题库里覆盖):
#   cartFailure         上游给 "on",但本版 flagd 只有百分比档 → 取 100%(全量失败,对应"on"的语义)
#   kafkaQueueProblems  上游给 "on",但本版 flagd 只有秒数档 → 取 3sec(中间档:故障可见又不至于
#                       把整条链拖到全线超时,免得测的变成"超时级联"而不是"队列积压")
# 判分不受影响(ground truth 服务名不变),但**对外报数时要说清这两题用的是覆盖档位**,
# 否则和上游跑分不可比。生成物里同时保留上游原值(flag_variant_upstream)供审计。
_VARIANT_OVERRIDE = {"cartFailure": "100%", "kafkaQueueProblems": "3sec"}


def _norm(text: str) -> str:
    """归一化:小写 + 去掉所有非字母数字。用于"类名 ↔ 题目 ID"的模糊对齐——
    LoadGeneratorFloodHomepage 和 registry 里的 loadgenerator_flood_homepage 拼写
    并不机械对应(下划线位置不同),只能靠归一化后的后缀匹配。"""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def load_registry_keys(aiopslab_root: Path) -> list[str]:
    """题目 ID 的权威来源:problem registry 的字典键。

    注意命名有两套:绝大多数是 `<应用>_<故障组>-<任务>-<实例号>`(如
    astronomy_shop_payment_service_failure-localization-1),但对照题是
    `noop_detection_<应用>`(没有实例号)——第一版正则要求结尾带数字,把对照题整类漏掉了。
    所以这里只按"字典键"的形态抓,任务类型另外从键名里认。"""
    reg = aiopslab_root / "aiopslab" / "orchestrator" / "problems" / "registry.py"
    if not reg.exists():
        sys.exit(f"找不到 problem registry:{reg}(--aiopslab-root 指对了吗)")
    text = reg.read_text()
    keys = re.findall(r'^\s*"([a-z0-9_\-]+)":', text, re.M)
    # 只留 Astronomy Shop 相关的(含对照题 noop_detection_astronomy_shop)
    return sorted({k for k in keys if "astronomy" in k})


_TASKS = ("detection", "localization", "analysis", "mitigation")


def _task_of(key: str) -> str | None:
    """从题目 ID 里认任务类型。两套命名都能认:xxx-localization-1 / noop_detection_xxx。"""
    return next((t for t in _TASKS if t in key), None)


def scan_problem_packages(aiopslab_root: Path) -> list[dict]:
    """扫题目包,只留引用了 AstronomyShop 的。提取:类名 / 故障开关 / ground truth 服务。"""
    root = aiopslab_root / "aiopslab" / "orchestrator" / "problems"
    out: list[dict] = []
    for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(pkg.glob("*.py"))
        if not files:
            continue
        text = "\n".join(f.read_text() for f in files)
        if "AstronomyShop" not in text:
            continue
        tasks = [c for c in re.findall(r"class (\w+)\(", text)
                 if c.endswith(("Detection", "Localization", "Analysis", "Mitigation"))]
        flags = sorted(set(re.findall(r'inject_fault\("([^"]+)"\)', text)))
        svcs = sorted(set(re.findall(r'self\.faulty_service\s*=\s*"([^"]+)"', text)))
        out.append({"package": pkg.name, "classes": tasks,
                    "flags": flags, "services": svcs})
    return out


def resolve_variant(flag: str, flagd_flags: dict) -> dict:
    """定变体:上游规则 → 本题库的人工覆盖 → 拿目标靶子的 flagd 快照核对合法性。"""
    upstream = _UPSTREAM_VARIANT.get(flag, _UPSTREAM_DEFAULT_VARIANT)
    want = _VARIANT_OVERRIDE.get(flag, upstream)
    overridden = want != upstream
    entry = flagd_flags.get(flag)
    if entry is None:
        return {"variant": want, "variant_ok": False, "overridden": overridden,
                "variant_upstream": upstream,
                "variant_note": f"目标靶子的 flagd 里没有 {flag} 这个开关"}
    available = list((entry.get("variants") or {}).keys())
    if want in available:
        note = (f"上游通用档位 {upstream!r} 在本版 flagd 里不存在,已人工定档 {want!r}"
                if overridden else "")
        return {"variant": want, "variant_ok": True, "overridden": overridden,
                "variant_upstream": upstream, "variant_note": note}
    # 覆盖值也不合法(靶子又换代了):如实标出,并给一个最接近的候选
    fallback = next((v for v in ("100%", "3sec", "10sec") if v in available), None)
    if fallback is None and available:
        fallback = next((v for v in available if v != "off"), None)
    return {"variant": want, "variant_ok": False, "overridden": overridden,
            "variant_upstream": upstream,
            "variant_note": (f"档位 {want!r} 不在该开关的合法档位里 "
                             f"(可选:{available})——照抄会导致注入静默失效"),
            "suggested_variant": fallback}


def build_cases(aiopslab_root: Path, flagd_flags: dict) -> tuple[list[dict], list[dict]]:
    keys = load_registry_keys(aiopslab_root)
    pkgs = scan_problem_packages(aiopslab_root)

    def match_pkg(key: str) -> dict | None:
        """题目 ID ↔ 题目包。用"归一化后包含"匹配并取最长命中。

        为什么不能只按包名匹配:包名和题目 ID 里的故障组名不总是一致——
        product_catalog_failure 这个包,对应题目 ID 是
        astronomy_shop_product_catalog_**service**_failure-...,中间多一个 service,
        纯包名包含匹配会漏。所以包名和它的类名都拿来当候选,取最长命中
        (最长是为了防短名误伤:ad_service_failure 不该被 ad_service_high_cpu 抢走)。"""
        nk = _norm(key)
        cands = [(p, _norm(p["package"])) for p in pkgs]
        cands += [(p, _norm(c)) for p in pkgs for c in p["classes"]]
        hits = [(p, n) for p, n in cands if n in nk]
        return max(hits, key=lambda pn: len(pn[1]), default=(None, None))[0]

    cases: list[dict] = []
    problems: list[dict] = []
    for key in keys:
        task = _task_of(key)
        if task is None:
            problems.append({"case_id": key, "issue": "认不出任务类型"})
            continue
        hit = match_pkg(key)
        if hit is None:
            problems.append({"case_id": key, "issue": "registry 里有这题,但找不到对应题目包"})
            continue
        # no_op 是对照题:不注入任何故障,期望答案是"没有故障"——它测的是误报率,
        # 比正例还容易被忽略,单独标出来。
        is_control = hit["package"] == "no_op"
        flag = hit["flags"][0] if hit["flags"] else None
        variant = resolve_variant(flag, flagd_flags) if flag else {
            "variant": None, "variant_ok": True, "variant_note": ""}
        if flag and not variant["variant_ok"]:
            problems.append({"case_id": key, "issue": variant["variant_note"],
                             "flag": flag,
                             "suggested_variant": variant.get("suggested_variant")})
        cases.append({
            "case_id": key,
            "group": hit["package"],
            "task": task,
            "control": is_control,
            "fault_flag": flag,
            "flag_variant": variant["variant"],
            "flag_variant_upstream": variant.get("variant_upstream"),
            "flag_variant_overridden": variant.get("overridden", False),
            "flag_variant_ok": variant["variant_ok"],
            "ground_truth_service": (None if is_control
                                     else (hit["services"][0] if hit["services"] else None)),
            "upstream_class": next((c for c in hit["classes"]
                                    if _norm(c) in _norm(key)), None),
        })
    cases.sort(key=lambda c: (c["group"], c["task"]))
    return cases, problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0])
    ap.add_argument("--aiopslab-root", type=Path, default=DEFAULT_AIOPSLAB,
                    help="AIOpsLab 源码目录(上游题库来源)")
    ap.add_argument("--flagd-json", type=Path, default=DEFAULT_FLAGD,
                    help="目标靶子的 flagd 配置快照(变体校验基准)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="生成物路径")
    args = ap.parse_args()

    flagd = json.loads(args.flagd_json.read_text()) if args.flagd_json.exists() else {}
    flagd_flags = flagd.get("flags") or {}
    if not flagd_flags:
        print(f"[警告] {args.flagd_json} 不存在或没有 flags——变体校验将全部标记为不通过")

    cases, problems = build_cases(args.aiopslab_root, flagd_flags)
    payload = {
        "source": "microsoft/AIOpsLab",
        "app": "astronomy_shop",
        "namespace": "astronomy-shop",
        "flagd_snapshot": args.flagd_json.name,
        "variant_rule_upstream": {**_UPSTREAM_VARIANT, "*": _UPSTREAM_DEFAULT_VARIANT},
        "cases": cases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    by_task: dict[str, int] = {}
    for c in cases:
        by_task[c["task"]] = by_task.get(c["task"], 0) + 1
    print(f"生成 {args.out}({len(cases)} 题)")
    print("  按任务类型:" + "、".join(f"{k} {v}" for k, v in sorted(by_task.items())))
    print("  对照组(no_op):" + str(sum(1 for c in cases if c["control"])) + " 题")
    bad = [c for c in cases if not c["flag_variant_ok"]]
    print(f"  变体校验:通过 {len(cases) - len(bad)}/{len(cases)}")
    ov = sorted({c["fault_flag"] for c in cases if c["flag_variant_overridden"]})
    if ov:
        print(f"  人工定档 {len(ov)} 个开关(上游档位在本版靶子上不存在):"
              + "、".join(f"{f}→{_VARIANT_OVERRIDE[f]}" for f in ov))
        print("  ⚠️  这几点对外报数时要声明,否则和上游跑分不可比")
    if problems:
        print(f"\n⚠️  {len(problems)} 处需要人工定夺:")
        for p in problems:
            extra = (f" → 建议改用 {p['suggested_variant']}"
                     if p.get("suggested_variant") else "")
            print(f"  - {p['case_id']}: {p['issue']}{extra}")


if __name__ == "__main__":
    main()
