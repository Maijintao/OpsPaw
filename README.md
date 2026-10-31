# OpsPaw 值班猫

OpsPaw 是面向运维值班的故障调查与处理应用，使用 Python、FastAPI 和 LangGraph。用户可以通过对话查询服务、检索运维知识、发起诊断，并在确认结论和授权操作后进入修复流程。

## 功能

| 模块 | 当前实现 |
| --- | --- |
| 对话与工具 | 流式对话、意图分流、服务发现、内置与 MCP 工具、任务确认与恢复 |
| 运维知识库 | 文档切块、MySQL/Milvus 双写、向量与 BM25 检索、结果融合与重排 |
| 知识维护 | 问题收集、知识挖掘、查重、人工审核与评估 |
| 故障归因 | Prometheus/Jaeger 采集、历史比较、调用关系、候选排序、OpsAgent 分析与解释 |
| 事故与告警 | 外部告警接入、定时巡检、事故记录与事件时间线 |
| 修复 | 根因确认、修复计划、连接器、操作授权、执行记录和结果检查 |
| 管理界面 | 平台与数据源、Grafana 面板、知识库、模型配置、成本与数据保留 |

## 运行

需要 Python 3.12 及以上、uv、MySQL 和模型服务。知识检索需要 Milvus，BM25 和完整检索评估使用 Milvus Standalone。开发时连接已有基础服务，直接运行 Python 进程。

```bash
uv sync --locked
cp .env.example .env
```

填写模型、数据库和 Milvus 配置，准备数据库及知识库后启动：

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

完整步骤见 [DEPLOY.md](DEPLOY.md)。已有 `.env` 不要被模板覆盖。

## 数据接入

管理界面支持登记平台及获授权的数据源。当前诊断主流程使用 Prometheus 与 Jaeger，Loki 日志是可选补充，Grafana 用于面板展示。

`mcp_servers/` 提供演示用监控和日志工具，按需单独启动；它们与平台接入的真实观测数据是两条不同路径。

## 页面入口

| 地址 | 用途 |
| --- | --- |
| `/`、`/admin` | 对话、管理首页 |
| `/kb`、`/rag-eval` | 知识库、检索评估 |
| `/review`、`/observability` | 知识审核、观测与成本 |
| `/topics`、`/acceptance` | 问题主题、分类器评估 |
| `/docs` | HTTP 接口文档 |

## 目录

| 目录 | 职责 |
| --- | --- |
| `app/api`、`app/schemas` | HTTP 接口与请求结构 |
| `app/core`、`app/graph` | 模型调用、检索、上下文和对话流程 |
| `app/kb`、`data/kb` | 文档处理、知识维护与运维知识示例 |
| `app/db` | 数据模型、数据访问与保留策略 |
| `app/diagnosis`、`app/repair` | 故障调查与修复 |
| `app/static` | 页面和公共样式 |
| `scripts`、`sql`、`deploy` | 建库、评估、同步、数据库与部署配置 |
| `tests`、`docs` | 功能验证与项目说明 |

模块边界见 [docs/module-map.md](docs/module-map.md)，产品要求见 [docs/product-acceptance.md](docs/product-acceptance.md)。

## 使用边界与来源

- 诊断输出需结合证据和实际环境确认；功能存在不代表所有产品要求均已验证。
- 历史评估文件只说明对应版本和数据集的结果，不能代替当前版本或客户环境的成绩。
- `.env`、数据库、日志、模型产物和观测录制文件属于运行环境，不随源码发布。
- 基础对话与知识检索结构参考《AI Agent 智能客服实战》配套实现；归因模块包含适配后的第三方 OpsAgent 代码。相关来源及授权说明随对应代码保留。
