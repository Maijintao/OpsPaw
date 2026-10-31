# 原生层稳定性开关,必须在任何 import 之前设置(libomp/grpc 在库初始化时读取):
# - 诊断归因会懒加载 torch/transformers,养出一池 OpenMP 工作线程;多线程进程里一旦
#   发生 fork(实测 2026-10-19 两次 segfault,崩溃线程都在 libomp),线程池状态即被
#   搅坏。适配路径模型全是 stub、不做张量计算,OMP 并行纯属白养——压到 1 线程,
#   崩溃面直接消失。
# - gRPC(Milvus 客户端)默认不支持多线程 fork;开着 fork 支持让 fork 不再破坏其状态。
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.acceptance import router as acceptance_router
from app.api.grafana import router as grafana_router
from app.api.incidents import router as incidents_router
from app.api.sentinels import router as sentinels_router
from app.api.actions import router as actions_router
from app.api.admin import router as admin_router
from app.api.agent import router as agent_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.diagnosis import router as diagnosis_router
from app.api.extract import router as extract_router
from app.api.feedback import router as feedback_router
from app.api.jobs import router as jobs_router
from app.api.kb import router as kb_router
from app.api.observability import router as observability_router
from app.api.rageval import router as rageval_router
from app.api.repair import router as repair_router
from app.api.review import router as review_router
from app.api.settings import router as settings_router
from app.api.targets import router as targets_router
from app.api.tools import router as tools_router
from app.api.topics import router as topics_router
from app.graph import runtime

# 让 app.* 的 INFO 日志可见(uvicorn 默认不给 app 记录器配 INFO handler,
# 否则 ch05 log 节点的 turn trace——验收1 靠它观测「强制检索节点被走到」——不会输出)
_applog = logging.getLogger("app")
if not _applog.handlers:
    _fmt = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    _h = logging.StreamHandler()
    _h.setFormatter(_fmt)
    _applog.addHandler(_h)
    # 同时落项目内 log/app.log:稳定的 tail 目标(app.* 决策日志,不含 uvicorn HTTP 噪声,
    # 因 propagate=False + uvicorn 日志不经 app 记录器)。目录随启动自建,已 gitignore。
    _log_dir = Path(__file__).resolve().parent.parent / "log"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(_log_dir / "app.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _applog.addHandler(_fh)
_applog.setLevel(logging.INFO)
_applog.propagate = False

logger = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).parent / "static"
# 页面 HTML 的缓存策略与静态资源(_CachedStatic 5 分钟)对齐:每次刷新省掉一次
# 跨国往返的 ETag 复验(实测复验还会退回 200 全量 171KB 重传)。页面 HTML 与
# 内嵌脚本是同一个文件,缓存原子生效,不存在"新页面配旧脚本"的错配。
_PAGE_CACHE = {"Cache-Control": "public, max-age=300"}


