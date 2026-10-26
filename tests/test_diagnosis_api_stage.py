import asyncio
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from app.main import app
from app.db import repository
from app.diagnosis import runner, detect
from app.graph import nodes
from app.tools import registry, service_catalog

async def test_empty_service_runs_scan_and_archives(db_clean, monkeypatch):
    async def no_data(**kwargs):
        assert kwargs.get("platform") == "platform-a"
        return {"status":"no_data","note":"没有可用观测数据","anchors":[]}
    monkeypatch.setattr(detect,"scan_platforms",no_data)
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as c:
        response=await c.post('/api/diagnosis',json={"service":"","platform":"platform-a"})
        assert response.status_code==200
        did=response.json()['diagnosis_id']
        for _ in range(100):
            data=(await c.get('/api/diagnosis/'+did)).json()
            if data['status']!='running' and await repository.get_diagnosis_run(did): break
            await asyncio.sleep(.01)
        assert data['status']=='done'
        assert data['result']['verdict']=='no_data'
        runner._RUNS.pop(did)
        assert (await c.get('/api/diagnosis/'+did)).json()['result']['verdict']=='no_data'
        assert (await c.post('/api/diagnosis/'+did+'/confirm',json={"confirmed":True})).status_code==409
        assert (await c.get('/api/diagnosis/missing-task')).status_code==404

async def test_diagnosis_tool_keeps_conversation_and_card(db_clean,monkeypatch):
    registry.scan_builtin();spec=registry.get_builtin_spec('start_diagnosis')
    async def specs(): return [spec]
    monkeypatch.setattr(registry,'get_all_specs',specs)
    async def targets(): return [SimpleNamespace(name='platform-a',enabled=1)]
    monkeypatch.setattr(repository,'list_targets',targets)
    seen=[]
    def start(service,symptom,**kwargs):seen.append((service,kwargs));return 'diag-test'
    monkeypatch.setattr(runner,'start',start)
    call={'name':'start_diagnosis','args':{'service':'platform-a'},'id':'diag-call','type':'tool_call'}
    out=await nodes.agent_tools({'messages':[AIMessage(content='',tool_calls=[call])],'conversation_id':42})
    assert seen==[('',{'conversation_id':42,'platform':'platform-a'})]
    assert out['suggested_actions']==[{'type':'diagnosis_card','diagnosis_id':'diag-test','service':''}]
    assert 'conversation_id' not in spec.json_schema.get('properties',{})
