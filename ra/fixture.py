"""本地测试站点：录制与回放链路的真实目标页面（自检与测试共用）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORM_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>示例站 · 联系我们</title></head>
<body>
<header><h1>示例站</h1><nav><a href="/">首页</a><a href="/contact" class="on">联系我们</a></nav></header>
<main>
  <h2>联系我们</h2>
  <p>留言将在 1 个工作日内回复。</p>
  <button id="open-form" type="button">打开留言表</button>
  <form id="contact" hidden>
    <label for="name">姓名</label><input id="name" name="name" type="text" placeholder="你的称呼">
    <label for="email">Email</label><input id="email" name="email" type="email" placeholder="name@example.com">
    <label for="pwd">密码</label><input id="pwd" name="pwd" type="password" autocomplete="current-password">
    <label for="topic">主题</label>
    <select id="topic" name="topic"><option value="ask">咨询</option><option value="bug">故障</option></select>
    <label for="msg">留言</label><textarea id="msg" name="msg" rows="3"></textarea>
    <div class="row"><button type="reset">重置</button><button id="send" type="submit">发送</button></div>
  </form>
  <p id="done" hidden>留言已收到，感谢反馈。</p>
  <section id="dupes">
    <button type="button" class="copy" aria-label="复制">复制</button>
    <button type="button" class="copy" aria-label="复制">复制</button>
  </section>
</main>
<script>
  document.getElementById('open-form').addEventListener('click', function () {
    document.getElementById('contact').hidden = false;
  });
  document.getElementById('contact').addEventListener('submit', function (event) {
    event.preventDefault();
    document.getElementById('done').hidden = false;
    document.getElementById('contact').hidden = true;
  });
</script>
</body></html>
"""

OTHER_PAGE = "<!DOCTYPE html><html><body><h1>其他来源</h1></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 约定
        body = FORM_PAGE if self.path in {"/", "/contact"} else OTHER_PAGE
        if getattr(self.server, "foreign", False):
            body = OTHER_PAGE
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默
        pass


class FixtureSite:
    """两个 HTTP 来源，便于验证跨来源事件被丢弃。"""

    def __init__(self) -> None:
        self.primary = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.secondary = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.secondary.foreign = True
        self.threads: list[threading.Thread] = []

    def start(self) -> "FixtureSite":
        for server in (self.primary, self.secondary):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    @property
    def url(self) -> str:
        host, port = self.primary.server_address[:2]
        return f"http://{host}:{port}/contact"

    @property
    def origin(self) -> str:
        host, port = self.primary.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def foreign_origin(self) -> str:
        host, port = self.secondary.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        for server in (self.primary, self.secondary):
            server.shutdown()
            server.server_close()
