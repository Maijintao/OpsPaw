from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_serves_chat_page():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # 聊天页是客户看的页面,不留后台入口;后台各页自己带「聊天页 →」
    assert 'href="/admin"' not in resp.text


def test_admin_pages_are_reachable():
    """后台各页都能直接开;共享外壳(样式 + 脚本 + 导航)也在静态目录里。

    2026-10-21 删掉两处过时断言:①"页面源码里必须出现某个接口名"——后台页改成共享
    外壳 + 页面脚本取数后,接口名搬进了 static/*.js,页面 HTML 里本来就不该再有它;
    ②"静态资源能匿名取到"——访问闸对 /static/* 要求登录态,匿名请求 401 是设计如此。
    接口与静态资源各由自己的测试覆盖,这里只保证页面打得开。
    """
    for path in ("/admin", "/kb", "/rag-eval", "/review", "/observability",
                 "/topics", "/topics/questions", "/acceptance",
                 "/acceptance/eval", "/acceptance/data", "/acceptance/errors"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html"), path
