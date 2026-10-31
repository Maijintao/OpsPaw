(function () {
  function priorQuestion(row) {
    let node = row.previousElementSibling;
    while (node) {
      if (node.classList.contains("user")) {
        return node.querySelector(".message-text")?.textContent || "";
      }
      node = node.previousElementSibling;
    }
    return "";
  }

  function citations(row, items) {
    if (!Array.isArray(items) || !items.length) return;
    row.querySelector(".rag-citations")?.remove();
    const box = document.createElement("details");
    box.className = "rag-citations";
    const summary = document.createElement("summary");
    summary.textContent = "回答依据（" + items.length + " 条）";
    box.appendChild(summary);
    const list = document.createElement("ol");
    for (const item of items) {
      const entry = document.createElement("li");
      const question = document.createElement("strong");
      question.textContent = item.question || "未命名知识块";
      const answer = document.createElement("p");
      answer.textContent = item.answer || "无正文";
      const source = document.createElement("small");
      source.textContent = [item.section_path, item.content_type, "id " + (item.id ?? "—")]
        .filter(Boolean).join(" · ");
      entry.append(question, answer, source);
      list.appendChild(entry);
    }
    box.appendChild(list);
    row.appendChild(box);
  }

  function feedback(row) {
    if (!row.classList.contains("assistant") || row.querySelector(".rag-feedback")) return;
    const bar = document.createElement("div");
    bar.className = "rag-feedback";
    const label = document.createElement("span");
    label.textContent = "这条回答有帮助吗？";
    const good = document.createElement("button");
    good.className = "btn sm";
    good.textContent = "有帮助";
    const bad = document.createElement("button");
    bad.className = "btn sm";
    bad.textContent = "没解决";

    const submit = async (rating, button) => {
      const cid = window.opspaw?.conversationId;
      const question = priorQuestion(row);
      if (!cid || !question) return;
      button.disabled = true;
      try {
        const result = await window.opspaw.json("/api/feedback", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({conversation_id: cid, rating, question}),
        });
        label.textContent = rating === "up"
          ? "已记录反馈"
          : (result.pooled ? "已进入知识补充队列" : "已记录反馈");
        good.remove();
        bad.remove();
      } catch (error) {
        label.textContent = "反馈失败：" + error.message;
        button.disabled = false;
      }
    };
    good.addEventListener("click", () => submit("up", good));
    bad.addEventListener("click", () => submit("down", bad));
    bar.append(label, good, bad);
    row.appendChild(bar);
  }

  document.addEventListener("opspaw:event", (event) => {
    const {data, row} = event.detail;
    if (data.event === "citations") citations(row, data.items);
    if (data.event === "done") feedback(row);
  });

  document.addEventListener("opspaw:history", (event) => {
    const {item, row} = event.detail;
    if (item.role !== "assistant") return;
    citations(row, item.citations);
    feedback(row);
  });
})();
