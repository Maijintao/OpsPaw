from app.kb import chunking


def test_split_sections_keeps_header_path():
    md = "# 值班手册\n\n## 告警阈值\n\nCPU 持续 5 分钟超过 80% 触发告警。\n\n## 变更窗口\n\n变更窗口为每天 02:00-04:00。"
    secs = chunking.split_sections(md)
    freight = [d for d in secs if d.metadata.get("h2") == "变更窗口"]
    assert freight, "应切出「变更窗口」小节"
    assert freight[0].metadata.get("h1") == "值班手册"
    assert "02:00-04:00" in freight[0].page_content


def test_recursive_split_breaks_oversized():
    text = "句子内容。" * 60  # 300 字
    parts = chunking.recursive_split(text, chunk_size=50, chunk_overlap=0)
    assert len(parts) > 1
    assert max(len(p) for p in parts) <= 50
