from app.kb.documents import Chunk, build_chunks


def test_policy_maps_heading_and_parent_path():
    md = "# 值班规范\n\n## 告警响应时效\n\nP0 告警 5 分钟内响应,先止血后排因。"
    chunks = build_chunks(md, content_type="policy")
    c = [c for c in chunks if c.questions == "告警响应时效"][0]
    assert c.category == "值班规范"
    assert c.section_path == "值班规范 / 告警响应时效"
    assert "P0" in c.answer
    assert c.content_type == "policy"
    assert c.is_key_clause == 1  # 含「告警」关键条款


def test_table_section_splits_by_rows_with_header():
    rows = "\n".join(f"| 服务{i} | {i} |" for i in range(1, 15))
    md = f"# 服务清单\n\n## 列表\n\n| 服务 | 实例数 |\n| --- | --- |\n{rows}"
    chunks = build_chunks(md, content_type="manual", table_max_rows=5)
    price = [c for c in chunks if c.questions == "列表"]
    assert len(price) >= 2  # 14 行按 5 切成 >=3 块
    for c in price:
        assert c.answer.startswith("| 服务 | 实例数 |")  # 每块复制表头


def test_returns_chunk_dataclass():
    chunks = build_chunks("# A\n\n## B\n\n正文。", content_type="faq")
    assert isinstance(chunks[0], Chunk)
