(() => {
  const $ = id => document.getElementById(id);
  let conversationId = null, userId = '', busy = false;
  const status = text => { $('status').textContent = text; };
  function message(role, content) {
    $('messages').querySelector('.welcome')?.remove();
    const row = document.createElement('article'); row.className = 'message ' + role;
    const text = document.createElement('div'); text.className = 'message-text'; text.textContent = content;
    row.append(text); $('messages').append(row); row.scrollIntoView({block:'end'}); return row;
  }
  async function json(url, options) {
    const response = await fetch(url, options); const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '请求失败'); return data;
  }
  async function sessions() {
    const data = await json('/api/conversations?user_id=' + encodeURIComponent(userId));
    $('sessions').replaceChildren();
    for (const item of data.items) {
      const button = document.createElement('button'); button.className = 'session' + (item.id === conversationId ? ' current' : '');
      button.textContent = item.title || item.preview || ('对话 ' + item.id); button.disabled = busy;
      button.onclick = () => history(item.id, item.title); $('sessions').append(button);
    }
  }
  async function history(id, title) {
    if (busy) return;
    try {
      status('正在读取历史消息…'); const data = await json('/api/conversations/' + id + '/messages');
      conversationId = id; $('conversation-title').textContent = title || '值班对话'; $('messages').replaceChildren();
      for (const item of data.items) { const row = message(item.role, item.content); document.dispatchEvent(new CustomEvent('opspaw:history', {detail:{item,row}})); }
      await sessions(); status('');
    } catch (error) { status(error.message); }
  }
  function setBusy(value) {
    busy = value; $('send').disabled = value; $('message-input').disabled = value; $('new-conversation').disabled = value;
    $('messages').setAttribute('aria-busy', String(value));
    $('sessions').querySelectorAll('button').forEach(button => { button.disabled = value; });
  }
  async function stream(path, payload, row) {
    if (busy) return;
    setBusy(true); status('值班猫正在处理…');
    try {
      const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '请求失败'); }
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const result = await reader.read(); buffer += decoder.decode(result.value || new Uint8Array(), {stream:!result.done});
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0,end); buffer = buffer.slice(end+2);
          const raw = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!raw || raw === '[DONE]') continue;
          const data = JSON.parse(raw);
          if (data.message && !data.event && !data.delta) throw new Error(data.message);
          if (data.delta) { row.querySelector('.message-text').textContent += data.delta; row.scrollIntoView({block:'end'}); }
          if (data.conversation_id) conversationId = data.conversation_id;
          document.dispatchEvent(new CustomEvent('opspaw:event', {detail:{data,row}}));
        }
        if (result.done) break;
      }
      status(''); await sessions();
    } catch (error) { status(error.message); if (!row.querySelector('.message-text').textContent) row.querySelector('.message-text').textContent = '这次请求未完成，请查看下方说明。'; }
    finally { setBusy(false); $('message-input').focus(); }
  }
  $('composer').onsubmit = event => {
    event.preventDefault(); const text = $('message-input').value.trim(); if (!text || busy) return;
    message('user',text); $('message-input').value = ''; const row = message('assistant','');
    stream('/api/chat',{user_id:userId,message:text,conversation_id:conversationId},row);
  };
  $('message-input').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } };
  $('new-conversation').onclick = () => { conversationId = null; $('messages').replaceChildren(); $('conversation-title').textContent = '新的值班对话'; status(''); sessions().catch(error => status(error.message)); $('message-input').focus(); };
  window.opspaw = {resume:value => stream('/api/actions/resume',{conversation_id:conversationId,...value},message('assistant','')), get conversationId(){return conversationId;}, json, status};
  json('/api/whoami').then(data => { userId = data.user_id; return sessions(); }).catch(error => status(error.message));
})();
