# OpsAgent 集成文件

本目录保留项目已有的第三方 OpsAgent 实现。OpsPaw 通过相邻的 `opsagent_adapter.py` 将指标、日志和链路转换为其特征输入。

当前执行方式为 AD、FT、RCL 各一次 `think`，未启用交叉评审。`utils/public_function.py` 的单次推理已经接入 OpsPaw 配置的 OpenAI 兼容端点；`reflection/ReflectionVector.py` 是本项目补充的本地经验存储接口。

`patterncla.pt` 是特征处理器 `CNNClassifier` 使用的原有形态分类权重，不是语言模型权重。语言模型权重和本地训练输出不随本仓库提交。

运行依赖以项目根目录的 `pyproject.toml` 和 `uv.lock` 为准；本目录原 `requirements.txt` 仅保留上游环境信息。源码和权重的原有第三方权利归其作者所有，本文件不授予额外许可。
