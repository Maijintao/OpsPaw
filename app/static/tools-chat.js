(() => {
  const json = (url,body) => window.opspaw.json(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function toolLabel(row,name){const tag=document.createElement('span');tag.className='tool-label';tag.textContent='已调用：'+name;row.append(tag);}
  document.addEventListener('opspaw:event',({detail:{data,row}})=>{
    if(data.event==='tool') toolLabel(row,data.name);
    if(data.event==='interrupt' && data.kind==='confirm_ticket'){
      const card=document.createElement('div');card.className='operation';const text=document.createElement('div');text.textContent='确认创建工单：'+(data.preview.description||'')+'（'+(data.preview.ticket_type||'')+'）';card.append(text);
      for(const [title,confirmed] of [['确认提交',true],['取消',false]]){const button=document.createElement('button');button.className='btn';button.textContent=title;button.onclick=()=>{card.querySelectorAll('button').forEach(b=>b.disabled=true);window.opspaw.resume({confirmed});};card.append(button);}row.append(card);
    }
    if(data.event==='actions') for(const action of data.items||[]){
      if(action.type!=='create_change')continue;
      const form=document.createElement('form');form.className='operation';const label=document.createElement('label');label.textContent='变更类型';const select=document.createElement('select');select.required=true;label.append(select);form.append(label);
      window.opspaw.json('/openapi.json').then(schema=>{for(const value of schema.components.schemas.CreateChangeRequest.properties.change_type.enum){const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option);}}).catch(error=>window.opspaw.status(error.message));
      const button=document.createElement('button');button.className='btn';button.textContent='确认提交变更申请';form.append(button);
      form.onsubmit=async event=>{event.preventDefault();button.disabled=true;try{const result=await json('/api/actions/create-change',{conversation_id:window.opspaw.conversationId,service_id:action.draft.service_id,change_type:select.value});button.textContent='已提交 '+result.ticket_no;}catch(error){window.opspaw.status(error.message);button.disabled=false;}};row.append(form);
    }
  });
  document.addEventListener('opspaw:history',({detail:{item,row}})=>{for(const name of item.tools||[])toolLabel(row,name);});
  const link=document.createElement('a');link.className='btn';link.href='/tools';link.textContent='工具清单';document.getElementById('feature-nav').append(link);
})();
