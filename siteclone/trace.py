"""Runtime network-capture stage (Playwright, optional/lazy).

Drives the LIVE site headless, records every network request the page
actually makes (JS-constructed URLs, runtime fetch/XHR payloads,
FontFace/worker loads, websocket URLs), and returns them so the static
pipeline can fetch the gap. Passive observers only — never intercepts.

Public API:
    trace_urls(origin, routes, outdir=None, per_page_s=30) -> list[str]

Writes ``urls.jsonl`` (method/url/status audit) into ``outdir`` when given.
Returns same-origin http(s) URLs. Missing playwright / browser failure
degrades to ``[]`` (static-only), never raises.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_API_HINT = re.compile(
    r"/(?:models|assets|media|static|fonts|images|videos|audio|3d|scenes)"
    r"[^\"'\\\s<>]*"
)

_SCROLL_JS = """(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const seenH = new Set();
  for (let i = 0; i < 40; i++) {
    window.scrollTo(0, (document.body.scrollHeight / 40) * (i + 1));
    await sleep(200);
    seenH.add(document.body.scrollHeight);
    if (i > 8) {
      const last = [...seenH].slice(-3);
      if (last.length === 3 && last[0] === last[1] && last[1] === last[2]) break;
    }
  }
  window.scrollTo(0, 0);
  await sleep(400);
  return true;
})()"""


def _quiesced(pending, deadline):
    calm_since = None
    while time.time() < deadline:
        if pending["open"] <= 0:
            if calm_since is None:
                calm_since = time.time()
            if time.time() - calm_since > 1.5:
                return True
        else:
            calm_since = None
        time.sleep(0.25)
    return False


def trace_urls(origin, routes, outdir=None, per_page_s=30):
    """Record live runtime request URLs for ``routes``. See module docstring."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[trace] playwright not installed — skipping runtime capture")
        return []

    seen: dict = {}
    pending = {"open": 0}

    def _record(method, url, status=None):
        if not url or not url.startswith(("http://", "https://")):
            return
        e = seen.setdefault(url, {"method": method, "status": status, "n": 0})
        e["n"] += 1
        if status is not None:
            e["status"] = status

    def _finished(_req):
        pending["open"] = max(0, pending["open"] - 1)

    def _failed(req):
        _finished(req)
        try:
            _record(req.method, req.url)
        except Exception:
            pass

    def _on_response(resp):
        try:
            u, st = resp.url, resp.status
        except Exception:
            return
        _record("GET", u, st)
        try:
            ct = (resp.headers.get("content-type") or "").lower()
        except Exception:
            ct = ""
        if resp.ok and ("/api/" in u or "json" in ct):
            try:
                body = resp.body().decode("utf-8", "ignore")
            except Exception:
                return  # evicted from inspector cache — URL already logged
            for m in _API_HINT.finditer(body):
                try:
                    _record("GET", urllib.parse.urljoin(u, m.group(0)))
                except Exception:
                    pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=_UA,
                )
                ctx.on("request",
                       lambda r: (pending.__setitem__("open", pending["open"] + 1),
                                  _record(getattr(r, "method", "GET"), getattr(r, "url", ""))))
                ctx.on("requestfinished", _finished)
                ctx.on("requestfailed", _failed)
                ctx.on("response", _on_response)
                ctx.on("websocket", lambda ws: _record("WS", getattr(ws, "url", "")))
                total = len(routes or ["/"])
                for _i, route in enumerate(routes or ["/"], 1):
                    _before = len(seen)
                    print("[trace] page %d/%d %s ..." % (_i, total, route), flush=True)
                    url = origin.rstrip("/") + (route if route.startswith("/") else "/" + route)
                    pg = ctx.new_page()
                    try:
                        try:
                            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                        except Exception as exc:
                            print("[trace] goto %s failed (%s)" % (route, exc))
                            continue
                        try:
                            pg.evaluate(_SCROLL_JS)
                        except Exception:
                            pass
                        _quiesced(pending, time.time() + max(10, int(per_page_s or 30)))
                        try:
                            pg.evaluate("window.dispatchEvent(new Event('visibilitychange'))")
                            pg.wait_for_timeout(1200)
                        except Exception:
                            pass
                    finally:
                        try:
                            pg.close()
                        except Exception:
                            pass
                    print("[trace] \u2713 +%d urls (total %d)" % (len(seen) - _before, len(seen)), flush=True)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        print("[trace] browser failed (%s) — continuing static-only" % exc)
        return []

    host = urllib.parse.urlparse(origin).netloc.lower()
    urls = sorted(u for u in seen
                  if urllib.parse.urlparse(u).netloc.lower() == host)
    if outdir:
        try:
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, "urls.jsonl"), "w", encoding="utf-8") as fh:
                for u in sorted(seen):
                    e = seen[u]
                    fh.write(json.dumps({"method": e["method"], "url": u,
                                         "status": e["status"], "n": e["n"]}) + "\n")
        except OSError:
            pass
    print("[trace] runtime urls: %d same-host" % len(urls))
    return urls
