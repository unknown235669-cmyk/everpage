"""Interactive visual CLI for siteclone: menus, prompts, live progress.

Run:  ``python -m siteclone.tui``  (from the folder containing ``siteclone/``)

No third-party deps — stdlib only, works in any Windows terminal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import webbrowser

from . import cli as _cli
from .serve import write_launcher, write_server
from .verify import verify_clone

BANNER = r"""
  ____  _ _       _____ _
 / ___|(_) |_ ___|_   _| | ___  _ __   ___
 \___ \| | __/ _ \ | | | |/ _ \| '_ \ / _ \
  ___) | | ||  __/ | | | | (_) | | | |  __/
 |____/|_|\__\___| |_| |_|\___/|_| |_|\___|
  paste a link -> get a 1:1 offline clone
"""


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _ask(prompt, default=None):
    if default is not None:
        raw = input("%s [%s]: " % (prompt, default)).strip()
        return raw or str(default)
    return input("%s: " % prompt).strip()


def _ask_int(prompt, default):
    while True:
        raw = _ask(prompt, default)
        try:
            return int(raw)
        except ValueError:
            print("  enter a number, e.g. %s" % default)


def _ask_yes_no(prompt, default_yes=True):
    hint = "Y/n" if default_yes else "y/N"
    raw = input("%s [%s]: " % (prompt, hint)).strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def _find_clones():
    """Candidate clone folders: *-clone dirs with an index.html, cwd first."""
    roots = [os.getcwd(),
             os.path.abspath(os.path.join(os.getcwd(), os.pardir))]
    found = []
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            full = os.path.join(root, name)
            if (name.endswith("-clone") and os.path.isdir(full)
                    and os.path.exists(os.path.join(full, "index.html"))
                    and full not in found):
                found.append(full)
    return found


def _pick_clone():
    clones = _find_clones()
    if clones:
        print("\nFound clones:")
        for i, path in enumerate(clones, 1):
            print("  %d) %s" % (i, path))
        raw = input("Pick one [1-%d] or paste a path: " % len(clones)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(clones):
            return clones[int(raw) - 1]
        if raw:
            return os.path.abspath(raw)
        return clones[0]
    return os.path.abspath(_ask("Clone folder path"))


def _summarize(outdir):
    total_files, total_bytes = 0, 0
    for _root, _dirs, files in os.walk(outdir):
        for fn in files:
            fp = os.path.join(_root, fn)
            try:
                total_bytes += os.path.getsize(fp)
                total_files += 1
            except OSError:
                pass
    colors, fonts = 0, 0
    tok = os.path.join(outdir, "tokens.json")
    if os.path.exists(tok):
        try:
            with open(tok, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            colors, fonts = len(data.get("colors", [])), len(data.get("fonts", []))
        except (OSError, ValueError):
            pass
    pages = 0
    for _root, _dirs, files in os.walk(outdir):
        pages += sum(1 for fn in files if fn.lower().endswith(".html")
                     and "shots" not in _root.replace(os.sep, "/"))
    mb = total_bytes / (1024 * 1024)
    print("\n  files   : %d (%.1f MB)" % (total_files, mb))
    print("  pages   : %d html" % pages)
    print("  tokens  : %d colors, %d fonts" % (colors, fonts))


def _serve_forever(outdir, port):
    routes = ["/"]
    write_server(outdir, routes, {}, port)
    bat = write_launcher(outdir, port)
    url = "http://localhost:%d/" % int(port)
    print("\n  launcher: %s" % bat)
    print("  serving : %s  (Enter stops it)" % url)
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(int(port)), "--bind", "127.0.0.1"],
        cwd=outdir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        webbrowser.open(url)
        input("  press Enter to stop the server ... ")
    except KeyboardInterrupt:
        print()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
    print("  server stopped.")


def do_clone():
    _clear()
    print(BANNER)
    print("--- clone a website ---\n")
    while True:
        url = _ask("Paste the site link")
        try:
            _page, origin = _cli.normalize_url(url)
            break
        except ValueError as exc:
            print("  bad link (%s) — try again" % exc)
    outdir = os.path.abspath(_ask("Save folder",
                                  os.path.basename(_cli.default_outdir_for(origin))))
    port = _ask_int("Local port", 8919)
    crawl = _ask_yes_no("Crawl every page (sitemap + links)?", True)
    max_pages, extra, argv = 50, "", [url, "-o", outdir, "--port", str(port)]
    if crawl:
        max_pages = _ask_int("Max pages to crawl", 50)
        argv += ["--max-pages", str(max_pages)]
    else:
        argv += ["--no-crawl"]
    if _ask_yes_no("Runtime trace (headless pass, catches hidden assets, slower)?", True):
        pass
    else:
        argv += ["--no-trace"]
    extra = _ask("Extra routes (comma-separated, or Enter to skip)", "")
    if extra:
        argv += ["--pages", extra]
    print("\n  cloning %s ..." % origin)
    print("  ─────────────────────────────────")
    try:
        rc = _cli.main(argv)
    except KeyboardInterrupt:
        print("\n  cancelled.")
        return
    print("  ─────────────────────────────────")
    if rc == 0:
        print("\n  DONE — clone verified clean.")
    else:
        print("\n  DONE with warnings (exit %d) — see [verify]/[rewrite] lines above." % rc)
    _summarize(outdir)
    if _ask_yes_no("\nOpen it in the browser now?", True):
        _serve_forever(outdir, port)
    input("\nEnter to return to menu ... ")


def do_serve():
    _clear()
    print(BANNER)
    print("--- serve a clone ---\n")
    outdir = _pick_clone()
    if not os.path.exists(os.path.join(outdir, "index.html")):
        print("  no index.html in %s" % outdir)
        input("\nEnter to return to menu ... ")
        return
    _serve_forever(outdir, _ask_int("Local port", 8919))
    input("\nEnter to return to menu ... ")


def do_verify():
    _clear()
    print(BANNER)
    print("--- verify a clone ---\n")
    outdir = _pick_clone()
    port = _ask_int("Local port", 8919)
    print("\n  verifying %s ..." % outdir)
    print("  ─────────────────────────────────")
    try:
        report, code = verify_clone(outdir, port, ["/"])
    except KeyboardInterrupt:
        print("\n  cancelled.")
        return
    print("  ─────────────────────────────────")
    print("  errors=%d failed=%d shots=%d %s" % (
        len(report.get("errors", [])), len(report.get("failed", [])),
        len(report.get("shots", [])), report.get("skipped", "")))
    for e in report.get("errors", [])[:10]:
        print("  ERR " + str(e)[:200])
    for f in report.get("failed", [])[:10]:
        print("  FAIL " + str(f)[:200])
    print("  RESULT: %s" % ("CLEAN ✅" if code == 0 else "ISSUES ❌ (exit %d)" % code))
    input("\nEnter to return to menu ... ")


def main():
    while True:
        _clear()
        print(BANNER)
        print("  1) Clone a website  (paste link -> 1:1 offline copy)")
        print("  2) Serve a clone   (open an existing clone in browser)")
        print("  3) Verify a clone  (headless check + screenshots)")
        print("  4) Quit\n")
        choice = input("Pick [1-4]: ").strip()
        if choice == "1":
            do_clone()
        elif choice == "2":
            do_serve()
        elif choice == "3":
            do_verify()
        elif choice == "4":
            print("bye bro 👋")
            return
        else:
            print("  1-4 only bro.")
            input("Enter to continue ... ")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye bro 👋")
