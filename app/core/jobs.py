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
    "kb-preview": JobSpec("kb-preview", "材料清单与切块预览", (sys.executable, "scripts/show_kb.py"), "无需额外服务"),
    "kb-build": JobSpec("kb-build", "离线建库", (sys.executable, "scripts/build_kb.py"), "需要 MySQL"),
    "kb-vectorize": JobSpec("kb-vectorize", "向量化待处理知识块", (sys.executable, "scripts/vectorize_kb.py"), "需要 MySQL、嵌入上游和 Milvus"),
    "kb-repatch": JobSpec("kb-repatch", "补丁式重嵌知识块", (sys.executable, "scripts/kb_repatch.py"), "需要 MySQL 和嵌入上游"),
    "kb-mine": JobSpec("kb-mine", "从历史对话挖掘知识", (sys.executable, "scripts/mine_knowledge.py"), "需要 MySQL 和聊天上游"),
    "eval-rag": JobSpec("eval-rag", "四策略 RAG 离线评估", (sys.executable, "scripts/eval_ch04.py"), "需要知识库、Milvus 和模型上游"),
    "calibrate-confidence": JobSpec("calibrate-confidence", "置信度阈值校准", (sys.executable, "scripts/calibrate_confidence.py"), "需要知识库、Milvus 和重排上游"),
    "flywheel": JobSpec("flywheel", "反馈问题标准化与归并", (sys.executable, "scripts/flywheel_pipeline.py"), "需要 MySQL 和聊天上游"),
    "eval-flywheel": JobSpec("eval-flywheel", "评估流水线并记录趋势", (sys.executable, "scripts/eval_flywheel.py"), "需要 MySQL、知识库和模型上游"),
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
