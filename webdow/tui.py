"""WebDow — interactive visual CLI: menus, prompts, live progress.

Run:  ``python -m webdow.tui``  (from the folder containing ``webdow/``)

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

try:
    os.system("")  # enable ANSI colours on Windows 10+
except Exception:
    pass

C_RST = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"


def _supports_color():
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() or os.name == "nt"


_USE_COLOR = _supports_color()


def _c(code, text):
    return "%s%s%s" % (code, text, C_RST) if _USE_COLOR else str(text)


BANNER = r"""
 __        __   _     ____
 \ \      / /__| |__ |  _ \  _____      __
  \ \ /\ / / _ \ '_ \| | | |/ _ \ \ /\ / /
   \ V  V /  __/ |_) | |_| | (_) \ V  V /
    \_/\_/ \___|_.__/|____/ \___/ \_/\_/
"""

TAGLINE = "paste a link  →  get a 1:1 offline clone"


def _rule(width=46, char="─"):
    print(_c(C_DIM, "  " + char * width))


def _hero(subtitle=None):
    print(_c(C_CYAN + C_BOLD, BANNER.rstrip("\n")))
    print(_c(C_DIM, "  " + TAGLINE))
    if subtitle:
        print(_c(C_MAGENTA, "\n  %s" % subtitle))


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _ask(prompt, default=None):
    label = _c(C_BOLD, prompt)
    if default is not None:
        raw = input("%s %s: " % (label, _c(C_DIM, "[%s]" % default))).strip()
        return raw or str(default)
    return input("%s: " % label).strip()


def _ask_int(prompt, default):
    while True:
        raw = _ask(prompt, default)
        try:
            return int(raw)
        except ValueError:
            print(_c(C_YELLOW, "  enter a number, e.g. %s" % default))


def _ask_yes_no(prompt, default_yes=True):
    hint = _c(C_DIM, "[Y/n]" if default_yes else "[y/N]")
    raw = input("%s %s: " % (_c(C_BOLD, prompt), hint)).strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def _ok(msg):
    print(_c(C_GREEN, "  ✓ " + msg))


def _warn(msg):
    print(_c(C_YELLOW, "  ! " + msg))


def _err(msg):
    print(_c(C_RED, "  ✗ " + msg))


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
        print(_c(C_BOLD, "\n  Found clones:"))
        for i, path in enumerate(clones, 1):
            print("  %s  %s" % (_c(C_CYAN, "%d)" % i), path))
        raw = input("  Pick one [1-%d] or paste a path: " % len(clones)).strip()
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
    print(_c(C_BOLD, "\n  Clone summary"))
    _rule(30)
    print("  files   : %s" % _c(C_CYAN, "%d (%.1f MB)" % (total_files, mb)))
    print("  pages   : %s html" % _c(C_CYAN, str(pages)))
    print("  tokens  : %s colors, %s fonts" % (_c(C_CYAN, str(colors)), _c(C_CYAN, str(fonts))))


def _serve_forever(outdir, port):
    routes = ["/"]
    write_server(outdir, routes, {}, port)
    bat = write_launcher(outdir, port)
    url = "http://localhost:%d/" % int(port)
    print("\n  launcher: %s" % bat)
    print("  serving : %s  %s" % (_c(C_CYAN, url), _c(C_DIM, "(Enter stops it)")))
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
    _hero("clone a website")
    print()
    while True:
        url = _ask("Paste the site link")
        try:
            _page, origin = _cli.normalize_url(url)
            break
        except ValueError as exc:
            _err("bad link (%s) — try again" % exc)
    outdir = os.path.abspath(_ask("Save folder",
                                  os.path.basename(_cli.default_outdir_for(origin))))
    port = _ask_int("Local port", 8919)
    crawl = _ask_yes_no("Crawl every page (sitemap + links)?", True)
    max_pages, extra, argv = 200, "", [url, "-o", outdir, "--port", str(port)]
    if crawl:
        max_pages = _ask_int("Max pages to crawl", 200)
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
    print(_c(C_BOLD, "\n  cloning %s ..." % origin))
    _rule()
    try:
        rc = _cli.main(argv)
    except KeyboardInterrupt:
        print(_c(C_YELLOW, "\n  cancelled."))
        return
    _rule()
    if rc == 0:
        _ok("DONE — clone verified clean.")
    else:
        _warn("DONE with warnings (exit %d) — see [verify]/[rewrite] lines above." % rc)
    _summarize(outdir)
    if _ask_yes_no("\nOpen it in the browser now?", True):
        _serve_forever(outdir, port)
    input(_c(C_DIM, "\nEnter to return to menu ... "))


def do_serve():
    _clear()
    _hero("serve a clone")
    print()
    outdir = _pick_clone()
    if not os.path.exists(os.path.join(outdir, "index.html")):
        _err("no index.html in %s" % outdir)
        input(_c(C_DIM, "\nEnter to return to menu ... "))
        return
    _serve_forever(outdir, _ask_int("Local port", 8919))
    input(_c(C_DIM, "\nEnter to return to menu ... "))


def do_verify():
    _clear()
    _hero("verify a clone")
    print()
    outdir = _pick_clone()
    port = _ask_int("Local port", 8919)
    print(_c(C_BOLD, "\n  verifying %s ..." % outdir))
    _rule()
    try:
        report, code = verify_clone(outdir, port, ["/"])
    except KeyboardInterrupt:
        print(_c(C_YELLOW, "\n  cancelled."))
        return
    _rule()
    print("  errors=%s failed=%s shots=%s %s" % (
        _c(C_RED if report.get("errors") else C_GREEN, str(len(report.get("errors", [])))),
        _c(C_RED if report.get("failed") else C_GREEN, str(len(report.get("failed", [])))),
        _c(C_CYAN, str(len(report.get("shots", [])))),
        report.get("skipped", "")))
    for e in report.get("errors", [])[:10]:
        print(_c(C_RED, "  ERR " + str(e)[:200]))
    for f in report.get("failed", [])[:10]:
        print(_c(C_YELLOW, "  FAIL " + str(f)[:200]))
    if code == 0:
        _ok("RESULT: CLEAN")
    else:
        _err("RESULT: ISSUES (exit %d)" % code)
    input(_c(C_DIM, "\nEnter to return to menu ... "))


def main():
    while True:
        _clear()
        _hero()
        print()
        print("  %s  Clone a website   %s" % (_c(C_CYAN + C_BOLD, "1)"), _c(C_DIM, "paste link → 1:1 offline copy")))
        print("  %s  Serve a clone     %s" % (_c(C_CYAN + C_BOLD, "2)"), _c(C_DIM, "open an existing clone in browser")))
        print("  %s  Verify a clone    %s" % (_c(C_CYAN + C_BOLD, "3)"), _c(C_DIM, "headless check + screenshots")))
        print("  %s  Quit" % _c(C_CYAN + C_BOLD, "4)"))
        print()
        choice = input("  Pick %s: " % _c(C_BOLD, "[1-4]")).strip()
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
            _warn("1-4 only bro.")
            input(_c(C_DIM, "Enter to continue ... "))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye bro 👋")
