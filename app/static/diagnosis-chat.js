(() => {
  function card(row, action) {
    if (!action.diagnosis_id || [...row.querySelectorAll('[data-diagnosis-id]')].some(c => c.dataset.diagnosisId === action.diagnosis_id)) return;
    const box=document.createElement('section'); box.className='operation diagnosis-card'; box.dataset.diagnosisId=action.diagnosis_id;
    const title=document.createElement('strong'); title.textContent='根因调查 · '+(action.service || '扫描平台');
    const progress=document.createElement('p'); progress.setAttribute('role','status'); progress.textContent='正在读取诊断任务…';
    const detail=document.createElement('a'); detail.className='btn'; detail.href='/diagnosis?id='+encodeURIComponent(action.diagnosis_id); detail.textContent='查看完整证据';
    const result=document.createElement('div'); box.append(title,progress,result,detail); row.append(box);
    async function poll() {
      if (!box.isConnected) return;
      try {
        const d=await window.opspaw.json('/api/diagnosis/'+encodeURIComponent(action.diagnosis_id));
        const events=d.events || []; progress.textContent=(d.status==='running'?'调查中 · ':'')+(events.at(-1)?.detail || d.stage || d.status);
        if(d.status==='running') { setTimeout(poll,2000); return; }
        result.replaceChildren(); const r=d.result || {}, summary=document.createElement('p'); summary.textContent=r.narrative?.fault || r.confidence_note || (d.status==='failed'?'诊断未完成，请查看详情。':r.verdict || '尚无结论'); result.append(summary);
        if(r.root_cause_service){const root=document.createElement('p');root.textContent='候选根因：'+r.root_cause_service;result.append(root);}
        if(r.missing?.length){const gaps=document.createElement('p');gaps.textContent='尚未确认：'+r.missing.join('；');result.append(gaps);}
        if(r.budget){const budget=document.createElement('p');budget.textContent='已用查询 '+r.budget.used+' 次'+(r.budget.exhausted?'，查询预算已用完':'');result.append(budget);}
        if(['root_cause','critical_failure'].includes(r.verdict)){
          const state=document.createElement('p');state.textContent='结论确认：'+({confirmed:'已确认',rejected:'已否认',pending:'待确认'}[d.confirmation] || '待确认');result.append(state);
          for(const [text,value] of [['确认根因',true],['否认根因',false]]){
            const button=document.createElement('button');button.className='btn';button.textContent=text;button.onclick=async()=>{const buttons=result.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);try{await window.opspaw.json('/api/diagnosis/'+encodeURIComponent(action.diagnosis_id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:value})});await poll();}catch(e){progress.textContent=e.message;buttons.forEach(b=>b.disabled=false);}};result.append(button);
          }
        }
      } catch(e) { progress.textContent='读取诊断失败：'+e.message; const retry=document.createElement('button');retry.className='btn';retry.textContent='重试';retry.onclick=()=>{retry.remove();poll();};result.append(retry); }
    }
    poll();
  }
  document.addEventListener('opspaw:event',({detail:{data,row}})=>{if(data.event==='actions')for(const action of data.items || [])if(action.type==='diagnosis_card')card(row,action);});
  document.addEventListener('opspaw:history',({detail:{item,row}})=>{for(const action of item.actions || [])if(action.type==='diagnosis_card')card(row,action);});
})();
