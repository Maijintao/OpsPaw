(() => {
  function mount(row, action) {
    if (!action.repair_id || row.querySelector(`[data-repair-id="${CSS.escape(action.repair_id)}"]`)) return;
    const card = document.createElement('section'); card.className = 'operation diagnosis-card'; card.dataset.repairId = action.repair_id;
    const title = document.createElement('strong'); title.textContent = '修复处理 · ' + (action.service || '已确认根因');
    const status = document.createElement('p'); status.setAttribute('role', 'status');
    const link = document.createElement('a'); link.className = 'btn'; link.href = '/repair/' + encodeURIComponent(action.repair_id); link.textContent = '查看修复回执';
    const stop = document.createElement('button'); stop.className = 'btn'; stop.textContent = '停止现场调查'; stop.hidden = true;
    stop.onclick = async () => { stop.disabled = true; try { await window.opspaw.json('/api/repair/runs/' + encodeURIComponent(action.repair_id) + '/stop', {method:'POST'}); status.textContent = '正在停止只读调查…'; poll(); } catch (e) { status.textContent = e.message; stop.disabled = false; } };
    card.append(title, status, link, stop); row.append(card);
    async function poll() {
      if (!card.isConnected) return;
      try {
        const run = await window.opspaw.json('/api/repair/runs/' + encodeURIComponent(action.repair_id));
        const last = (run.events || []).at(-1); status.textContent = (run.status === 'running' ? '处理中 · ' : '') + (last?.detail || run.stage || run.status);
        stop.hidden = run.status !== 'running' || run.stage !== 'scene';
        if (run.status === 'running') setTimeout(poll, 2000);
      } catch (e) { status.textContent = '读取修复进度失败：' + e.message; }
    }
    poll();
  }
  document.addEventListener('opspaw:event', ({detail:{data,row}}) => { if (data.event === 'actions') for (const a of data.items || []) if (a.type === 'repair_card') mount(row,a); });
  document.addEventListener('opspaw:history', ({detail:{item,row}}) => { for (const a of item.actions || []) if (a.type === 'repair_card') mount(row,a); });
})();
