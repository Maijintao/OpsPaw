# OpsPaw 运行与部署

开发时直接运行应用，连接已有 MySQL、Milvus 和观测服务。以下命令在项目根目录执行，不创建或启动容器。

## 安装与配置

需要 Python 3.12 及以上和 uv：

```bash
uv sync --locked
```

首次运行且没有 `.env` 时复制模板，已有配置不要覆盖：

```bash
cp .env.example .env
```

| 配置 | 说明 |
| --- | --- |
| `CHAT_BASE_URL`、`CHAT_MODEL`、`CHAT_API_KEY` | 支持工具调用的聊天模型 |
| `EMBED_BASE_URL`、`EMBED_MODEL`、`EMBED_API_KEY` | 嵌入服务 |
| `RERANK_BASE_URL`、`RERANK_MODEL`、`RERANK_API_KEY` | 支持项目 `/rerank` 接口的重排服务 |
| `DATABASE_URL` | MySQL 业务库异步连接地址 |
| `TEST_DATABASE_URL` | 独立测试库，仅数据库测试需要 |
| `MILVUS_URI` | Milvus Standalone 地址 |
| `ACCESS_USER`、`ACCESS_PASS` | 对外访问账号和密码 |

可选 Langfuse 配置为 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`。真实凭据只放在部署环境中。

## 数据库与知识库

在现有 MySQL 服务创建业务库和用户。基础表按顺序应用下列 SQL，已有库只执行尚未应用的升级：

1. `sql/ch02-ddl.sql`
2. `sql/ch03-ddl.sql`
3. `sql/ch03-staging-review.sql`
4. `sql/ch04-ddl.sql`
5. `sql/ch07-ddl.sql`
6. `sql/ch08-ddl.sql`
7. `sql/ch09-ddl.sql`
8. `sql/ch10-ddl.sql`

平台、诊断、修复和事故等扩展表还由应用启动过程创建。已有表的字段升级需结合 `sql/` 的对应脚本核对，不要清空业务数据。

```bash
make kb-build
make kb-vectorize
```

已有知识块、只需更换向量库时，使用 `make kb-vectorize-all` 重新向量化。

## 启动

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000` 和 `/admin`。需要演示工具时，分别运行：

```bash
uv run python mcp_servers/monitor_server.py
uv run python mcp_servers/log_server.py
```

真实调查需要在管理界面接入平台的 Prometheus、Jaeger，以及可选的 Loki、Grafana。

## 同步到已有服务器

```bash
./scripts/sync_server.sh
./scripts/sync_server.sh --restart
```

第一条同步源码，第二条额外重启已配置的 `opspaw-server`。执行前核对脚本中的主机、密钥和部署目录是否适用于当前环境。服务器的 `.env`、数据库和运行数据单独管理。

## 检查

打开首页和管理页，检查数据源状态；录入运维文档并检索。诊断结果应与所选平台和调查时间对应，并显示数据质量和结论依据。修复按页面确认与授权流程执行。

产品要求见 [docs/product-acceptance.md](docs/product-acceptance.md)。
