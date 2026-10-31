"""建库材料清单:哪些文件进知识库、各自算什么内容类型。

一处定义、三处共用——离线建库 CLI(scripts/build_kb.py)、切块预览(scripts/show_kb.py)、
录入页(/kb)。三边各写一份就会出现「预览看到三份、实际入库四份」这种对不上的账。
"""
import pathlib

KB_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "kb"

# 文件 → content_type
SOURCE_TYPES: dict[str, str] = {
    "ops-faq.md": "faq",              # 值班高频问答
    "oncall-handbook.md": "policy",   # 值班制度与变更管理(规则型内容)
    "service-catalog.md": "spec",     # 服务目录(CMDB):服务名/编号精确词召回靠它
    "runbook-cpu-high.md": "manual",        # CPU 使用率过高 runbook
    "runbook-memory-high.md": "manual",     # 内存使用率过高 runbook
    "runbook-service-down.md": "manual",    # 服务不可用 runbook
    "runbook-slow-response.md": "manual",   # 响应时间过长 runbook
    "runbook-disk-high.md": "manual",       # 磁盘使用率过高 runbook
}

# 录入页允许选的内容类型:政策/手册这类没有天然问题,questions 落章节标题
CONTENT_TYPES: tuple[str, ...] = ("faq", "policy", "manual", "spec")
CONTENT_TYPE_DESC: dict[str, str] = {
    "faq": "值班 FAQ:questions 填真实问法",
    "policy": "制度条款:questions 填章节标题、category 填上级路径",
    "manual": "runbook 手册:同政策,按标题层级切",
    "spec": "服务目录:含服务名与编号,精确词召回靠它",
}
