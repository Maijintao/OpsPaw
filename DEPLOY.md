# OpsPaw 运行与部署

本文说明当前分支的本地运行、RAG 知识库构建和可选服务配置。应用需要 Python 3.12 及以上、uv，以及可访问的 MySQL、Milvus 和模型上游。

## 安装与配置

```bash
uv sync --locked
cp .env.example .env
```

至少填写以下配置，真实凭据不要提交到仓库：

- `CHAT_BASE_URL`、`CHAT_MODEL`、`CHAT_API_KEY`
- `EMBED_BASE_URL`、`EMBED_MODEL`、`EMBED_API_KEY`
- `RERANK_BASE_URL`、`RERANK_MODEL`、`RERANK_API_KEY`
- `DATABASE_URL`
- `MILVUS_URI`
- `CHECKPOINTER_DB_PATH`

根据部署情况继续配置 MCP、Grafana、修复连接器和可选的 Langfuse。

## 数据库与知识库

应用启动时会按当前 ORM 模型创建缺失的数据表。`sql/ch02-ddl.sql`、`sql/ch03-ddl.sql`、`sql/ch03-staging-review.sql`、`sql/ch04-ddl.sql` 和 `sql/ch09-ddl.sql` 可作为建表及升级参考；这些脚本包含 `CREATE TABLE`，不要在已有表上盲目重复执行。

知识库材料位于 `data/kb/`。首次构建：

```bash
make kb-build
make kb-vectorize
```

切换 Milvus 实例后可执行 `make kb-vectorize-all` 重新写入全部已有知识块。修改 Markdown 材料后可执行 `make kb-repatch`，只重嵌发生变化的块。

## 启动

```bash
make dev
```

等价的直接命令：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

主要页面：

- `/`：值班对话
- `/kb`：知识库录入、切块预览、向量化和检索测试
- `/review`：待审知识队列
- `/rag-eval`：RAG 评估结果与忠实度个案
- `/observability`：成本、评估趋势和置信度校准
- `/platforms`：数据源与监控
- `/diagnosis`：诊断证据
- `/repair/{id}`：修复回执
- `/alerts`：告警与巡检
- `/incidents`：事故中心
- `/admin`：配置、后台作业和保留策略
- `/topics`：主题分类

## 可选服务

- 演示 MCP：`bash scripts/start_mcp.sh`。脚本运行期间保持前台，退出时会清理子进程。
- Langfuse：配置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 后生成 token 用量报告。
- 主题分类器：准备模型和 tokenizer 后按 `scripts/ch10/README.md` 启动。
- 修复连接器：从平台接入页生成一次性安装命令；写操作受计划、审批和审计流程约束。

## 检查

```bash
make test
make validate-rag
```

完整的四策略 RAG 评估需要可用的 Milvus、已向量化知识库、嵌入、重排和聊天上游：

```bash
make eval-rag
make calibrate-confidence
```

评估和诊断回放只代表给定样例上的结果，不等同于生产环境准确率。
