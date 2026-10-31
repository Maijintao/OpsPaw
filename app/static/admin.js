/* 后台管理外壳:一份导航挂在所有后台页上(知识库录入 / RAG 评估 / 待审队列 / 观测与成本 /
   主题分布 / 分类器验收)。

   这些页面分属不同章、样式各自内联,所以导航自带样式、自己注入,不去动宿主页的 CSS;
   整个文件包在 IIFE 里,只往 window 上挂两个名字,免得和宿主页的 $ / el 撞名。
   模块入口都是各章原本的路径,导航只是把它们收到一处,不做跳转改写——文档里贴的链接照样能用。

   另外这里负责**只读账号**的前端那一半:角标、藏掉聊天入口、把写操作拦在发出去之前。
   真正的封条在服务端(见 app/main.py 的 _viewer_blocked),这里只是让点下去的人
   当场知道"为什么没反应",而不是看到一个没头没尾的报错。 */
(function () {
  /* 角色由 /api/session 回答;取到之前先按管理员渲染,取到之后再纠正(见 applyViewerChrome)。
     不阻塞渲染是有意的:多等一次往返才出导航,页面会闪。 */
  let readonly = false;
  /* 导航就一层:产品页和课程各章的验收页平铺,不额外分组——
     工具清单从后台首页的按钮进,不占导航位。 */
  const NAV = [
    { href: "/admin", label: "后台首页" },
    { href: "/kb", label: "知识库录入" },
    { href: "/rag-eval", label: "RAG 评估" },
    { href: "/review", label: "飞轮待审" },
    { href: "/observability", label: "观测与成本" },
    { href: "/topics", label: "主题分布" },
    {
      href: "/acceptance", label: "分类器验收",
      children: [
        ["/acceptance", "总览"],
        ["/acceptance/eval", "评测详情"],
        ["/acceptance/data", "数据产物"],
        ["/acceptance/errors", "错例复核"],
      ],
    },
  ];

  const CSS = `
  .mh-nav { margin-top: 12px; }
  .mh-nav .row {
    display: flex; flex-wrap: wrap; align-items: stretch;
    border: 3px solid #2b2632; background: #fffaf0; box-shadow: 3px 3px 0 #2b2632;
  }
  .mh-nav .row a {
    display: flex; align-items: center; gap: 6px; padding: 7px 13px; font-size: 13px;
    text-decoration: none; color: #2b2632; border-right: 3px solid #2b2632; font-weight: 700;
    transition: background .1s, color .1s;
  }
  /* 有反馈才不像一块死木头:悬停变色、按下再深一档,当前页保持橘色 */
  .mh-nav .row a:hover { background: #ffedd6; }
  .mh-nav .row a:active { background: #ffd9b0; }
  .mh-nav .row a.on:hover { background: #ffb473; }
  .mh-nav .row a:last-child { border-right: 0; }
  .mh-nav .row a.on { background: #ff9f57; }
  .mh-nav .row .brand {
    display: flex; align-items: center; padding: 7px 12px; font-size: 12px;
    background: #2b2632; color: #fff6e6; border-right: 3px solid #2b2632; letter-spacing: 1px;
  }
  .mh-nav .row .grow { flex: 1; border-right: 3px solid #2b2632; }
  .mh-nav .sub {
    display: flex; flex-wrap: wrap; border: 3px solid #2b2632; border-top: 0;
    background: #fff6e6; box-shadow: 3px 3px 0 #2b2632;
  }
  .mh-nav .sub a {
    padding: 5px 12px; font-size: 12px; text-decoration: none; color: #2b2632;
    border-right: 3px solid #2b2632;
  }
  .mh-nav .sub a:last-child { border-right: 0; }
  .mh-nav .sub a.on { background: #ff9f57; font-weight: 700; }
  /* 只读角标:跟顶栏同一个语言(3px 描边 + 硬阴影),一眼能看出这个账号不能改东西 */
  .mh-nav .row .ro-badge {
    display: flex; align-items: center; padding: 7px 12px; font-size: 12px; font-weight: 700;
    background: #ffd9b0; color: #2b2632; border-right: 3px solid #2b2632; letter-spacing: 1px;
  }
  /* 退出:弱化处理,别跟导航抢注意力 */
  .mh-nav .row .ro-out { color: #6d6478; }
  /* 操作被挡下时的提示条:底部浮一条,2.6 秒自己走 */
  #mh-toast {
    position: fixed; left: 50%; bottom: 26px; transform: translate(-50%, 12px);
    background: #2b2632; color: #fff6e6; border: 3px solid #2b2632;
    box-shadow: 3px 3px 0 #ff9f57; padding: 9px 16px; font-size: 13px; font-weight: 700;
    opacity: 0; pointer-events: none; transition: opacity .15s, transform .15s; z-index: 9999;
  }
  #mh-toast.on { opacity: 1; transform: translate(-50%, 0); }
  @media (max-width: 640px) { .mh-nav .row .grow { display: none; } }
  `;

  function injectCss() {
    if (document.getElementById("mh-nav-css")) return;
    const s = document.createElement("style");
    s.id = "mh-nav-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function link(href, label, on, cls) {
    const a = document.createElement("a");
    a.href = href;
    a.className = (on ? "on " : "") + (cls || "");
    a.textContent = label;
    return a;
  }

  /** 当前页归属哪个模块:精确命中优先,其次按前缀(/acceptance/eval 归 /acceptance)。 */
  function moduleOf(active) {
    return NAV.find((m) => m.href === active)
      || NAV.find((m) => m.href !== "/" && active.startsWith(m.href + "/"));
  }

  function renderAdminNav(active) {
    injectCss();
    const wrap = document.createElement("div");
    wrap.className = "mh-nav";
    const row = document.createElement("div");
    row.className = "row";
    const brand = document.createElement("span");
    brand.className = "brand";
    brand.textContent = "后台管理";
    row.appendChild(brand);
    const mod = moduleOf(active);
    for (const m of NAV) row.appendChild(link(m.href, m.label, m === mod));
    const grow = document.createElement("span");
    grow.className = "grow";
    row.appendChild(grow);
    row.appendChild(link("/", "聊天页 →", false));
    // 退出放在最右:它不属于任何一个模块,是"离开这套后台"的动作
    row.appendChild(link("/logout", "退出", false, "ro-out"));
    wrap.appendChild(row);

    if (mod && mod.children) {
      const sub = document.createElement("div");
      sub.className = "sub";
      for (const [href, label] of mod.children) sub.appendChild(link(href, label, href === active));
      wrap.appendChild(sub);
    }
    return wrap;
  }

  /** 挂到顶栏底下(顶栏是每页自己的标题条);找不到顶栏就摆在 .wrap 最前面。 */
  function mountAdminNav(active) {
    const nav = renderAdminNav(active);
    const bar = document.querySelector(".topbar");
    if (bar && bar.parentNode) bar.parentNode.insertBefore(nav, bar.nextSibling);
    else {
      const w = document.querySelector(".wrap") || document.body;
      w.insertBefore(nav, w.firstChild);
    }
    applyViewerChrome();      // 角色可能比导航晚到,晚到就由 bootSession 再补一次
    return nav;
  }

  /* ---------- 只读账号 ---------- */

  function toast(msg) {
    let t = document.getElementById("mh-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "mh-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("on"), 2600);
  }

  /** 只读账号的可见标识:顶栏加一个「只读」角标。幂等——导航先渲染、角色后到,
      或者反过来,最终结果都一样。聊天页对只读账号是能进的,入口照常给。 */
  function applyViewerChrome() {
    if (!readonly) return;
    const row = document.querySelector(".mh-nav .row");
    if (row && !row.querySelector(".ro-badge")) {
      const b = document.createElement("span");
      b.className = "ro-badge";
      b.textContent = "只读";
      // 紧挨着"后台管理"放,而不是甩到最右边:角标属于身份,不属于操作区
      row.insertBefore(b, row.children[1] || null);
    }
  }

  /** 把写操作拦在发出去之前。
      为什么还拦一遍(服务端已经拒了):页面各处的 catch 五花八门,有的把 detail 吞掉只
      弹一句"操作失败",看的人会以为是系统坏了。这里当场给一句人话,并且回一个**形状
      跟服务端 403 一模一样**的 Response,各页原有的错误处理不用改一行。 */
  function guardWrites() {
    const raw = window.fetch;
    window.fetch = function (input, init) {
      const method = String(
        (init && init.method) || (input && input.method) || "GET").toUpperCase();
      if (readonly && !["GET", "HEAD", "OPTIONS"].includes(method)) {
        toast("只读账号:可以看,不能改。");
        return Promise.resolve(new Response(
          JSON.stringify({ detail: "这是只读账号:可以看,不能改。" }),
          { status: 403, headers: { "Content-Type": "application/json" } }));
      }
      return raw.apply(this, arguments);
    };
  }

  /** 问服务端"我是谁"。取不到就按管理员渲染——服务端该拦的还是会拦,不靠前端兜底。 */
  async function bootSession() {
    try {
      const r = await fetch("/api/session", { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      const s = await r.json();
      readonly = !!s.readonly;
      if (readonly) applyViewerChrome();
    } catch (e) { /* 网络抖动:保持管理员渲染,后端仍是唯一裁判 */ }
  }

  guardWrites();
  bootSession();

  window.renderAdminNav = renderAdminNav;
  window.mountAdminNav = mountAdminNav;
  window.adminToast = toast;
})();
