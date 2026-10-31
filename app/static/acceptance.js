/* Shared helpers for the RAG administration pages. */
(function () {
  window.$ = (id) => document.getElementById(id);

  window.el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };

  window.api = async (path, options) => {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail || body);
      throw new Error(detail || ("HTTP " + response.status));
    }
    return body;
  };

  window.toast = (message, error) => {
    const node = $("toast") || document.body.appendChild(el("div", "toast"));
    node.id = "toast";
    node.textContent = message;
    node.className = error ? "show err" : "show";
    clearTimeout(node._timer);
    node._timer = setTimeout(() => { node.className = ""; }, 3200);
  };

  window.missingBox = (message) => el("div", "miss", message || "暂无数据");

  window.fmtTime = (value) => value ? String(value).replace("T", " ").slice(0, 19) : "—";

  window.pct = (value) => value === null || value === undefined
    ? "—"
    : (Number(value) * 100).toFixed(1) + "%";

  window.readNoteHtml = (note, fallback) => {
    const text = note || fallback || "";
    return String(text)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replace(/\\*\\*(.+?)\\*\\*/g, "<b>$1</b>");
  };

  window.mountAdminNav = (active) => {
    if (document.querySelector(".rag-admin-nav")) return;
    const links = [
      ["/admin", "后台首页"], ["/kb", "知识库录入"],
      ["/rag-eval", "RAG 评估"], ["/review", "飞轮待审"],
      ["/observability", "观测与成本"], ["/topics", "主题分布"],
    ];
    const nav = el("nav", "rag-admin-nav nav");
    for (const [href, label] of links) {
      const link = el("a", href === active ? "on" : "", label);
      link.href = href;
      nav.appendChild(link);
    }
    const host = document.querySelector(".topbar");
    const wrap = document.querySelector(".wrap") || document.body;
    if (host && host.parentNode) host.parentNode.insertBefore(nav, host.nextSibling);
    else wrap.insertBefore(nav, wrap.firstChild);
  };
})();
