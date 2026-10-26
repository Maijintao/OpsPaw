# 主题分类推理服务

`serve.py` 提供独立 ONNX 推理服务。需要先由分类数据流水线生成 `data/ch10/onnx/model.onnx`、`tokenizer.json` 和 `threshold.json`；这些模型文件不放入仓库。当前没有接入 RAG 问题池，因此本目录不含问题池批量分类、分类结果落库和分布统计。

启动：`uv run python -m scripts.ch10.serve`
