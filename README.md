# OpsPaw 值班猫

面向运维值班的对话应用。当前分支已接入会话管理、意图分流、上下文摘要、工具执行、工单确认和 MCP 工具；其他模块按后续计划接入。

## 运行

需要 Python 3.12 及以上和 uv。安装依赖后，首次运行复制 `.env.example` 为 `.env`，填写模型和数据库配置；已有配置不要覆盖。

```bash
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

应用启动时创建本阶段需要的基础表。访问 `/` 使用对话，`/tools` 查看实际可用工具，`/docs` 查看接口。需要演示监控与日志工具时另行运行 `bash scripts/start_mcp.sh`。

`DATABASE_URL` 支持 MySQL 异步连接，也可配置 `sqlite+aiosqlite:///data/opspaw.sqlite` 做本地验证。外部服务连接信息与真实凭据只保留在部署环境。

## 当前能力

- 流式对话、会话列表、历史消息回载与删除。
- 意图识别、指代消解、上下文窗口、后台摘要和标题。
- 工具注册、参数校验、只读重试、写操作确认和调用审计。
- 服务目录查询、服务快照、工单及变更申请。
- 多个 MCP 服务的动态工具发现与结果格式化。

## 验证

测试使用独立 SQLite 和模型替身，不连接业务库或真实模型服务。

```bash
uv run pytest -q
```

本机使用 Anaconda Python 且在 readline 初始化时崩溃时，可加 `-p no:capture` 关闭相关测试输出捕获插件。
