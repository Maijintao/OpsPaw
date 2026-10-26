# 当前分支部署说明

本文说明 `mjt` 分支现有功能的部署方式。完整 RAG 模块尚未合入；不要把知识库、RAG 作业或问题池统计当作已部署能力。

## 环境准备

需要 Python 3.12 及以上、uv、可访问的异步 MySQL，或用于本机开发的 SQLite。复制 `.env.example` 为 `.env`，至少填写：

- `CHAT_MODEL`、`CHAT_BASE_URL`、`CHAT_API_KEY`
- `DATABASE_URL`
- `CHECKPOINTER_DB_PATH`

根据实际部署需要再填写 MCP、Grafana 和修复连接器地址。Langfuse 仅用于可选的模型调用追踪和 token 报告；没有配置时不影响对话。

安装并启动：

```bash
uv sync --locked
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

应用启动时创建当前模型所需的数据库表。MySQL 手工建表参考 `sql/targets-ddl.sql` 和 `sql/repair-ddl.sql`；诊断、告警、事故和设置表由应用按当前模型创建。数据库凭据、Langfuse 密钥和上游请求头只放在部署环境，不提交到仓库。

## 可选服务

- 演示 MCP 服务：`bash scripts/start_mcp.sh`。脚本退出时会清理它启动的进程。
- Langfuse：配置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`，在 `/admin` 运行“生成意图 token 用量报告”，再到 `/observability` 查看。报告未生成时页面显示未生成，不用旧文件代替。
- 主题分类器：只有准备好 `data/ch10/onnx/` 中的模型、tokenizer 和阈值文件后，才运行 `uv run python -m scripts.ch10.serve`。模型和训练产物不随仓库提交；没有文件时服务不会伪装成在线。
- 修复连接器：平台管理员先在平台接入页配置可访问的 OpsPaw 地址，再按页面生成的一次性安装命令部署。连接器只主动连接 OpsPaw；写操作受计划、审批和审计流程约束。

## 页面和状态

`/platforms` 管理数据源和查看监控；`/diagnosis?id=任务号` 查看诊断证据；`/repair/任务号` 查看修复回执；`/alerts` 管理告警与巡检；`/incidents` 查看事故；`/admin` 查看配置状态、作业与保留策略；`/topics` 查看主题目录和可用分类器状态。

RAG 问题池及分类分布依赖另一模块，当前分支没有这些表和数据。诊断回放也只衡量离线样例的确定性阶段，不等同真实模型或生产环境效果。
