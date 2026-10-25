"""验证流式对话落库及工单确认流程，模型和 MCP 均使用替身。"""
import json

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import func, select

import app.main as main
from app.core import coref, intent, summarizer, titler
from app.db import base, repository
from app.db.models import Ticket, ToolAuditLog
from app.graph import nodes, runtime
from app.tools import registry


def events(text):
    return [json.loads(line[5:].strip()) for line in text.splitlines()
            if line.startswith('data:') and line[5:].strip() != '[DONE]']

async def prepare(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'engine', base.engine)
    monkeypatch.setattr(runtime.settings, 'checkpointer_db_path', str(tmp_path / 'checkpoint.sqlite'))

    async def classify(*args):
        return {'intent': '服务状态', 'confidence': 1.0}

    async def resolve(query, history):
        return query

    async def no_background(*args):
        return None

    monkeypatch.setattr(intent, 'classify', classify)
    monkeypatch.setattr(coref, 'resolve', resolve)
    monkeypatch.setattr(summarizer, 'maybe_schedule_summary', no_background)
    monkeypatch.setattr(titler, 'maybe_schedule_title', no_background)


async def test_streaming_reply_is_saved_and_can_be_reloaded(db_session_factory, monkeypatch, tmp_path):
    await prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, 'get_chat_model', lambda **kwargs: FakeListChatModel(responses=['已收到值班问题']))

    async def no_tools():
        return []

    monkeypatch.setattr(registry, 'get_all_specs', no_tools)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/chat', json={'user_id':'tester','message':'查一下服务状态'})
            assert response.status_code == 200
            stream = events(response.text)
            assert ''.join(event.get('delta','') for event in stream) == '已收到值班问题'
            cid = next(event['conversation_id'] for event in stream if event.get('event') == 'done')
            history = await client.get(f'/api/conversations/{cid}/messages')
            assert [(m['role'],m['content']) for m in history.json()['items']] == [('user','查一下服务状态'),('assistant','已收到值班问题')]
            assert (await client.get('/')).status_code == 200


class TicketModel(BaseChatModel):
    @property
    def _llm_type(self):
        return 'ticket-flow-test'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        message = (AIMessage(content='操作流程结束') if results else
                   AIMessage(content='', tool_calls=[{'name':'create_ticket','args':{'description':'服务不可用，需要人工排查','ticket_type':'故障'},'id':'ticket-call','type':'tool_call'}]))
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.parametrize('confirmed', [True, False])
async def test_ticket_waits_for_user_confirmation(db_session_factory, monkeypatch, tmp_path, confirmed):
    await prepare(monkeypatch, tmp_path)
    registry.scan_builtin()
    monkeypatch.setattr(nodes, 'get_chat_model', lambda **kwargs: TicketModel())

    async def ticket_specs():
        return [registry.get_builtin_spec('create_ticket')]

    monkeypatch.setattr(registry, 'get_all_specs', ticket_specs)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/chat', json={'user_id':'tester','message':'帮我创建故障工单'})
            preview = next(e for e in events(response.text) if e.get('event') == 'interrupt')
            assert preview['kind'] == 'confirm_ticket'
            snap = await runtime.get_graph().aget_state({'configurable': {'thread_id': str(preview['conversation_id'])}})
            assert snap.next == ('agent_tools',)
            async with db_session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Ticket)) == 0
            response = await client.post('/api/actions/resume', json={'conversation_id':preview['conversation_id'],'confirmed':confirmed})
            assert any(e.get('event') == 'done' for e in events(response.text)), response.text
            async with db_session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(Ticket)) == int(confirmed)
                audit = (await session.execute(select(ToolAuditLog))).scalar_one()
                assert audit.status == ('成功' if confirmed else '权限拒绝')
            history = await repository.list_dialog_messages(preview['conversation_id'])
            assert history[-1].meta['tools'] == ['create_ticket']
