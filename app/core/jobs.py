"""后台作业白名单。只允许重跑已登记的本地评估与报告命令。"""
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "log" / "jobs"

@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    argv: tuple[str, ...]
    needs: str

JOBS = {
    "cost-report": JobSpec("cost-report", "生成意图 token 用量报告", (sys.executable, "scripts/cost_by_intent.py"), "需要配置 Langfuse"),
    "diagnosis-replay": JobSpec("diagnosis-replay", "离线回放诊断样例", (sys.executable, "scripts/eval_diagnosis.py", "run"), "需要已有离线样例"),
}

@dataclass
class JobRun:
    status: str = "idle"
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    log_path: Path | None = None
    returncode: int | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

_RUNS: dict[str, JobRun] = {}

async def start(name: str) -> None:
    if name not in JOBS:
        raise KeyError(name)
    run = _RUNS.setdefault(name, JobRun())
    if run.status == "running":
        raise RuntimeError("该作业正在运行")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run.log_path = LOG_DIR / f"{name}.log"
    run.status, run.returncode = "running", None
    run.process = await asyncio.create_subprocess_exec(*JOBS[name].argv, cwd=ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    run.task = asyncio.create_task(_collect(run))

async def _collect(run: JobRun) -> None:
    assert run.process is not None and run.process.stdout is not None and run.log_path is not None
    with run.log_path.open("w", encoding="utf-8") as out:
        while line := await run.process.stdout.readline():
            out.write(line.decode("utf-8", "replace")); out.flush()
    run.returncode = await run.process.wait()
    run.status = "ok" if run.returncode == 0 else "failed"

async def stop(name: str) -> None:
    run = _RUNS.get(name)
    if run is None or run.process is None or run.process.returncode is not None:
        raise RuntimeError("该作业当前没有运行")
    run.process.terminate()
    if run.task:
        await run.task
    run.status = "stopped"

def status(name: str) -> dict:
    run = _RUNS.get(name, JobRun())
    log = run.log_path.read_text(encoding="utf-8", errors="replace")[-12000:] if run.log_path and run.log_path.exists() else ""
    spec = JOBS[name]
    return {"name": name, "title": spec.title, "needs": spec.needs,
            "status": run.status, "returncode": run.returncode, "log": log}

def status_all() -> list[dict]:
    return [status(name) for name in JOBS]
