"""Stage 1 entry point: ``python -m everpage.cli <url> [-o outdir] [--port N]
[--pages route1,route2]``.

Parses args, normalizes the URL, creates the output dir, runs stage 1
(homepage + manifest + extra pages via :mod:`everpage.fetcher`), then
hands off to sibling stages (detect, chunks, rewrite, serve, verify) when
they exist. Sibling stages are built by other agents in parallel, so every
one of them is imported defensively: missing modules are reported and
skipped, never fatal.
"""

import argparse
import os
import posixpath
import re
import sys
import urllib.parse

from .fetcher import download_many, fetch_page, make_session, save

MANIFEST_CANDIDATES = ("manifest.json", "manifest.webmanifest", "site.webmanifest")


def normalize_url(raw):
    """Ensure scheme, split origin; return (page_url, origin).

    ``page_url`` keeps the path but drops any trailing slash (root "/" is
    dropped too — the fetcher maps it to index.html). ``origin`` is
    ``scheme://host[:port]``.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty URL")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if not parsed.netloc:
        raise ValueError("could not parse URL: %r" % raw)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""
    page_url = origin + path
    if parsed.query:
        page_url += "?" + parsed.query
    return page_url, origin


def default_outdir_for(origin):
    host = urllib.parse.urlparse(origin).netloc.split(":")[0]
    safe = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in host)
    return os.path.abspath((safe or "site") + "-clone")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="everpage",
        description="Clone a site in stages: fetch, detect, chunks, rewrite, serve, verify.",
    )
    parser.add_argument("url", help="site URL to clone (scheme optional)")
    parser.add_argument("-o", "--outdir", default=None, help="output directory")
    parser.add_argument("--port", type=int, default=8000, help="preview server port (serve stage)")
    parser.add_argument(
        "--pages",
        default="",
        help="extra comma-separated routes to fetch, e.g. 'about,pricing/blog'",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=200,
        help="auto-crawl page cap including homepage (default 200)",
    )
    parser.add_argument(
        "--no-crawl",
        action="store_true",
        help="clone homepage + --pages only, skip sitemap/link auto-crawl",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="skip headless runtime network capture (static scan only, faster)",
    )
    return parser.parse_args(argv)


def _optional_import(name):
    try:
        module = __import__("everpage." + name, fromlist=["*"])
        print("[stage:%s] loaded" % name)
        return module
    except ImportError as exc:
        print("[stage:%s] skipped (not available yet: %s)" % (name, exc))
    except Exception as exc:  # stub present but broken — never fatal to stage 1
        print("[stage:%s] skipped (import error: %s)" % (name, exc))
    return None


def _run_stage(module, func_names, *args):
    """Call the first existing func in ``func_names``; True when one ran."""
    if module is None:
        return False
    for func_name in func_names:
        func = getattr(module, func_name, None)
        if callable(func):
            try:
                func(*args)
                print("[stage:%s] %s() done" % (module.__name__.rsplit(".", 1)[-1], func_name))
                return True
            except TypeError as exc:
                print(
                    "[stage:%s] %s() signature mismatch (%s) — skipped"
                    % (module.__name__.rsplit(".", 1)[-1], func_name, exc)
                )
                return False
            except Exception as exc:
                print(
                    "[stage:%s] %s() failed (%s) — continuing"
                    % (module.__name__.rsplit(".", 1)[-1], func_name, exc)
                )
                return False
    print("[stage:%s] no known entry point %s — skipped" % (module.__name__, func_names))
    return False


def stage_fetch(origin, outdir, extra_routes, session=None):
    """Stage 1: homepage + manifest candidates + extra pages. Returns local paths."""
    session = session or make_session(origin + "/")
    jobs = [origin + "/"]
    for route in extra_routes:
        route = route.strip().strip("/")
        if route:
            jobs.append(origin + "/" + route)
    print("[fetch] downloading %d page(s) ..." % len(jobs))
    report = download_many([(url, outdir) for url in jobs], workers=16, referer=origin + "/")
    fetched = dict(report["ok"])

    manifests = []
    for name in MANIFEST_CANDIDATES:
        try:
            body = fetch_page(session, origin + "/" + name)
            path = save(body, outdir, "/" + name)
            manifests.append(path)
            print("[fetch] manifest OK /%s" % name)
        except Exception:
            continue
    if not manifests:
        print("[fetch] no web manifest found (tried %s)" % ", ".join(MANIFEST_CANDIDATES))
    index_path = fetched.get(origin + "/")
    return {"index": index_path, "pages": fetched, "manifests": manifests}


_FW_HINT = {
    "next-webpack": "webpack",
    "next-turbopack": "turbopack",
    "vite-spa": "vite",
    "astro": "astro",
    "svelte": "svelte",
    "static": "generic",
    "unknown": "generic",
}

_TELEMETRY_HINTS = (
    "googletagmanager", "googletagservices", "gtag/js",
    "google-analytics", "googleadservices", "doubleclick",
    "fbevents", "hotjar", "mixpanel", "segment.io",
    "fullstory", "analytics.js", "_vercel/insights",
    "insights/script.js", "gtm.js",
)
_BEACON_RE = re.compile(r"^/[0-9a-f]{8,}/script\.js", re.IGNORECASE)


def _is_telemetry(ref):
    """True when a ref is analytics/telemetry (skipped at download, stubbed at serve)."""
    low = (ref or "").lower().split("?", 1)[0]
    if any(h in low for h in _TELEMETRY_HINTS):
        return True
    return bool(_BEACON_RE.match(low))

_CSS_URL_RE = re.compile(r"""url\(\s*["']?([^\)"'\s<>]{1,300})["']?\s*\)""", re.IGNORECASE)


def _dedup(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _expand_enum(spec, cap=5000):
    urls = []
    try:
        bases = spec.get("bases") or []
        ids = spec.get("ids") or []
        exts = spec.get("exts") or []
    except AttributeError:
        return urls
    for b in bases:
        for i in ids:
            for e in exts:
                urls.append("%s%s.%s" % (b if b.endswith("/") else b + "/", i, e))
                if len(urls) >= cap:
                    return urls
    return urls


def _expand_template(spec, ids=None, num_cap=50, ids_cap=150):
    urls = []
    try:
        prefixes = spec.get("prefixes") or ([spec.get("prefix")] if spec.get("prefix") else [])
        suffix = spec.get("suffix") or ""
    except AttributeError:
        return urls
    if not suffix or "${" in suffix:
        return urls
    pcap = int(spec.get("pcap", 12) or 12)
    for prefix in prefixes[:max(1, min(pcap, 64))]:
        if not prefix or "${" in prefix or "}" in prefix:
            continue
        for i in range(0, max(0, int(num_cap or 0))):
            urls.append("%s%d%s" % (prefix, i, suffix))
        for _id in (ids or [])[:max(0, int(ids_cap or 0))]:
            urls.append("%s%s%s" % (prefix, _id, suffix))
        for _f in (spec.get("fillers") or [])[:15]:
            if _f and "${" not in _f and "}" not in _f:
                urls.append("%s%s%s" % (prefix, _f, suffix))
    return urls


def _is_junk_file(path, url):
    """True when a downloaded asset is actually an HTML fallback page.

    Some servers answer 200 + index.html for unknown paths; saving that
    as .drc/.ogg would fake success. Real assets never start with markup.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(512).lstrip()[:64].lower()
    except OSError:
        return True
    return head.startswith((b"<!doctype", b"<html"))


