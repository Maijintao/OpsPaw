(() => {
  const $ = id => document.getElementById(id), el = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; };
  async function api(url, options) { const r=await fetch(url,options); const d=await r.json(); if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'请求失败'); return d; }
  async function load() {
    $('items').setAttribute('aria-busy','true');
    try {
      const [data, targets]=await Promise.all([api('/api/sentinels'),api('/api/targets')]);
      const old=$('target').value; $('target').replaceChildren(new Option('使用已启用平台',''));
      for(const t of targets.targets||[]) $('target').append(new Option(t.name,t.id)); $('target').value=old;
      $('items').replaceChildren();
      for(const x of data.items||[]) {
        const card=el('article',undefined,'platform'); card.append(el('h3',x.name+' · '+(x.enabled?'启用':'停用')));
        card.append(el('p','平台：'+(targets.targets||[]).find(t=>t.id===x.target_id)?.name+' · 服务：'+(x.services||[]).join('、')||'未指定'));
        if(x.token) { const hook=el('p','Webhook：'+location.origin+'/api/sentinels/hook/'+x.token,'health'); card.append(hook); }
        const acts=el('div',undefined,'actions');
        const run=el('button','立即检查','btn'); run.onclick=async()=>{run.disabled=true;try{await api('/api/sentinels/'+x.id+'/test',{method:'POST'});await load();}catch(e){$('status').textContent=e.message;}finally{run.disabled=false;}};
        const toggle=el('button',x.enabled?'停用':'启用','btn'); toggle.onclick=async()=>{try{await api('/api/sentinels/'+x.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!x.enabled})});await load();}catch(e){$('status').textContent=e.message;}};
        const del=el('button','删除','btn'); del.onclick=async()=>{if(!confirm('删除这条接入？'))return;try{await api('/api/sentinels/'+x.id,{method:'DELETE'});await load();}catch(e){$('status').textContent=e.message;}};
        acts.append(run,toggle,del); card.append(acts);
        const list=el('div'); for(const ev of x.events||[]) list.append(el('p',`${ev.at||''} · ${ev.trigger||''} · ${ev.service||'未定位'}${ev.skipped?' · '+ev.skipped:''}${ev.diagnosis_id?' · 诊断 '+ev.diagnosis_id:''}`,'health'));
        if(!(x.events||[]).length)list.append(el('p','还没有触发记录。','empty')); card.append(list); $('items').append(card);
      }
      if(!(data.items||[]).length)$('items').append(el('p','还没有告警接入。','empty'));
      $('status').textContent='已读取 '+(data.items||[]).length+' 条接入。';
    } catch(e) { $('status').textContent=e.message; }
    finally {$('items').removeAttribute('aria-busy');}
  }
  $('form').onsubmit=async e=>{e.preventDefault();$('save').disabled=true;$('status').textContent='正在保存…';try{
    const triggers={schedule:{enabled:$('schedule').checked,mode:'interval',interval_min:$('interval').value?Number($('interval').value):null},webhook:{enabled:$('webhook').checked},threshold:{enabled:$('threshold').checked,metric:$('metric').value,op:$('op').value,value:$('value').value?Number($('value').value):null}};
    const body={name:$('name').value.trim(),target_id:$('target').value?Number($('target').value):null,services:$('service').value.trim()?[$('service').value.trim()]:[],triggers,action:$('action').value,forward_url:$('forward-url').value.trim()};
    await api('/api/sentinels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});e.target.reset();$('webhook').checked=true;await load();
  }catch(err){$('status').textContent=err.message;}finally{$('save').disabled=false;}};
  $('refresh').onclick=load; load();
})();
