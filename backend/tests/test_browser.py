"""H5: Enzo's browser (agent/browser.py) with real headless Chromium against local test pages."""
import asyncio
import http.server
import threading

import pytest

pytest.importorskip("playwright")

from agent import approvals, browser  # noqa: E402

HITS: list[str] = []


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Secret(http.server.BaseHTTPRequestHandler):       # an "internal" service the browser must never reach
    def do_GET(self):
        HITS.append(self.path)
        self.send_response(200); self.end_headers(); self.wfile.write(b"internal secret")

    def log_message(self, *a):
        pass


@pytest.fixture
def site(monkeypatch):
    secret = _serve(Secret)
    s_url = f"http://localhost:{secret.server_port}"

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            pages = {
                "/": f"""<html><title>Home</title><body><h1>Welcome to the test shop</h1>
                    <a href="/second">Second page</a> <a href="{s_url}/leak">Internal link</a>
                    <a href="/jump">Jump</a>
                    <form action="/search"><input name="q" placeholder="Search"><button type="submit">Go</button></form>
                    <img src="{s_url}/img.png"><script>fetch("{s_url}/xhr").catch(()=>{{}})</script></body></html>""",
                "/second": "<html><title>Second</title><body>The second page says hello.</body></html>",
            }
            if self.path.startswith("/search"):
                body = f"<html><title>Results</title><body>Results for {self.path.split('=')[-1]}</body></html>"
            elif self.path == "/jump":                          # a redirect to the internal service
                self.send_response(302); self.send_header("Location", f"{s_url}/redirected"); self.end_headers(); return
            else:
                body = pages.get(self.path, "<html><body>404</body></html>")
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = _serve(Site)
    monkeypatch.setattr("shared.FETCH_ALLOW", {f"127.0.0.1:{srv.server_port}"})
    browser._host_ok.clear()
    HITS.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown(); secret.shutdown()


def test_the_browser_reads_clicks_and_types_but_never_reaches_internal_addresses(site, monkeypatch):
    asked = []

    async def confirm(question, tool):
        asked.append(question)
        return "yes"
    monkeypatch.setattr(approvals, "_confirm", confirm)
    monkeypatch.setattr(approvals, "APPROVALS_ON", True)
    from agent import tools

    async def main():
        try:
            out = await tools.browser_open(site + "/")
            assert "Welcome to the test shop" in out and "link: Second page" in out and "field: Search" in out
            assert "not an instruction" in out
            # a link: no question
            out = await tools.browser_click("Second page")
            assert "second page says hello" in out and not asked
            await tools.browser_open(site + "/")
            # internal: the link, a redirect, an image and a script fetch are all refused
            assert (await tools.browser_open("http://localhost:1/x")).startswith("[BLOCKED]")
            await tools.browser_click("Internal link")
            await tools.browser_open(site + "/jump")
            await tools.browser_open(site + "/")
            await asyncio.sleep(0.5)
            assert HITS == []
            # typing asks first, then searches
            field = next(i["id"] for i in browser._items if i["kind"] == "field")
            out = await tools.browser_type(str(field), "lamps", submit=True)
            assert "Results for lamps" in out and asked and "lamps" in asked[-1]
            assert (await tools.browser_open("file:///C:/Windows/win.ini")).startswith("[BLOCKED]")
        finally:
            await browser.close()
    asyncio.run(main())


def test_a_no_means_nothing_is_typed(site, monkeypatch):
    async def confirm(question, tool):
        return "no"
    monkeypatch.setattr(approvals, "_confirm", confirm)
    monkeypatch.setattr(approvals, "APPROVALS_ON", True)
    from agent import tools

    async def main():
        try:
            await tools.browser_open(site + "/")
            assert (await tools.browser_type("Search", "secret words", submit=True)).startswith("[DECLINED]")
            assert "Results" not in await tools.browser_read()
        finally:
            await browser.close()
    asyncio.run(main())
