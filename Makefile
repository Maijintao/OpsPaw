.PHONY: help dev test kb-preview kb-build kb-vectorize kb-vectorize-all \
	kb-repatch kb-mine eval-retrieval eval-rag validate-rag judge-check \
	calibrate-confidence flywheel eval-flywheel

help:
	@printf '%s\n' \
	  'make dev                 启动开发服务' \
	  'make test                运行测试' \
	  'make kb-preview          查看知识材料与切块结果' \
	  'make kb-build            从 data/kb 构建知识块' \
	  'make kb-vectorize        向量化待处理知识块' \
	  'make kb-vectorize-all    重新向量化全部知识块' \
	  'make kb-repatch          按文档变化补丁式重嵌' \
	  'make kb-mine             从历史对话挖掘候选知识' \
	  'make eval-retrieval      运行检索评估' \
	  'make eval-rag            运行四策略 RAG 评估' \
	  'make calibrate-confidence 校准证据置信度阈值' \
	  'make flywheel            处理反馈问题池' \
	  'make eval-flywheel       运行评估流水线并记录趋势'

dev:
	uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	uv run pytest -v

kb-preview:
	uv run python scripts/show_kb.py

kb-build:
	uv run python scripts/build_kb.py

kb-vectorize:
	uv run python scripts/vectorize_kb.py

kb-vectorize-all:
	uv run python scripts/vectorize_kb.py --all

kb-repatch:
	uv run python scripts/kb_repatch.py

kb-mine:
	uv run python scripts/mine_knowledge.py

eval-retrieval:
	uv run python scripts/eval_retrieval.py

eval-rag:
	uv run python scripts/eval_ch04.py

validate-rag:
	uv run python scripts/validate_eval_ch04.py

judge-check:
	uv run python scripts/judge_check.py

calibrate-confidence:
	uv run python scripts/calibrate_confidence.py

flywheel:
	uv run python scripts/flywheel_pipeline.py

eval-flywheel:
	uv run python scripts/eval_flywheel.py
