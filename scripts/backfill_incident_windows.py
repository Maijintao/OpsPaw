"""把历史跑批的故障注入窗回填进诊断存档(diagnosis_runs),幂等可重跑。

为什么需要:基线剔除(detect._incident_windows)只认 verdict=root_cause/critical_failure
的诊断窗——但**被判失的故障**(粗筛漏报的 cart/kafka/image_slow 这些组)没有根因结论,
它们的开窗不在存档里,故障期的指标照样污染 24h 历史分布,下一轮跑批同款故障照样失明。
跑批记录(group json)本身知道每次注入的时刻:有 data_quality 窗用精确窗;没有的
(clear 早退组)按"注入≈诊断前 185s,恢复≈诊断后 120s"重建。回填行 id 用
bk+hash,重跑自动跳过已存在的。
"""
import asyncio
import json
import sys
from datetime import datetime
from hashlib import md5
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval_detect_offline import RUNS_DIR, _diag_moment  # noqa: E402


async def main() -> None:
    from app.db import base as appdb
    from app.db import repository
    from app.db.models import DiagnosisRun
    await repository.ensure_diagnosis_table()

    inserted = skipped = 0
    for run_dir in sorted(p for p in RUNS_DIR.iterdir() if p.is_dir()):
        for path in sorted(run_dir.glob("*.json")):
            if path.name in ("summary.json", "offline_detect_replay.json"):
                continue
            blob = json.loads(path.read_text())
            if not isinstance(blob, dict) or "cases" not in blob:
                continue
            cases = blob.get("cases") or []
            if not cases or cases[0].get("control"):
                continue                      # 对照组没注入,没有故障窗
            run = blob.get("run") or {}
            dq = ((run.get("result") or {}).get("data_quality") or {}).get("trace") or {}
            ws, we = dq.get("window_start"), dq.get("window_end")
            if not (ws and we):
                at = _diag_moment(path, run.get("events") or [], skip_offset=0)
                if at is None:
                    continue
                ws, we = at - 185, at + 120   # 注入≈诊断前 settle,恢复≈诊断后
            rid = "bk" + md5(f"{run_dir.name}/{path.stem}".encode()).hexdigest()[:13]
            async with appdb.async_session() as s:
                if await s.get(DiagnosisRun, rid) is not None:
                    skipped += 1
                    continue
                s.add(DiagnosisRun(
                    id=rid, service=str(blob.get("group") or path.stem)[:128],
                    symptom=f"回填故障窗(跑批 {run_dir.name})", status="done",
                    verdict="root_cause", root_cause_service=None, events=[],
                    result={"data_quality": {"trace": {
                        "window_start": float(ws), "window_end": float(we)}}}))
                await s.commit()
            inserted += 1
            print(f"回填 {run_dir.name}/{path.stem}: "
                  f"{datetime.fromtimestamp(float(ws)):%m-%d %H:%M} ~ "
                  f"{datetime.fromtimestamp(float(we)):%H:%M}")
    print(f"\n完成:新增 {inserted},已存在跳过 {skipped}")


if __name__ == "__main__":
    asyncio.run(main())
