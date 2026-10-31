import pytest

from scripts import bench_precheck


async def _finish_precheck(monkeypatch, flags, restored):
    monkeypatch.setattr(bench_precheck, "read_flags", lambda: flags)
    monkeypatch.setattr(bench_precheck, "restore", lambda: restored.append(True))
    monkeypatch.setattr(
        bench_precheck._runner, "_resolve_sources",
        lambda service: _resolved_sources(),
    )
    monkeypatch.setattr(bench_precheck.collect, "list_services", _services)
    monkeypatch.setattr(bench_precheck.collect, "_fetch_spans_once", _spans)
    monkeypatch.setattr(bench_precheck.time, "time", lambda: 1000.0)


async def _resolved_sources():
    return ({"url": "http://prom"}, {"url": "http://jaeger"}, None)


async def _services(*args, **kwargs):
    return set()


async def _spans(*args, **kwargs):
    return [], "ok", None


@pytest.mark.asyncio
async def test_precheck_is_read_only_by_default(monkeypatch, capsys):
    restored = []
    await _finish_precheck(monkeypatch, [{"flag": "x", "current": "on"}], restored)

    await bench_precheck.main()

    assert restored == []
    assert "只读检查未修改靶子" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_precheck_restores_only_when_explicit(monkeypatch):
    restored = []
    await _finish_precheck(monkeypatch, [{"flag": "x", "current": "on"}], restored)

    await bench_precheck.main(restore_dirty=True)

    assert restored == [True]


@pytest.mark.asyncio
async def test_precheck_does_not_restore_clean_target(monkeypatch):
    restored = []
    await _finish_precheck(monkeypatch, [{"flag": "x", "current": "off"}], restored)

    await bench_precheck.main(restore_dirty=True)

    assert restored == []
