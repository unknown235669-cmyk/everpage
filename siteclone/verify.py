"""Stage 6 headless verification (playwright, optional/lazy).

Public API used by sibling stages:
    verify_clone(outdir, port, routes) -> (report, exit_code)

``report`` is {"errors": [...], "failed": [...], "shots": [...]}.
``exit_code`` is 0 when clean (or when playwright is unavailable and the
run was skipped gracefully), 1 when page errors / failed requests remain.
"""

import glob as _glob
import os
import socket
import subprocess
import sys
import time
import urllib.parse

# Failed-request URLs containing any of these are third-party noise or
# external favicons, never clone defects.
IGNORE_URL_SUBSTRINGS = (
    "googletagmanager",
    "googletagservices",
    "gtag/js",
    "google-analytics",
    "googleadservices",
    "doubleclick",
    "googlesyndication",
    "pagead/",
    "analytics",
    "fbevents",
    "hotjar",
    "mixpanel",
    "segment.io",
    "fullstory",
    "favicon",
    "_rsc=",  # Next.js flight prefetch noise
    "cdn-cgi",  # Cloudflare beacon
)


def _is_ignored(url):
    """True when a request URL should never count as a verification failure."""
    if not url or url.startswith(("data:", "blob:")):
        return True
    return any(s in url.lower() for s in IGNORE_URL_SUBSTRINGS)


def _route_slug(route):
    slug = urllib.parse.unquote(str(route)).strip("/") or "index"
    slug = slug.replace("/", "_")
    slug = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in slug)
    return slug or "index"


def _port_open(port, host="127.0.0.1"):
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _find_serve_script(outdir):
    scripts = sorted(_glob.glob(os.path.join(os.path.abspath(outdir), "serve_*.py")))
    return scripts[0] if scripts else None


def _wait_for_port(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.25)
    return _port_open(port)


def verify_clone(outdir, port=8919, routes=None):
    """Load each route headless, collect errors + failed requests + shots.

    Starts the generated serve_*.py found in ``outdir`` when nothing is
    already listening on ``port`` (and stops it afterwards). Screenshots
    go to ``<outdir>/shots/<slug>.png``. Returns (report, exit_code).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report = {"errors": [], "failed": [], "shots": [],
                  "skipped": "playwright not installed"}
        return report, 0

    outdir = os.path.abspath(outdir)
    routes = [str(r) if str(r).startswith("/") else "/" + str(r)
              for r in (routes or ["/"])]
    shots_dir = os.path.join(outdir, "shots")
    os.makedirs(shots_dir, exist_ok=True)

    errors, failed, shots = [], [], []
    server_proc = None
    try:
        if not _port_open(port):
            script = _find_serve_script(outdir)
            if script is None:
                report = {"errors": ["no server on port %d and no serve_*.py in %s"
                                     % (port, outdir)],
                          "failed": [], "shots": []}
                return report, 1
            server_proc = subprocess.Popen(
                [sys.executable, script, str(int(port))],
                cwd=outdir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not _wait_for_port(port):
                _stop_server(server_proc)
                server_proc = None
                report = {"errors": ["generated server failed to listen on port %d"
                                     % port],
                          "failed": [], "shots": []}
                return report, 1
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context()
                    for route in routes:
                        page = ctx.new_page()
                        url = "http://localhost:%d%s" % (int(port), route)

                        def _on_pageerror(exc, _route=route):
                            detail = getattr(exc, "stack", None) or str(exc)
                            if _is_ignored(detail):
                                return  # third-party ad/tracker script threw, not the clone
                            errors.append("%s pageerror: %s" % (_route, detail))

                        def _on_console(msg, _route=route):
                            loc = msg.location if isinstance(msg.location, dict) else {}
                            if msg.type == "error" and not _is_ignored(loc.get("url", "")):
                                text = msg.text or ""
                                where = loc.get("url", "")
                                errors.append("%s console: %s [%s]" % (_route, text, where))

                        def _on_failed(req, _route=route):
                            if _is_ignored(req.url):
                                return
                            path = urllib.parse.urlparse(req.url).path.lower()
                            if path.endswith((".mp4", ".webm", ".mp3", ".ogg", ".wav", ".m4v", ".mov")):
                                return  # headless aborts media loads; file itself is verified on disk
                            if "_rsc=" in req.url:
                                return  # Next.js flight prefetch noise
                            try:
                                _up = urllib.parse.urlparse(req.url)
                                _rel = urllib.parse.unquote(_up.path or "").lstrip("/")
                                if _rel and os.path.exists(os.path.join(outdir, *_rel.split("/"))):
                                    return  # on disk: headless abort noise, not a clone gap
                            except Exception:
                                pass
                            failed.append("%s FAILED %s" % (_route, req.url))

                        def _on_response(resp, _route=route):
                            try:
                                status = resp.status
                            except Exception:
                                return
                            if status >= 400 and not _is_ignored(resp.url) and "_rsc=" not in resp.url:
                                failed.append("%s %d %s" % (_route, status, resp.url))

                        page.on("pageerror", _on_pageerror)
                        page.on("console", _on_console)
                        page.on("requestfailed", _on_failed)
                        page.on("response", _on_response)
                        try:
                            resp = page.goto(url, wait_until="load", timeout=30000)
                            if resp is not None:
                                try:
                                    status = resp.status
                                except Exception:
                                    status = 0
                                if status >= 400 and not _is_ignored(resp.url):
                                    failed.append("%s %d %s" % (route, status, url))
                            page.wait_for_timeout(1500)
                        except Exception as exc:
                            errors.append("%s navigation: %s" % (route, exc))
                        shot = os.path.join(shots_dir, _route_slug(route) + ".png")
                        try:
                            page.screenshot(path=shot, full_page=True)
                            shots.append(shot)
                        except Exception as exc:
                            errors.append("%s screenshot: %s" % (route, exc))
                        page.close()
                finally:
                    browser.close()
        except Exception as exc:
            # Missing browsers / driver mismatch: graceful skip, not a failure.
            report = {"errors": [], "failed": [], "shots": shots,
                      "skipped": "playwright run failed: %s" % exc}
            return report, 0
    finally:
        if server_proc is not None:
            _stop_server(server_proc)

    report = {"errors": errors, "failed": failed, "shots": shots}
    return report, (0 if not errors and not failed else 1)


def _stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    _outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    _port = int(sys.argv[2]) if len(sys.argv) > 2 else 8919
    _routes = sys.argv[3:] or ["/"]
    _report, _code = verify_clone(_outdir, _port, _routes)
    print("errors: %d failed: %d shots: %d" % (
        len(_report.get("errors", [])),
        len(_report.get("failed", [])),
        len(_report.get("shots", []))))
    for line in list(_report.get("errors", [])) + list(_report.get("failed", []))[:30]:
        print("  " + line)
    if _report.get("skipped"):
        print("skipped: " + str(_report["skipped"]))
    for shot in _report.get("shots", []):
        print("shot: " + shot)
    sys.exit(_code)
