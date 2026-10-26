(() => {
  const $ = id => document.getElementById(id);
  let platforms = [], monitorRequest = 0;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  async function api(path, options) { const r = await fetch(path, options); const d = await r.json(); if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail || d)); return d; }
  async function busy(button, status, fn) { button.disabled = true; status.textContent = '正在处理，请稍候…'; status.classList.remove('error'); try { await fn(); } catch (e) { status.textContent = e.message; status.classList.add('error'); } finally { button.disabled = false; } }
  function sourceRow(src = {}) {
    const row = el('div', undefined, 'source'), type = el('select'); type.setAttribute('aria-label', '数据源类型');
    for (const name of ['prometheus','jaeger','loki','mcp']) { const op = el('option', name); op.value = name; type.append(op); }
    type.value = src.type || 'prometheus'; const url = el('input'); url.value = src.url || ''; url.required = true; url.placeholder = 'https://observability.example.com'; url.setAttribute('aria-label', '数据源地址');
    const headers = el('textarea'); headers.value = src.headers ? JSON.stringify(src.headers, null, 2) : ''; headers.placeholder = '请求头 JSON（可选）'; headers.setAttribute('aria-label', '鉴权请求头 JSON');
    const remove = el('button', '移除', 'btn'); remove.type = 'button'; remove.onclick = () => row.remove(); row.append(type, url, headers, remove); $('sources').append(row);
  }
  function reset() { $('platform-form').reset(); $('target-id').value = ''; $('sources').replaceChildren(); sourceRow(); }
  function edit(p) { $('target-id').value = p.id; $('target-name').value = p.name; $('target-cluster').value = p.cluster || ''; $('sources').replaceChildren(); p.sources.forEach(sourceRow); $('editor').open = true; $('editor').scrollIntoView({behavior:'smooth'}); $('target-name').focus(); }
  async function load(fresh = false) {
    $('platform-list').setAttribute('aria-busy', 'true');
    try {
      const data = await api('/api/targets' + (fresh ? '?fresh=true' : '')); platforms = data.targets; $('platform-list').replaceChildren();
      for (const p of platforms) {
        const card = el('article', undefined, 'platform'); card.append(el('h3', p.name + (p.cluster ? ' · ' + p.cluster : '')));
        for (const h of p.health || []) card.append(el('div', `${h.type} · ${h.url} · ${h.online ? '在线' : '不可用'} · ${h.detail}`, 'health' + (h.online ? '' : ' error')));
        const actions = el('div', undefined, 'actions'), editBtn = el('button', '编辑', 'btn'), del = el('button', '删除接入', 'btn'); editBtn.onclick = () => edit(p);
        del.onclick = () => { if (confirm(`删除「${p.name}」的接入配置？`)) busy(del, $('platform-status'), async () => { await api('/api/targets/' + p.id, {method:'DELETE'}); await load(); }); };
        const monitor = el('button', '查看监控', 'btn'); monitor.onclick = async () => { $('monitor-target').value = p.id; await services(); await metrics(); };
        actions.append(editBtn, monitor, del); card.append(actions); $('platform-list').append(card);
      }
      if (!platforms.length) $('platform-list').append(el('p', '还没有接入平台，请在下方添加。', 'empty'));
      const old = $('monitor-target').value; $('monitor-target').replaceChildren(new Option('请选择平台', '')); platforms.forEach(p => $('monitor-target').append(new Option(p.name, p.id))); $('monitor-target').value = old;
      $('platform-status').textContent = `已接入 ${platforms.length} 个平台。`;
    } finally { $('platform-list').removeAttribute('aria-busy'); }
  }
  async function services() {
    const ticket = ++monitorRequest; $('metric-cards').replaceChildren(); $('dashboard').hidden = true; $('monitor-service').replaceChildren(new Option('全部服务',''));
    if (!$('monitor-target').value) return;
    $('monitor-status').textContent = '正在发现服务…';
    try { const d = await api('/api/grafana/services?target_id=' + $('monitor-target').value); if (ticket !== monitorRequest) return; d.services.forEach(s => $('monitor-service').append(new Option(s,s))); $('monitor-status').textContent = `发现 ${d.services.length} 个服务。`; }
    catch(e) { if (ticket === monitorRequest) $('monitor-status').textContent = e.message; }
  }
  async function metrics() {
    const target = $('monitor-target').value; if (!target) { $('monitor-status').textContent = '请先选择平台。'; return; }
    const ticket = ++monitorRequest;
    await busy($('load-monitor'), $('monitor-status'), async () => {
      const params = new URLSearchParams({target_id:target, service:$('monitor-service').value, minutes:$('monitor-minutes').value});
      const d = await api('/api/grafana/series?' + params); if (ticket !== monitorRequest) return;
      $('metric-cards').replaceChildren();
      for (const c of d.cards) {
        const card = el('article', undefined, 'metric'); card.append(el('h3', c.title), el('p', c.desc));
        const rows = c.series.map(s => ({...s, points:s.points.filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]))})).filter(s => s.points.length);
        if (!rows.length) card.append(el('p','此时间范围没有可用数据。','empty'));
        else {
          const all = rows.flatMap(s=>s.points), xs=all.map(p=>p[0]), ys=all.map(p=>p[1]), minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox','0 0 600 150'); svg.setAttribute('role','img'); svg.setAttribute('aria-label',c.title + '趋势，数值见下方图例');
          const list=el('ul'), colors=['var(--ink)','var(--coral)','var(--muted)','var(--fur)'];
          rows.forEach((s,i)=>{ const line=document.createElementNS(svg.namespaceURI,'polyline'); line.setAttribute('points',s.points.map(p=>`${10+(p[0]-minX)/(maxX-minX || 1)*580},${140-(p[1]-minY)/(maxY-minY || 1)*130}`).join(' ')); line.setAttribute('fill','none'); line.setAttribute('stroke',colors[i%colors.length]); line.setAttribute('stroke-width',s.name===d.service?'4':'2'); svg.append(line); const item=el('li',`${s.name} · 最新 ${s.points.at(-1)[1].toLocaleString()} ${c.unit}`); list.append(item); });
          card.append(svg,el('p',`${new Date(minX*1000).toLocaleTimeString()} — ${new Date(maxX*1000).toLocaleTimeString()} · 数值范围 ${minY.toLocaleString()} — ${maxY.toLocaleString()} ${c.unit}`),list);
        }
        $('metric-cards').append(card);
      }
      $('monitor-status').textContent = `${d.target} · ${d.service || '全部服务'} · 已更新`;
    });
  }
  $('platform-form').onsubmit = e => { e.preventDefault(); busy($('save'),$('form-status'),async()=>{
    const sources=[...$('sources').children].map(row=>{ const [type,url,headers]=row.querySelectorAll('select,input,textarea'); let parsed={}; if(headers.value.trim()) parsed=JSON.parse(headers.value); if(!parsed || Array.isArray(parsed) || typeof parsed!=='object' || Object.values(parsed).some(v=>typeof v!=='string')) throw new Error('请求头应为键值均为字符串的 JSON 对象。'); return {type:type.value,url:url.value.trim(),headers:parsed}; });
    const id=$('target-id').value, body={name:$('target-name').value.trim(),cluster:$('target-cluster').value.trim(),sources};
    await api('/api/targets'+(id?'/'+id:''),{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); reset(); await load(); $('form-status').textContent='保存成功，连接检查结果见上方。';
  }); };
  $('refresh').onclick=()=>busy($('refresh'),$('platform-status'),()=>load(true)); $('reset').onclick=reset; $('add-source').onclick=()=>sourceRow(); $('monitor-target').onchange=services; $('load-monitor').onclick=metrics;
  $('load-dashboard').onclick=()=>busy($('load-dashboard'),$('monitor-status'),async()=>{ const id=$('monitor-target').value; if(!id) throw new Error('请先选择平台。'); const d=await api('/api/grafana/dashboard?target_id='+id); const url=new URL('/d/'+encodeURIComponent(d.uid),d.grafana_url); url.searchParams.set('kiosk',''); $('dashboard').src=url.href; $('dashboard').hidden=false; $('monitor-status').textContent='Grafana 面板已打开。'; });
  reset(); busy($('refresh'),$('platform-status'),()=>load());
})();
