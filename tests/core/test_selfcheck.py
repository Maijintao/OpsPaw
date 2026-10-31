import pytest

from app.core import selfcheck


@pytest.mark.asyncio
async def test_check_sufficient_parses_flat(monkeypatch):
    class Fake:
        useful = False
        reason = "证据只讲变更窗口,未涉及告警阈值"

    async def fake_invoke(_):
        return Fake()

    class Chain:
        def __or__(self, o):
            return self

        async def ainvoke(self, _):
            return Fake()

    monkeypatch.setattr("app.core.selfcheck._chain", lambda: Chain())
    out = await selfcheck.check_sufficient("告警阈值是多少", ["变更窗口是 02:00-04:00"])
    assert out == {"useful": False, "reason": "证据只讲变更窗口,未涉及告警阈值"}