def _expand_frames(spec):
    try:
        tpl = spec.get("template", "")
        start = int(spec.get("start"))
        end = int(spec.get("end"))
        pad = int(spec.get("pad") or 0)
    except (TypeError, ValueError):
        return []
    if not tpl or "{}" not in tpl or end < start or (end - start) > 5000:
        return []
    urls = []
    for n in range(start, end + 1):
        num = str(n).zfill(pad) if pad else str(n)
        urls.append(tpl.replace("{}", num))
    return urls


def _is_page_route(r):
    """Navigable document route: extensionless or .html/.htm (qs/frag stripped)."""
    r = (r or "").split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
    if r == "/":
        return False
    return os.path.splitext(r)[1].lower() in ("", ".html", ".htm")


def run_pipeline(session, origin, outdir, extra_routes, fetch_result,
                 detect, chunks_m, rewrite_m, serve_m, verify_m, port,
                 crawl=True, max_pages=200, trace_m=None, trace=True):
    """Run detect -> assets -> chunks -> css -> rewrite -> serve -> verify."""
    host = urllib.parse.urlparse(origin).netloc.lower()
    index_path = fetch_result.get("index")
    with open(index_path, "r", encoding="utf-8") as fh:
        index_html = fh.read()
    framework = detect.detect_framework(index_html)
    print("[detect] framework: %s" % framework)

    pages = [("/", index_path)]
    route_map = {"/": "index.html"}
    fetched = dict((fetch_result.get("pages") or {}))
    if crawl:
        pending = []
        for sm in ("/sitemap.xml", "/sitemap_index.xml"):
            try:
                xml = fetch_page(session, origin + sm)
            except Exception:
                continue
            for m in re.finditer(r"<loc>\s*([^<>]+?)\s*</loc>", xml):
                p = urllib.parse.urlparse((m.group(1) or "").strip())
                if p.netloc.lower() == host and p.path:
                    pending.append(p.path)
        try:
            _seed_ax = detect.extract_assets(index_html, origin)
        except Exception:
            _seed_ax = {}
        pending.extend(_seed_ax.get("routes") or [])
        for _lk in (_seed_ax.get("links") or []):
            _lk = (_lk or "").strip()
            if _lk.startswith(origin):
                _lk = _lk[len(origin):]
            if not _lk.startswith("/") or _is_telemetry(_lk):
                continue
            pending.append(_lk)
        queue = []
        for route in list(extra_routes) + pending:
            _rt = (route or "").strip()
            if _rt.startswith(origin):
                _rt = _rt[len(origin):]
            if not _rt or "://" in _rt or _is_telemetry(_rt):
                continue
            r = "/" + _rt.lstrip("./").strip("/")
            r = r.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
            if not _is_page_route(r) or r in queue:
                continue
            queue.append(r)
        idx, limit = 0, max(2, int(max_pages or 200))
        while idx < len(queue) and len(fetched) < limit:
            r = queue[idx]
            idx += 1
            if origin + r in fetched:
                continue
            try:
                text = fetch_page(session, origin + r)
            except Exception:
                print("[crawl] skip %s (fetch failed)" % r)
                continue
            try:
                fetched[origin + r] = save(text, outdir, r)
            except OSError:
                continue
            try:
                _ax = detect.extract_assets(text, origin)
            except Exception:
                _ax = {}
            for nxt in list(_ax.get("routes") or []) + list(_ax.get("links") or []):
                _nx = (nxt or "").strip()
                if _nx.startswith(origin):
                    _nx = _nx[len(origin):]
                if not _nx or "://" in _nx or _is_telemetry(_nx):
                    continue
                n = "/" + _nx.lstrip("./").strip("/")
                n = n.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
                if not _is_page_route(n):
                    continue
                if n in queue or origin + n in fetched:
                    continue
                queue.append(n)
        print("[crawl] pages: %d" % len(fetched))
    for full_url, src in list(fetched.items()):
        p = urllib.parse.urlparse(full_url).path or "/"
        r = p.rstrip("/") or "/"
        if r == "/":
            continue  # homepage already in pages
        if not _is_page_route(r):
            continue  # file asset, not a page
        if not src or not os.path.exists(src):
            print("[fetch] note: extra page %s not downloaded" % r)
            continue
        rel = rewrite_m.default_route_file(r)
        dst = os.path.join(outdir, *rel.split("/"))
        if os.path.abspath(src) != os.path.abspath(dst):
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.replace(src, dst)
        route_map[r] = rel
        pages.append((r, dst))

    trace_urls = []
    if trace and trace_m is not None and hasattr(trace_m, "trace_urls"):
        try:
            trace_urls = trace_m.trace_urls(origin, [r for r, _p in pages], outdir) or []
        except Exception as exc:
            print("[trace] failed (%s) — continuing static-only" % exc)
            trace_urls = []

    all_refs = []
    for _route, path in pages:
        with open(path, "r", encoding="utf-8") as fh:
            assets = detect.extract_assets(fh.read(), origin)
        for key in ("scripts", "stylesheets", "images", "fonts",
                    "videos", "data_attrs", "links"):
            all_refs.extend(assets.get(key) or [])
    jobs = []
    for ref in _dedup(all_refs + trace_urls):
        if not ref or "/_next/image?" in ref or _is_telemetry(ref):
            continue
        full = urllib.parse.urljoin(origin + "/", ref)
        if urllib.parse.urlparse(full).netloc.lower() == host:
            jobs.append(full)
    print("[assets] downloading %d refs ..." % len(jobs))
    arep = download_many(jobs, outdir, workers=16, referer=origin + "/")
    print("[assets] ok=%d fail=%d" % (len(arep["ok"]), len(arep["fail"])))

    entry_js = [u for u in _dedup(all_refs)
                if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".js")]
    hint = _FW_HINT.get(framework, "generic")
    found = chunks_m.crawl_bundles(session, origin, entry_js, framework=hint,
                                    out_dir=outdir, save=True)
    dl = []
    pool: list = []
    tmps: list = []
    for item in found or []:
        if isinstance(item, dict) and item.get("type") == "ids":
            for _id in (item.get("ids") or [])[:400]:
                _s = str(_id or "")
                if not _s or re.fullmatch(r"_+", _s):
                    continue
                if _s not in pool:
                    pool.append(_s)
                if len(pool) >= 800:
                    break
        elif isinstance(item, dict) and item.get("type") == "template":
            tmps.append(item)
    pool = pool[:800]
    live_prefix: dict = {}
    probe_map: dict = {}
    for ti, item in enumerate(tmps):
        try:
            prefixes = item.get("prefixes") or []
            suffix = item.get("suffix") or ""
            fillers = item.get("fillers") or []
            pcap = int(item.get("pcap", 12) or 12)
        except AttributeError:
            continue
        if not suffix or "${" in suffix:
            continue
        for p in prefixes[:max(1, min(pcap, 64))]:
            if not p or "${" in p or "}" in p:
                continue
            _spread = []
            if pool:
                _n = len(pool)
                _idxs = {0, 1}
                _idxs.update(round(_n * i / 25) for i in range(1, 25))
                _idxs.add(_n - 1)
                for _idx in sorted(_idxs):
                    if 0 <= _idx < _n and str(pool[_idx]) not in _spread:
                        _spread.append(str(pool[_idx]))
            _probes = ["0", "1"] + _spread
            for _f in (fillers or [])[:3]:
                if _f and "${" not in _f and "}" not in _f:
                    _probes.append(str(_f))
            for _probe in _probes:
                full = urllib.parse.urljoin(origin + "/", "%s%s%s" % (p, _probe, suffix))
                if urllib.parse.urlparse(full).netloc.lower() != host:
                    continue
                probe_map.setdefault(full, []).append((ti, p))
    if probe_map:
        print("[tpl] probing %d prefixes ..." % len(probe_map))
        rep = download_many(sorted(probe_map), outdir, workers=16, referer=origin + "/")
        for url, path in rep["ok"]:
            if _is_junk_file(path, url):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            for ti, p in probe_map[url]:
                live_prefix.setdefault(ti, [])
                if p not in live_prefix[ti]:
                    live_prefix[ti].append(p)
    print("[tpl] live: %d/%d templates, pool=%d" % (
        sum(1 for ti in range(len(tmps)) if live_prefix.get(ti)),
        len(tmps), len(pool)))
    _ppool = list(pool[:20])
    if len(pool) > 20:
        _step = max(1, len(pool) // 40)
        for _i in range(20, len(pool), _step):
            if pool[_i] not in _ppool:
                _ppool.append(pool[_i])
            if len(_ppool) >= 60:
                break
    for ti, item in enumerate(tmps):
        prefixes = live_prefix.get(ti, [])
        if not prefixes:
            continue
        sub = {"type": "template", "prefixes": prefixes,
               "suffix": item.get("suffix"), "fillers": item.get("fillers"),
               "pcap": item.get("pcap", 12)}
        for u in _expand_template(sub, pool, num_cap=30):
            if not isinstance(u, str) or not u or "/_next/image?" in u or _is_telemetry(u):
                continue
            full = urllib.parse.urljoin(origin + "/", u)
            if urllib.parse.urlparse(full).netloc.lower() == host:
                dl.append(full)
    for item in found or []:
        if isinstance(item, dict) and item.get("type") in ("template", "ids"):
            continue
        elif isinstance(item, dict) and item.get("type") == "enum":
            cands = _expand_enum(item)
        else:
            cands = _expand_frames(item) if isinstance(item, dict) else [item]
        for u in cands:
            if not isinstance(u, str) or not u or "/_next/image?" in u or _is_telemetry(u):
                continue
            full = urllib.parse.urljoin(origin + "/", u)
            if urllib.parse.urlparse(full).netloc.lower() == host:
                dl.append(full)
    dl = _dedup(dl)
    print("[chunks] downloading %d discovered refs ..." % len(dl))
    crep = download_many(dl, outdir, workers=16, referer=origin + "/") if dl else {"ok": [], "fail": []}
    print("[chunks] ok=%d fail=%d" % (len(crep["ok"]), len(crep["fail"])))
    _junk = 0
    _good = []
    for url, path in crep["ok"]:
        if _is_junk_file(path, url):
            try:
                os.remove(path)
            except OSError:
                pass
            _junk += 1
        else:
            _good.append((url, path))
    crep["ok"] = _good
    if _junk:
        print("[chunks] dropped %d HTML-fallback impostors" % _junk)
    seg_jobs = []
    for u in _dedup(jobs + dl):
        if u.split("?", 1)[0].lower().endswith(".m3u8"):
            try:
                txt = fetch_page(session, u)
            except Exception:
                continue
            for line in txt.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                full = urllib.parse.urljoin(u, line)
                if urllib.parse.urlparse(full).netloc.lower() == host:
                    seg_jobs.append(full)
    seg_jobs = _dedup(seg_jobs)
    if seg_jobs:
        print("[hls] downloading %d segments ..." % len(seg_jobs))
        download_many(seg_jobs, outdir, workers=16, referer=origin + "/")

    css_files = []
    for root, _dirs, files in os.walk(outdir):
        for fn in files:
            if fn.lower().endswith(".css"):
                css_files.append(os.path.join(root, fn))
    css_jobs = []
    for cp in sorted(css_files):
        rel = os.path.relpath(cp, outdir).replace(os.sep, "/")
        base = origin + "/" + posixpath.dirname(rel) + "/"
        try:
            text = open(cp, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _CSS_URL_RE.finditer(text):
            ref = (m.group(1) or "").strip()
            if not ref or ref.startswith(("data:", "blob:", "http://", "https://", "//", "#", "%23")):
                continue
            full = urllib.parse.urljoin(base, ref)
            if urllib.parse.urlparse(full).netloc.lower() == host:
                css_jobs.append(full)
    css_jobs = _dedup(css_jobs)
    if css_jobs:
        print("[css] downloading %d url() refs ..." % len(css_jobs))
        download_many(css_jobs, outdir, workers=16, referer=origin + "/")

    for _round in range(2):
        before = sum(len(f) for _r, _d, f in os.walk(outdir))
        deep_jobs, foreign = [], set()
        for root, _dirs, files in os.walk(outdir):
            for fn in files:
                low = fn.lower()
                fp = os.path.join(root, fn)
                try:
                    if not (low.endswith((".gltf", ".glb", ".splinecode")) or (low.endswith(".json") and os.path.getsize(fp) < 2000000)):
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(fp, outdir).replace(os.sep, "/")
                furl = origin + "/" + rel
                try:
                    with open(fp, "rb") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                for ref in chunks_m.expand_deep_file(furl, blob):
                    if not ref or ref.startswith("data:"):
                        continue
                    full = urllib.parse.urljoin(furl, ref)
                    if urllib.parse.urlparse(full).netloc.lower() == host:
                        deep_jobs.append(full)
                    else:
                        foreign.add(full.split("?", 1)[0])
        deep_jobs = _dedup(deep_jobs)
        if deep_jobs:
            print("[deep] downloading %d nested refs ..." % len(deep_jobs))
            download_many(deep_jobs, outdir, workers=16, referer=origin + "/")
        if foreign:
            try:
                with open(os.path.join(outdir, "foreign.txt"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(sorted(foreign)) + "\n")
            except OSError:
                pass
            print("[deep] %d third-party refs listed in foreign.txt (not downloaded)" % len(foreign))
        after = sum(len(f) for _r, _d, f in os.walk(outdir))
        if after == before:
            break

    for cp in sorted(css_files):
        rel = os.path.relpath(cp, outdir).replace(os.sep, "/")
        depth = len([p for p in posixpath.dirname(rel).split("/") if p])
        try:
            text = open(cp, "r", encoding="utf-8", errors="replace").read()
            with open(cp, "w", encoding="utf-8") as fh:
                fh.write(rewrite_m.rewrite_css(text, css_depth=depth, origin=origin))
        except OSError as exc:
            print("[rewrite] css %s: %s" % (rel, exc))
    problems = {}
    for route, path in pages:
        rel = os.path.relpath(path, outdir).replace(os.sep, "/")
        d = posixpath.dirname(rel)
        depth = 0 if not d else len([p for p in d.split("/") if p])
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        out, missing = rewrite_m.rewrite_page(html, depth, route_map, framework, origin)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(out)
        probs = rewrite_m.verify_paths(out, outdir, page_file=rel, framework=framework, origin=origin)
        if missing or probs:
            problems[route] = {"missing_routes": missing, "path_problems": probs}
    if problems:
        print("[rewrite] problems: %s" % problems)
    else:
        print("[rewrite] all pages clean")
    _bt, _br = rewrite_m.rewrite_bundle_origins(outdir, origin)
    print("[rewrite] bundle origins: %d files, %d urls" % (_bt, _br))

    _tok_colors, _tok_fonts = set(), set()
    for _cp in sorted(css_files):
        try:
            _ct = open(_cp, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        _tok_colors.update(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", _ct))
        for _fm in re.finditer(r"font-family\s*:\s*([^;}{]+)", _ct, re.IGNORECASE):
            _tok_fonts.update(x.strip().strip("\"'") for x in _fm.group(1).split(",") if x.strip())
    try:
        import json as _json
        with open(os.path.join(outdir, "tokens.json"), "w", encoding="utf-8") as _fh:
            _fh.write(_json.dumps({"colors": sorted(_tok_colors), "fonts": sorted(_tok_fonts)}, indent=1))
    except OSError:
        pass
    print("[tokens] colors=%d fonts=%d -> tokens.json" % (len(_tok_colors), len(_tok_fonts)))

    routes = [r for r, _p in pages]
    server_path = serve_m.write_server(outdir, routes, {}, port)
    serve_m.write_launcher(outdir, port)
    print("[serve] %s + OPEN-ME.bat" % server_path)
    report, exit_code = verify_m.verify_clone(outdir, port, routes)
    print("[verify] errors=%d failed=%d shots=%d %s" % (
        len(report.get("errors", [])), len(report.get("failed", [])),
        len(report.get("shots", [])), report.get("skipped", "")))
    for e in report.get("errors", [])[:10]:
        print("  ERR " + str(e)[:200])
    for f in report.get("failed", [])[:10]:
        print("  FAIL " + str(f)[:200])
    return exit_code


def main(argv=None):
    args = parse_args(argv)
    try:
        page_url, origin = normalize_url(args.url)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    outdir = os.path.abspath(args.outdir) if args.outdir else default_outdir_for(origin)
    os.makedirs(outdir, exist_ok=True)
    print("everpage -> %s" % page_url)
    print("origin    : %s" % origin)
    print("outdir    : %s" % outdir)

    extra_routes = [r for r in (args.pages or "").split(",") if r.strip()]
    try:
        result = stage_fetch(origin, outdir, extra_routes)
    except Exception as exc:
        print("[fetch] FAILED: %s" % exc, file=sys.stderr)
        return 1
    if not result.get("index"):
        print("[fetch] FAILED: homepage download failed", file=sys.stderr)
        return 1
    print("[fetch] homepage -> %s" % result["index"])

    # Full pipeline — all modules are landed.
    detect = _optional_import("detect")
    chunks_m = _optional_import("chunks")
    rewrite_m = _optional_import("rewrite")
    serve_m = _optional_import("serve")
    verify_m = _optional_import("verify")
    trace_m = _optional_import("trace")
    if detect is None or chunks_m is None or rewrite_m is None or serve_m is None or verify_m is None:
        print("[everpage] pipeline module missing; clone incomplete", file=sys.stderr)
        return 1
    session = make_session(origin + "/")
    try:
        code = run_pipeline(session, origin, outdir, extra_routes, result,
                            detect, chunks_m, rewrite_m, serve_m, verify_m, args.port,
                            crawl=not args.no_crawl, max_pages=args.max_pages,
                            trace_m=trace_m, trace=not args.no_trace)
    except Exception as exc:
        print("[everpage] FAILED: %s" % exc, file=sys.stderr)
        return 1
    print("DONE: %s" % outdir)
    return code


def console_entry():
    """Entry point for the ``everpage`` console script (preserves exit code)."""
    raise SystemExit(main())


if __name__ == "__main__":
    sys.exit(main())
