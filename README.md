# OpsPaw

运维值班应用的初始化骨架。

## 启动

需要 Python 3.12 及以上和 uv。

```bash
uv sync
uv run uvicorn app.main:app --reload
```

启动后访问 `http://127.0.0.1:8000/docs`。

## 目录

```text
app/
  main.py
  api/
  core/
  db/
  schemas/
  static/
tests/
scripts/
data/
docs/
sql/
```