async def _warm_grafana_cache() -> None:
    """后台预热:把每个启用平台的面板和服务清单先算一遍,进页面就是秒开。"""
    try:
        from app.db import repository
        from app.tools import grafana as gmod
        await asyncio.sleep(3)
        for t in [r for r in await repository.list_targets() if r.enabled]:
            try:
                await gmod.ensure_dashboard(t.id)
                logger.info("面板预热完成:%s", t.name)
            except Exception:  # noqa: BLE001  单个平台失败不影响其它
                logger.warning("面板预热失败:%s", t.name, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("面板预热任务异常", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动预热 Milvus:加载集合并轮询直到可检索,避免首个用户请求撞上 Standalone 的加载竞态
    (集合 load 在后台异步完成,未就绪时检索会静默返回空)。预热失败不阻塞启动。"""
    try:
        from app.core import retrieval
        for _ in range(15):
            hits = await retrieval.search_knowledge("CPU 告警阈值", strategy="bm25", top_k=1)
            if hits:
                logger.info("Milvus 预热完成,集合可检索")
                break
            await asyncio.sleep(1)
        else:
            logger.warning("Milvus 预热超时(15s)未返回结果,继续启动")
    except Exception:
        logger.warning("Milvus 预热异常,继续启动", exc_info=True)
    from app.db import repository
    await repository.ensure_targets_table()   # 靶子表启动自建(checkfirst,已有表不碰)
    await repository.ensure_settings_table()  # API 设置表启动自建(同上)
    await repository.ensure_diagnosis_table()  # 诊断任务存档表启动自建(同上,卡片持久化)
    # 告警接入:建表 + 起调度器(定时巡检/指标阈值都靠它)
    try:
        from app.api import sentinels as sentinels_mod
        await sentinels_mod.ensure_tables()
        sentinels_mod.start_scheduler()
        # 面板和服务清单的生成要四秒多:启动后先在后台预热,
        # 用户第一次点「服务监控」就不用干等(预热失败不影响启动)
        asyncio.create_task(_warm_grafana_cache())
    except Exception:
        logger.warning("告警接入调度器启动失败,定时巡检不可用", exc_info=True)
    await repository.ensure_repair_tables()    # 修复连接器两张表启动自建(同上,注册与身份)
    await repository.ensure_repair_runs_table()  # 修复 agent 任务存档表启动自建(同上,卡片持久化)
    await repository.ensure_incident_tables()   # 事故中心两张表启动自建(同上,事故与时间线)
    from app.tools import registry as tool_registry
    tool_registry.scan_builtin()   # ch08:内置工具服务启动时登记(扫描 builtin/ 包,import 即注册)
    await runtime.init_graph()
    # 数据源地址自愈后台循环:甲方重建容器导致端口漂移后,一分钟内自动跟随新地址
    # (三道闸:host 不变/唯一应答者/实例指纹验身,认不准不动,见 api/targets.py)
    from app.api import targets as targets_api
    heal_task = asyncio.create_task(targets_api.heal_loop())
    # 数据保留后台循环:每 6 小时醒一次,开关(admin「数据保留」)开着才清
    from app.db import retention as retention_mod
    retention_task = asyncio.create_task(retention_mod.retention_loop())
    yield
    heal_task.cancel()
    retention_task.cancel()
    await runtime.close_graph()


app = FastAPI(title="OpsPaw", version="0.1.0", lifespan=lifespan)

# ---- 传输压缩(2026-10-20):服务端在澳洲,国内访问单次往返近 1 秒、带宽只有几百 kbps,
# 后台首页 HTML 41KB、聊天页 99KB 明文传要一秒多。开 gzip 后体积降到四分之一左右,
# 首屏「加载中」明显变短。1KB 以下的小响应不压(压缩头本身就够抵消收益)。
from fastapi.middleware.gzip import GZipMiddleware


class _Gzip(GZipMiddleware):
    """压缩要绕开连接器的长轮询。

    连接器的 /api/repair/poll 是 25 秒挂着的长轮询;GZipMiddleware 要等响应体
    才开始压,叠在外层 BaseHTTPMiddleware 上时,长请求会被判成"没有响应返回"
    (2026-10-21 实测:加完 gzip 后连接器立刻开始 "取任务失败 HTTP 500",
    journal 里是 EndOfStream / No response returned)。这些接口的响应本来就小,
    压不压没差别,直接放行。"""

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith("/api/repair/"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(_Gzip, minimum_size=1024)


# ---- 访问防护(2026-10-20):8000 公网暴露后的登录闸 ----
# 只防外网:连接器/跑批/本机探活全从 127.0.0.1 进来(含 SSH 隧道转发),直接放行。
# 外部访问没有有效会话 cookie 时,页面一律 302 到 /login(同全局风格的登录页),
# API 调用返回 401 JSON;登录成功发签名 cookie(HttpOnly,30 天),一台机器输一次就行。
# 账号密码可用环境变量覆盖(ACCESS_USER/ACCESS_PASS),不配就用这套默认。
import hashlib
import hmac
import time as _time

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

_ACCESS_USER = os.environ.get("ACCESS_USER", "admin")
_ACCESS_PASS = os.environ.get("ACCESS_PASS", "sztu@172712")
# 只读演示账号:配了才存在,不配就没有这个账号(默认不开口子)。
# 用途是给人看效果——能进后台各页看数据,不能对话、不能改任何东西。
_VIEWER_USER = os.environ.get("ACCESS_VIEWER_USER", "")
_VIEWER_PASS = os.environ.get("ACCESS_VIEWER_PASS", "")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
_SESSION_COOKIE = "opspaw_session"
_SESSION_TTL = 30 * 24 * 3600          # 30 天免登录
_LOGIN_FORM = _STATIC_DIR / "login.html"

# 角色:admin 全权;viewer 只读(见 _viewer_blocked)
_ROLE_ADMIN = "admin"
_ROLE_VIEWER = "viewer"


def _cred_key(user: str, pwd: str) -> bytes:
    """一组凭据派生一把签名密钥。换密码 = 换密钥 = 该角色的旧会话自动作废。"""
    return hashlib.sha256(f"{user}:{pwd}".encode()).digest()


_ADMIN_KEY = _cred_key(_ACCESS_USER, _ACCESS_PASS)
# 没配只读账号时这里为 None:既签不出 viewer 令牌,也验不过任何 viewer 令牌
_VIEWER_KEY = _cred_key(_VIEWER_USER, _VIEWER_PASS) if _VIEWER_USER and _VIEWER_PASS else None
_SESSION_KEY = _ADMIN_KEY          # 旧格式令牌(升级前发出去的)仍按管理员验,不把人踢下线


def _session_token(now: float, role: str = _ROLE_ADMIN) -> str:
    """会话凭据 = 过期时间 + 角色 + HMAC。角色签在令牌里,不另存会话表。"""
    exp = int(now) + _SESSION_TTL
    key = _VIEWER_KEY if role == _ROLE_VIEWER else _ADMIN_KEY
    payload = f"{exp}.{role}"
    return f"{payload}.{hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()}"


def _session_role(token: str) -> str | None:
    """验签返回角色,任何异常都当没登录。两段旧格式(无角色)按管理员认,免得升级踢人下线。"""
    try:
        parts = (token or "").split(".")
        if len(parts) == 2:
            exp, sig = parts
            want = hmac.new(_SESSION_KEY, exp.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, want) and int(exp) > _time.time():
                return _ROLE_ADMIN
            return None
        if len(parts) != 3:
            return None
        exp, role, sig = parts
        if role not in (_ROLE_ADMIN, _ROLE_VIEWER):
            return None
        key = _VIEWER_KEY if role == _ROLE_VIEWER else _ADMIN_KEY
        if key is None:
            return None
        want = hmac.new(key, f"{exp}.{role}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        return role if int(exp) > _time.time() else None
    except Exception:  # noqa: BLE001  格式不对/解不开一律当没登录
        return None


# 只读账号的封条:非读方法全拒;对话类接口连读也不给。聊天页和会话记录可以看。
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_VIEWER_BLOCKED_PREFIXES = ("/api/chat", "/api/agent", "/api/actions",
                            "/api/extract", "/api/feedback")


def _viewer_blocked(method: str, path: str):
    """返回响应 = 拦下;None = 放行。先判接口再判方法,发消息的提示才说得对。"""
    for p in _VIEWER_BLOCKED_PREFIXES:
        if path == p or path.startswith(p + "/"):
            return JSONResponse(status_code=403,
                                content={"detail": "这是只读账号:不能对话。"})
    if method not in _READ_METHODS:
        return JSONResponse(status_code=403,
                            content={"detail": "这是只读账号:可以看,不能改。"})
    return None


def _cred_match(user: str, pwd: str, want_user: str, want_pass: str) -> bool:
    """定长比较。compare_digest 只吃 ASCII,非 ASCII 会抛 TypeError,兜成不匹配。"""
    try:
        return (hmac.compare_digest(user, want_user)
                and hmac.compare_digest(pwd, want_pass))
    except TypeError:
        return False


@app.middleware("http")
async def _access_guard(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client in _LOOPBACK_HOSTS:
        return await call_next(request)
    path = request.url.path
    if path in ("/login", "/logout"):
        return await call_next(request)
    role = _session_role(request.cookies.get(_SESSION_COOKIE, ""))
    if role is None:
        if path.startswith("/api") or path.startswith("/static"):
            return JSONResponse(status_code=401, content={"detail": "需要登录"})
        next_url = request.url.path if request.method == "GET" else "/"
        return RedirectResponse(f"/login?next={next_url}", status_code=302)
    if role == _ROLE_VIEWER:
        blocked = _viewer_blocked(request.method, path)
        if blocked is not None:
            return blocked
    return await call_next(request)


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(_LOGIN_FORM)


@app.get("/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    """清掉会话 cookie 回登录页。用 GET 是有意的:一个链接就能退。"""
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@app.get("/api/session", include_in_schema=False)
async def session_info(request: Request) -> dict:
    """当前会话是谁。前端据此决定要不要显示"只读"角标、要不要拦写操作。

    本机请求(隧道/跑批/连接器)在中间件里是直接放行的,这里也按管理员报,
    免得本机页面把自己当成只读账号、按钮全灰。
    """
    client = request.client.host if request.client else ""
    role = (_ROLE_ADMIN if client in _LOOPBACK_HOSTS
            else _session_role(request.cookies.get(_SESSION_COOKIE, "")))
    return {"role": role, "readonly": role == _ROLE_VIEWER}


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    form = await request.form()
    user = str(form.get("username", ""))
    pwd = str(form.get("password", ""))
    next_url = str(form.get("next", "/"))
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"                      # 只认站内路径,防跳外站
    if _cred_match(user, pwd, _ACCESS_USER, _ACCESS_PASS):
        role = _ROLE_ADMIN
    elif _VIEWER_KEY and _cred_match(user, pwd, _VIEWER_USER, _VIEWER_PASS):
        role = _ROLE_VIEWER
    else:
        return RedirectResponse(f"/login?err=1&next={next_url}", status_code=303)
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(_SESSION_COOKIE, _session_token(_time.time(), role),
                    max_age=_SESSION_TTL, httponly=True, samesite="lax")
    return resp


app.include_router(actions_router)
app.include_router(chat_router)
app.include_router(extract_router)
app.include_router(agent_router)
app.include_router(conversations_router)
app.include_router(diagnosis_router)
app.include_router(repair_router)
app.include_router(feedback_router)
app.include_router(review_router)
app.include_router(tools_router)
app.include_router(topics_router)
app.include_router(acceptance_router)
app.include_router(grafana_router)
app.include_router(sentinels_router)
app.include_router(incidents_router)
app.include_router(kb_router)
app.include_router(rageval_router)
app.include_router(observability_router)
app.include_router(admin_router)
app.include_router(targets_router)
app.include_router(settings_router)
app.include_router(jobs_router)

# 后台各页共用外壳(样式 + 取数/重跑脚本 + 导航),抽成文件放静态目录
class _CachedStatic(StaticFiles):
    """静态资源带 5 分钟缓存,省掉每次进页面的一次往返(国际链路一次 ~1 秒)。

    刻意不用 `@app.middleware("http")` 全局中间件:那是 BaseHTTPMiddleware,
    会把每个请求包一层内存流,**连接器 25 秒的长轮询被它包住后,客户端一断开就抛
    EndOfStream → 500**(2026-10-21 实测:加完缓存中间件,连接器立刻开始报
    "取任务失败 HTTP 500")。挂载点里加响应头,只影响静态文件,长轮询不受牵连。
    """

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers.setdefault("Cache-Control", "public, max-age=300")
        return resp


app.mount("/static", _CachedStatic(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def chat_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html", headers=_PAGE_CACHE)


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    """后台管理首页:知识库、RAG 评估、飞轮待审、观测与成本、主题分布、分类器验收各一张卡,一处进出。
    各模块页面还是各自原本的路径,首页只把入口收到一起,文档里贴过的链接照样能用。"""
    return FileResponse(_STATIC_DIR / "admin.html", headers=_PAGE_CACHE)


@app.get("/kb", include_in_schema=False)
async def kb_page() -> FileResponse:
    """ch03 知识库录入页:贴文档就能切块入库、向量化、当场检索自测。"""
    return FileResponse(_STATIC_DIR / "kb.html", headers=_PAGE_CACHE)


@app.get("/rag-eval", include_in_schema=False)
async def rageval_page() -> FileResponse:
    """ch04 RAG 评估页:四策略对照的那份报告,页面上看,也在页面上重跑。"""
    return FileResponse(_STATIC_DIR / "rageval.html", headers=_PAGE_CACHE)


@app.get("/review", include_in_schema=False)
async def review_page() -> FileResponse:
    """ch09 飞轮待审队列后台管理页。"""
    return FileResponse(_STATIC_DIR / "review.html", headers=_PAGE_CACHE)


@app.get("/observability", include_in_schema=False)
async def observability_page() -> FileResponse:
    """ch09 观测与成本页:意图成本账、评估趋势、置信度阈值校准三张报表,页上看也页上重跑。"""
    return FileResponse(_STATIC_DIR / "observability.html", headers=_PAGE_CACHE)


@app.get("/tools", include_in_schema=False)
async def tools_page() -> FileResponse:
    """ch08 工具清单页:Agent 现拉的统一工具面(内置 ∪ MCP),横排卡片看每个工具的用途与参数。"""
    return FileResponse(_STATIC_DIR / "tools.html", headers=_PAGE_CACHE)


@app.get("/diagnosis/{diag_id}", include_in_schema=False)
async def diagnosis_page(diag_id: str) -> FileResponse:
    """根因分析详情页:全链路拓扑(健康度分级着色)+候选+证据链+缺口+过程,聊天卡片「查看详情」跳到这里。"""
    return FileResponse(_STATIC_DIR / "diagnosis.html", headers=_PAGE_CACHE)


@app.get("/repair/{repair_id}", include_in_schema=False)
async def repair_page(repair_id: str) -> FileResponse:
    """修复回执详情页:agent 修复回执(现场确认/修复过程/结果确认/看法)+执行证据,修复卡「查看详情」跳到这里。"""
    return FileResponse(_STATIC_DIR / "repair.html", headers=_PAGE_CACHE)


@app.get("/incident/{iid}", include_in_schema=False)
async def incident_page(iid: str) -> FileResponse:
    """事故详情页:时间线(告警/诊断/修复/状态)+关联诊断与修复回执,事故卡「查看详情」跳到这里。"""
    return FileResponse(_STATIC_DIR / "incident.html", headers=_PAGE_CACHE)


@app.get("/topics", include_in_schema=False)
async def topics_page() -> FileResponse:
    """ch10 主题分布页:分类器旁路归类结果,按 17 类看问题量。"""
    return FileResponse(_STATIC_DIR / "topics.html", headers=_PAGE_CACHE)


@app.get("/topics/questions", include_in_schema=False)
async def topic_questions_page() -> FileResponse:
    """ch10 类目问题列表:从分布图点进某一类,分页看这类下归类到的全部问题。"""
    return FileResponse(_STATIC_DIR / "topic-questions.html", headers=_PAGE_CACHE)


@app.get("/acceptance", include_in_schema=False)
async def acceptance_page() -> FileResponse:
    """ch10 验收总览:九项实证各一张闸门卡,产物缺失就地重跑。"""
    return FileResponse(_STATIC_DIR / "acceptance.html", headers=_PAGE_CACHE)


@app.get("/acceptance/eval", include_in_schema=False)
async def acceptance_eval_page() -> FileResponse:
    """ch10 评测详情:每类 P/R/F1 与容错红线、micro vs macro、阈值扫描、混淆矩阵、单句试分类。"""
    return FileResponse(_STATIC_DIR / "acceptance-eval.html", headers=_PAGE_CACHE)


@app.get("/acceptance/data", include_in_schema=False)
async def acceptance_data_page() -> FileResponse:
    """ch10 数据产物:语料血缘、三份考卷与泄漏自检、训练与 ONNX 产物盘点。"""
    return FileResponse(_STATIC_DIR / "acceptance-data.html", headers=_PAGE_CACHE)


@app.get("/acceptance/errors", include_in_schema=False)
async def acceptance_errors_page() -> FileResponse:
    """ch10 错例复核:逐条标准/预测对照、错误方向记账、边界摩擦配对。"""
    return FileResponse(_STATIC_DIR / "acceptance-errors.html", headers=_PAGE_CACHE)
