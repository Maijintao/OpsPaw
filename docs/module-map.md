# OpsPaw 模块划分

按当前代码职责划分，供源码整理、交接和维护使用。模块导入顺序不等于历史开发顺序，作者记录以实际完成的工作为准。

## 一、最外层框架

仅包含项目名称、目录、忽略规则、文件格式规则和最小应用入口：

- `.gitignore`、`.gitattributes`。
- 精简项目说明和最小依赖声明。
- `app/`、`api/`、`core/`、`db/`、`schemas/`、`static/` 的目录与空包入口。
- 最小 FastAPI 入口，业务路由、页面、数据库和模型配置随相应模块接入。

现有 `app/main.py` 已挂载完整业务，`pyproject.toml`、`uv.lock` 包含完整依赖，不能直接将当前全文当作空框架。本次不创建替代应用，不改写现有运行入口。

## 二、RAG 与知识库

### 独立文件

| 范围 | 文件 |
| --- | --- |
| 文档处理与存储 | `app/kb/` 全部模块：切块、解析、来源、查重、双写、Milvus、知识挖掘 |
| 检索与生成检查 | `app/core/retrieval.py`、`embeddings.py`、`rerank.py`、`confidence.py`、`selfcheck.py` |
| 问题理解与知识维护 | `app/core/query_understanding.py`、`flywheel.py` |
| HTTP 接口 | `app/api/kb.py`、`rageval.py`、`review.py` |
| 请求结构 | `app/schemas/rageval.py`、`review.py` |
| 页面 | `app/static/kb.html`、`rageval.html`、`review.html` |
| 知识材料 | `data/kb/` 的运维文档 |
| 建库维护 | `scripts/build_kb.py`、`show_kb.py`、`vectorize_kb.py`、`kb_repatch.py`、`mine_knowledge.py`、`flywheel_pipeline.py` |
| 评估 | `scripts/eval_retrieval.py`、`eval_ch04.py`、`validate_eval_ch04.py`、`eval_expand.py`、`eval_mining.py`、`eval_flywheel.py`、`validate_flywheel_samples.py`、`calibrate_confidence.py`、`judge_check.py` |
| 服务检查 | `scripts/smoke_embed.py`、`smoke_rerank.py`、`smoke_milvus_bm25.py` |
| 数据结构 | `sql/ch03-ddl.sql`、`ch03-staging-review.sql`、`ch04-ddl.sql`、`ch09-ddl.sql` 中的知识、评估和审核表 |

### 共享文件

这些文件混有其他功能，交接时应确认具体定义，不能整份算作 RAG：

- `app/config.py`：嵌入、Milvus、重排、检索及回答检查配置。
- `app/core/llm.py`、`prompts.py`：共享模型客户端和相关提示词。
- `app/db/models.py`：`KnowledgeChunk`、`QaExtractionStaging`、`LowConfidenceQuestion`、`ReviewQueue`、`EvalRun`、`FaithCase`。
- `app/db/repository.py`：知识块、候选知识、问题收集、审核和评估结果的读写函数。
- `app/graph/nodes.py`、`state.py`、`routing.py`、`build.py`：检索、证据检查、回答检查及相关状态和分支。
- `app/tools/builtin/faq.py`：对话中的知识检索入口。
- `app/core/jobs.py`、`Makefile`：知识库与评估任务。
- `app/main.py`、`app/static/admin.html`、`admin.js`：相关路由和公共页面外壳。
- `tests/conftest.py`：共享测试准备。

### 相关验证文件

`tests/test_chunking_*.py`、`test_documents.py`、`test_dedup.py`、`test_embeddings.py`、`test_retrieval*.py`、`test_rerank*.py`、`test_kb_*.py`、`test_rageval_api.py`，以及 `tests/tools/test_query_faq_rag.py`。

其他知识维护、问题改写和图节点测试按实际被测函数划分。RAG 依赖共享数据库、模型配置、作业系统和页面外壳，独立文件清单不表示已能脱离应用运行。

## 三、其余应用能力

除上述 RAG 范围外，其余现有业务划入应用主体：

- 对话、意图分流、上下文管理、模型与工具编排。
- 服务发现、平台与数据源配置、Grafana 面板。
- `app/diagnosis/`：检测、采集、调用关系、定位、归因与解释。
- `app/repair/`：连接器、授权、计划、执行与结果检查。
- 事故中心、告警接入、巡检、访问控制和数据保留。
- 主题分类、成本观测、同步与部署，以及对应页面、接口、SQL、脚本和验证。

第三方代码的来源和许可随代码保留，模块归类不改变其作者或授权。
