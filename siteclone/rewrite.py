"""Offline path rewriting + route mapping (siteclone pipeline stage 4).

Ports the proven per-site one-off scripts into one reusable module:

* ``rewrite_sarang.py`` -- root-absolute -> relative, ``/_next/image`` -> local,
  nav routes -> ``.html``, CSS media urls, srcset ``", /"`` leftovers.
* ``rewrite_alche.py`` / ``rewrite2_alche.py`` -- longest-match-first route map,
  depth-relative targets, missing-target reports.
* ``rewrite_arif.py`` -- sibling-file rename strategy for routes that collide
  with directories (``/projects/lead-unity`` -> ``projects/lead-unity.html``),
  image-optimizer query decoding, leftover scans.
* ``repair_cj.py`` -- ``/_next/image?url=%2F...`` -> local file, srcset
  ``", /path"`` leftover fix.
* ``verify_cj.py`` -- resolve every local ref against the output dir.

Known bug lessons baked in (each has a regression test in :func:`self_test`)::

* **Stripped attribute names** -- an attribute pattern such as ``src`` must
  never match as a suffix of a longer name (``data-src``, ``srcset``) and must
  re-emit the attribute name byte-identical.  Achieved with an exact-name
  alternation (``srcset`` listed before ``src``), a ``(?<![\\w\\-.])`` guard,
  and re-emitting the captured name/whitespace/quote characters.
* **Glued ``./index.html`` prefix** -- a naive ``str.replace`` of the
  empty-path route ``/`` glues the index filename onto longer routes
  (``"/about"`` -> ``"./index.htmlabout"``).  Routes are therefore matched as
  whole quoted values and looked up longest-first; the bare ``/`` route is
  only ever replaced as an exact value, never as a substring.
* **``srcSet``/``src`` double-match** -- the value of ``srcset`` must be
  parsed as comma-separated ``url [descriptor]`` candidates exactly once,
  never re-scanned by the single-URL path (which would corrupt descriptors
  and re-rewrite URLs).

Framework-sensitive refs that MUST stay root-absolute (see
:func:`keep_absolute`)::

* ``cj`` / ``sarang`` (plain Next, ``framework="next"``) -- everything can go
  relative.  Proven OK.
* ``arif`` (Next + turbopack, ``framework="next-turbopack"``) -- turbopack
  ``<script src>`` / ``<link href>`` refs stay root-absolute: the runtime
  derives chunk URLs from them and matches them with ``querySelector``, so
  relativising breaks boot silently.
* ``alche`` (Astro, ``framework="astro"``) -- JS-consumed ``data-*`` asset
  attributes (e.g. ``data-top_works_item="/cms-media/....avif"``) stay
  root-absolute: site JS reads the literal values and a relative path sends
  it down a broken branch.  In fact *all* ``data-*`` attributes are left
  untouched by this module, in every framework.

Public API::

    keep_absolute(framework) -> predicate(tag, attr, url) -> bool
    rewrite_page(html, page_depth, route_map, framework) -> (html, missing)
    rewrite_css(text, css_depth=2) -> text
    verify_paths(html, outdir, page_file="index.html", framework=None) -> list
    default_route_file(route_path) -> filename
    self_test() -> None
"""

from __future__ import annotations

import os
import posixpath
import re
import urllib.parse

__all__ = [
    "keep_absolute",
    "rewrite_page",
    "rewrite_css",
    "verify_paths",
    "default_route_file",
    "self_test",
]


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

def keep_absolute(framework):
    """Return the keep-absolute predicate for a framework.

    The predicate ``should_keep(tag, attr, url) -> bool`` reports whether a
    root-absolute URL must be left untouched:

    * every ``data-*`` attribute, in every framework -- site JS consumes the
      literal values (alche carousel ``data-top_works_item`` lesson);
    * ``<script src>`` / ``<link href>`` refs under ``/_next/`` whose file
      name contains ``turbopack``, or -- when ``framework`` itself mentions
      turbopack (e.g. ``"next-turbopack"``) -- every ``/_next/`` script/link
      ref, because the turbopack runtime builds chunk URLs from them and
      matches them via ``querySelector`` (arif lesson: relativising breaks
      boot silently, with no console error pointing at the rewrite).
    """
    fw = (framework or "").lower()
    turbopack_fw = "turbopack" in fw

    def should_keep(tag, attr, url):
        a = (attr or "").lower()
        if a.startswith("data-"):
            return True
        if (tag or "").lower() in ("script", "link"):
            path = (url or "").split("?", 1)[0].split("#", 1)[0]
            if path.startswith("/_next/"):
                if turbopack_fw or "turbopack" in posixpath.basename(path).lower():
                    return True
        return False

    return should_keep


# ---------------------------------------------------------------------------
# route helpers
# ---------------------------------------------------------------------------

def default_route_file(route_path):
    """Guess the local filename for an extensionless route (sibling-file strategy).

    ``/`` -> ``index.html``; ``/about`` -> ``about.html``;
    ``/projects/lead-unity`` -> ``projects/lead-unity.html`` -- a *sibling*
    file next to the ``projects/`` directory, so a route never collides with
    a directory of the same name (arif ``lead-unity`` lesson).
    """
    p = (route_path or "").strip() or "/"
    if p == "/":
        return "index.html"
    p = p.strip("/")
    return p + ".html" if p else "index.html"


def _normalize_routes(route_map):
    """Normalise route_map keys: trailing-slash variants collapsed, longest first."""
    norm = {}
    for route, target in (route_map or {}).items():
        key = route if route == "/" else route.rstrip("/") or "/"
        norm[key] = target
    # longest-first ordering: "/projects/lead-unity" wins over "/projects"
    # wins over "/" -- the "/" key can then never glue a prefix onto a longer
    # route (the "./index.htmlabout" mangling bug).
    return dict(sorted(norm.items(), key=lambda kv: len(kv[0]), reverse=True))


def _lookup_route(path, norm_routes):
    """Look up a root-absolute path (no query/hash) in the route map.

    Tries the raw, URL-decoded, and trailing-slash-stripped forms so that
    ``/works/cloud%20rendering`` and ``/works/cloud rendering`` both hit.
    """
    cands = [path]
    decoded = urllib.parse.unquote(path)
    if decoded != path:
        cands.append(decoded)
    cands.extend(c.rstrip("/") or "/" for c in list(cands))
    for cand in cands:
        if cand in norm_routes:
            return norm_routes[cand]
    return None


def _looks_like_asset(path):
    """Heuristic: last path segment carries a file extension."""
    base = path.rsplit("/", 1)[-1]
    return "." in base and not base.startswith(".") and "/" not in base


# ---------------------------------------------------------------------------
# regexes
# ---------------------------------------------------------------------------

# Exact attribute names only (srcset BEFORE src so the alternation wins at the
# same start position; the lookbehind blocks suffix matches such as the "src"
# in "data-src" or "xsrc").  Re-emits name/whitespace/quotes verbatim.
_ATTR_RE = re.compile(
    r"(?<![\w\-.])(?P<attr>srcset|imagesrcset|src|href|poster|content)"
    r"(?P<eq>\s*=\s*)(?P<q>[\"'])(?P<val>.*?)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)
_SRCSET_ATTRS = {"srcset", "imagesrcset"}

# url(...) in <style> blocks / style="" attributes.
_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<q>[\"']?)(?P<url>.*?)(?P=q)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# srcset candidate descriptor: "1x", "2x", "100w", "1.5x".
_DESCRIPTOR_RE = re.compile(r"^\d+(?:\.\d+)?[wx]$")

# Stash targets: comments + <script> bodies (opening tags stay visible so
# their src attributes are still rewritten; bodies may contain HTML-looking
# JS strings that must not be treated as tags).
_STASH_RE = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|(?P<open><\s*script\b[^<>]*>)(?P<body>.*?)(?P<close><\s*/\s*script\s*>)",
    re.DOTALL | re.IGNORECASE,
)

_TAG_OPEN_RE = re.compile(r"<\s*([a-zA-Z][\w-]*)", re.IGNORECASE)

_BLOB_FMT = "\ue000{0}\ue001"


def _enclosing_tag(html, pos):
    """Best-effort tag name enclosing the attribute match at ``pos``."""
    lt = html.rfind("<", 0, pos)
    if lt == -1:
        return ""
    m = _TAG_OPEN_RE.match(html[lt:lt + 64])
    return m.group(1).lower() if m else ""


def _split_tail(url):
    m = re.match(r"^([^?#]*)([?#].*)?$", url, re.DOTALL)
    return m.group(1), m.group(2) or ""


# ---------------------------------------------------------------------------
# single-URL + srcset rewriting
# ---------------------------------------------------------------------------

def _next_image_target(raw):
    """Map a ``/_next/image?url=...`` value to a mirrored local path.

    Returns the site-root-relative path (no ``./`` prefix), or ``None`` when
    ``raw`` is not an image-optimizer URL.  Handles HTML-escaped ``&amp;``
    separators, already-relativised leftovers (``./_next/image?...`` from
    earlier partial runs -- the repair_cj case), and remote ``http(s)`` URL
    args, which downloaders store flat by basename.
    """
    if raw.startswith("/_next/image?"):
        qs = raw.split("?", 1)[1]
    else:
        m = re.match(r"^(?:\./|\.\./)+_next/image\?(.*)$", raw, re.DOTALL)
        if not m:
            return None
        qs = m.group(1)
    qs = qs.replace("&amp;", "&")
    try:
        params = urllib.parse.parse_qs(qs)
    except Exception:
        return None
    u = (params.get("url") or [""])[0]
    if not u:
        return None
    u = urllib.parse.unquote(u)
    if u.startswith(("http://", "https://", "//")):
        if u.startswith("//"):
            u = "https:" + u
        return urllib.parse.urlparse(u).path.rsplit("/", 1)[-1] or None
    return urllib.parse.unquote(u).lstrip("/") or None


class _Ctx:
    __slots__ = ("prefix", "norm_routes", "missing", "keep", "origin")

    def __init__(self, prefix, norm_routes, missing, keep, origin=None):
        self.prefix = prefix
        self.norm_routes = norm_routes
        self.missing = missing
        self.keep = keep
        self.origin = origin


def _rewrite_single(url, tag, attr, ctx):
    """Rewrite one URL value.  Returns ``(new_url, changed)``."""
    if not url:
        return url, False
    if url.startswith(("http://", "https://")):
        if not ctx.origin:
            return url, False  # absolute and no origin context: hands off
        try:
            ou = urllib.parse.urlparse(url)
            oh = urllib.parse.urlparse(ctx.origin).netloc.lower()
        except Exception:
            return url, False
        if not ou.netloc or ou.netloc.lower() != oh:
            return url, False  # foreign absolute: hands off
        url = ou.path or "/"
        if ou.query:
            url += "?" + ou.query
        if ou.fragment:
            url += "#" + ou.fragment
    if not url.startswith("/"):
        return url, False
    if url.startswith("//"):
        return url, False  # protocol-relative external

    if ctx.keep is not None and ctx.keep(tag, attr, url):
        return url, False  # framework-sensitive: turbopack / data-*

    # Next image-optimizer URLs collapse to the downloaded file.
    local = _next_image_target(url)
    if local is not None:
        return ctx.prefix + local, True

    path, tail = _split_tail(url)
    target = _lookup_route(path, ctx.norm_routes)
    if target is not None:
        return ctx.prefix + target + tail, True
    if _looks_like_asset(path):
        # Mirrored 1:1 by the downloader -- relativise blindly.
        return ctx.prefix + path.lstrip("/") + tail, True
    # Extensionless route with no mapping: record it, but still rewrite to
    # the conventional sibling-file guess so output has zero leftovers.
    ctx.missing.add(path)
    return ctx.prefix + default_route_file(path) + tail, True


def _rewrite_srcset_value(value, tag, attr, ctx):
    """Comma/descriptor-aware srcset rewrite (fixes leftover ``, /path``)."""
    parts = value.split(",")
    changed = False
    out = []
    for part in parts:
        cand = part.strip()
        if not cand:
            out.append(part)
            continue
        bits = cand.rsplit(None, 1)
        if len(bits) == 2 and _DESCRIPTOR_RE.match(bits[1]):
            raw_url, desc = bits
            tail = " " + bits[1]
        else:
            raw_url, tail = cand, ""
        new_url, did = _rewrite_single(raw_url, tag, attr, ctx)
        changed = changed or did
        out.append(new_url + tail)
    return ", ".join(out) if changed else value, changed


def _rewrite_css_urls(text, prefix, origin=None):
    """Rewrite ``url(/...)`` occurrences with a depth prefix (idempotent)."""
    base = (origin or "").rstrip("/")

    def sub(m):
        url = m.group("url").strip()
        if base and url.startswith(base):
            url = url[len(base):] or "/"
        if not url.startswith("/") or url.startswith("//"):
            return m.group(0)
        if url.startswith(("http://", "https://", "data:")):
            return m.group(0)
        path, tail = _split_tail(url)
        if _next_image_target(url) is not None:
            path = _next_image_target(url)
        else:
            path = path.lstrip("/")
        q = m.group("q")
        return "url(" + q + prefix + path + tail + q + ")"

    return _CSS_URL_RE.sub(sub, text)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def rewrite_page(html, page_depth, route_map, framework, origin=None):
    """Rewrite one HTML page for offline viewing.

    ``page_depth`` is the page's directory depth below the site root
    (``index.html`` -> 0, ``projects/x.html`` -> 1); the relative prefix is
    ``./`` at depth 0, ``../`` * depth otherwise.  ``route_map`` maps root
    routes (``/about``) to root-relative local files (``about.html``).
    ``framework`` selects the :func:`keep_absolute` policy.

    Returns ``(rewritten_html, missing_targets)`` where ``missing_targets``
    is the sorted list of extensionless routes found in the page but absent
    from ``route_map``.  The rewrite is idempotent: running it again changes
    nothing.
    """
    prefix = "./" if (page_depth or 0) == 0 else "../" * page_depth
    norm_routes = _normalize_routes(route_map)
    keep = keep_absolute(framework)
    missing = set()
    ctx = _Ctx(prefix, norm_routes, missing, keep, origin)

    # 1. stash comments + script bodies (opening tags stay).
    blobs = []

    def stash(m):
        if m.group("comment") is not None:
            blobs.append(m.group("comment"))
            return _BLOB_FMT.format(len(blobs) - 1)
        blobs.append(m.group("body"))
        return m.group("open") + _BLOB_FMT.format(len(blobs) - 1) + m.group("close")

    masked = _STASH_RE.sub(stash, html)

    # 2. attribute pass (srcset handled by its own parser -- never rescanned
    #    by the single-URL path, fixing the src/srcset double-match bug).
    def attr_sub(m):
        attr = m.group("attr")
        val = m.group("val")
        tag = _enclosing_tag(masked, m.start())
        if tag == "meta":
            return m.group(0)  # SEO/social content is never a path
        if attr.lower() in _SRCSET_ATTRS:
            new_val, did = _rewrite_srcset_value(val, tag, attr, ctx)
        else:
            new_val, did = _rewrite_single(val, tag, attr, ctx)
        if not did:
            return m.group(0)
        return attr + m.group("eq") + m.group("q") + new_val + m.group("q")

    masked = _ATTR_RE.sub(attr_sub, masked)

    # 3. url(...) inside <style> blocks / style="" attributes.
    masked = _rewrite_css_urls(masked, prefix, origin)

    # 4. restore stashed blobs.
    def unstash(m):
        return blobs[int(m.group(1))]

    masked = re.sub("\ue000(\\d+)\ue001", unstash, masked)

    return masked, sorted(missing)


def rewrite_css(text, css_depth=2, origin=None):
    """Rewrite ``url(/...)`` refs inside a CSS file to depth-relative paths.

    ``css_depth`` is the CSS file's directory depth below the site root;
    Next.js bundles live at ``_next/static/css/`` (depth 2, the default).
    External URLs, ``data:`` URIs, protocol-relative refs and ``#fragment``
    refs are left untouched.  Absolute same-origin URLs are folded to
    root-relative first (``origin``).  Idempotent.
    """
    prefix = "./" if (css_depth or 0) == 0 else "../" * css_depth
    return _rewrite_css_urls(text, prefix, origin)


def verify_paths(html, outdir, page_file="index.html", framework=None, origin=None):
    """Check every local ref in ``html`` against ``outdir`` on disk.

    ``page_file`` locates the page (``"index.html"`` at root, or e.g.
    ``"projects/x.html"``) so ``../`` refs resolve correctly.  Returns the
    sorted list of problem refs: local files that do not exist, plus any
    leftover root-absolute refs -- except policy-kept ones when
    ``framework`` is given (turbopack scripts/links, ``data-*`` values are
    never reported since :func:`rewrite_page` intentionally preserves them).
    External refs (``http(s)``, ``//``, ``data:``, ``mailto:`` ...) are
    ignored.  A clean offline page returns ``[]``.
    """
    keep = keep_absolute(framework) if framework else None
    page_dir = posixpath.dirname(page_file.replace(os.sep, "/"))
    problems = set()

    def check_ref(ref, tag, attr):
        ref = (ref or "").strip()
        if not ref:
            return
        if (tag or "").lower() == "meta":
            return  # SEO/social content is never a path
        if ref.startswith(("http://", "https://", "//", "data:", "blob:",
                            "mailto:", "tel:", "javascript:")):
            return
        if ref.startswith("#"):
            return
        if ref.startswith(("http://", "https://")):
            if origin:
                try:
                    same = urllib.parse.urlparse(ref).netloc.lower() == urllib.parse.urlparse(origin).netloc.lower()
                except Exception:
                    same = False
                if same:
                    problems.add(ref)  # absolute same-origin leftover
            return
        if ref.startswith("/"):
            if keep is not None and keep(tag, attr, ref):
                return
            problems.add(ref)  # leftover that should have been rewritten
            return
        # local relative ref -- must exist on disk.
        disk = urllib.parse.unquote(ref.split("?", 1)[0].split("#", 1)[0])
        if not disk:
            return
        full = os.path.normpath(os.path.join(str(outdir), page_dir, disk)) \
            if page_dir else os.path.normpath(os.path.join(str(outdir), disk))
        if not os.path.exists(full):
            problems.add(ref)

    for m in _ATTR_RE.finditer(html):
        attr, val = m.group("attr"), m.group("val")
        tag = _enclosing_tag(html, m.start())
        if attr.lower() in _SRCSET_ATTRS:
            for part in val.split(","):
                cand = part.strip()
                if not cand:
                    continue
                bits = cand.rsplit(None, 1)
                if len(bits) == 2 and _DESCRIPTOR_RE.match(bits[1]):
                    check_ref(bits[0], tag, attr)
                else:
                    check_ref(cand, tag, attr)
        else:
            check_ref(val, tag, attr)
    for m in _CSS_URL_RE.finditer(html):
        check_ref(m.group("url").strip(), "style", "url")

    return sorted(problems)


# ---------------------------------------------------------------------------
# self-test (regression coverage for every known bug lesson)
# ---------------------------------------------------------------------------

def rewrite_bundle_origins(outdir, origin):
    """Replace absolute same-origin URLs inside JS/CSS/JSON text assets.

    Bundles hardcode e.g. ``new Worker("https://host/assets/x.js")``;
    workers/modules are strictly same-origin, so under the local preview
    server every such construction throws and the app never boots past
    its loading screen. Rewriting the origin to ```` restores
    root-relative same-origin semantics. Returns (files, replacements).
    """
    if not origin:
        return 0, 0
    base = (origin or "").rstrip("/")
    if not base:
        return 0, 0
    exts = (".js", ".css", ".json", ".webmanifest", ".splinecode")
    touched, reps = 0, 0
    for root, _d, fs in os.walk(outdir):
        for x in fs:
            if not x.lower().endswith(exts):
                continue
            p = os.path.join(root, x)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    t = fh.read()
            except (OSError, ValueError, UnicodeError):
                continue
            if base not in t:
                continue
            n = t.count(base)
            t = t.replace(base, "")
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(t)
            except OSError:
                continue
            touched += 1
            reps += n
    return touched, reps


def self_test():
    """Exercise the tricky cases; raises AssertionError on any regression."""
    # --- 1. basic route mapping + "/" glue regression --------------------
    html = (
        '<a href="/">home</a><a href="/about">a</a>'
        '<a href="/about#team">t</a><a href="/about?x=1">q</a>'
        '<img src="/hero/photo.png">'
    )
    routes = {"/": "index.html", "/about": "about.html"}
    out, missing = rewrite_page(html, 0, routes, "next")
    assert 'href="./index.html"' in out, out
    assert out.count("./index.html") == 1, out  # no glue onto /about
    assert 'href="./about.html"' in out, out
    assert 'href="./about.html#team"' in out, out
    assert 'href="./about.html?x=1"' in out, out
    assert 'src="./hero/photo.png"' in out, out
    assert missing == [], missing

    # --- 2. longest-match-first: /projects vs /projects/lead-unity --------
    html = '<a href="/projects">p</a><a href="/projects/lead-unity">l</a>'
    routes = {"/projects": "projects.html",
              "/projects/lead-unity": "projects/lead-unity.html"}
    out, missing = rewrite_page(html, 1, routes, "next")
    assert 'href="../projects.html"' in out, out
    assert 'href="../projects/lead-unity.html"' in out, out
    assert missing == [], missing

    # --- 3. turbopack scripts/links stay absolute (arif lesson) ----------
    html = (
        '<script src="/_next/static/chunks/turbopack-abc123.js" async=""></script>'
        '<script src="/_next/static/chunks/app/page-def456.js"></script>'
        '<link rel="stylesheet" href="/_next/static/css/main.css">'
    )
    out, _ = rewrite_page(html, 0, {}, "next-turbopack")
    assert 'src="/_next/static/chunks/turbopack-abc123.js"' in out, out
    assert 'src="/_next/static/chunks/app/page-def456.js"' in out, out
    assert 'href="/_next/static/css/main.css"' in out, out  # kept under turbopack fw
    out2, _ = rewrite_page(html, 0, {}, "next")
    assert 'src="/_next/static/chunks/turbopack-abc123.js"' in out2, out2
    assert 'src="./_next/static/chunks/app/page-def456.js"' in out2, out2
    assert 'href="./_next/static/css/main.css"' in out2, out2

    # --- 4. data-* asset attrs stay absolute (alche lesson) ---------------
    html = ('<div data-top_works_item="/cms-media/ABC-w800.avif" '
            'data-x="/works/y" src="/real.png">')
    out, missing = rewrite_page(html, 0, {}, "astro")
    assert 'data-top_works_item="/cms-media/ABC-w800.avif"' in out, out
    assert 'data-x="/works/y"' in out, out
    assert 'src="./real.png"' in out, out
    assert missing == [], missing  # data-* routes never reported

    # --- 5. src/srcset double-match: each value rewritten exactly once ----
    html = ('<img src="/a.png" srcset="/a.png 1x, /b.png 2x">'
            '<img srcset="/c.png 480w, /d.png 800w">')
    out, _ = rewrite_page(html, 0, {}, "next")
    assert 'src="./a.png"' in out, out
    assert 'srcset="./a.png 1x, ./b.png 2x"' in out, out
    assert 'srcset="./c.png 480w, ./d.png 800w"' in out, out
    assert ", /" not in out, out  # no ", /path" leftovers

    # --- 6. /_next/image optimizer: local path + http->basename ----------
    html = ('<img src="/_next/image?url=%2Fprojects%2Ffoo.jpg&amp;w=1080&amp;q=75">'
            '<img src="/_next/image?url=https%3A%2F%2Fcdn.site%2Fimg%2Fbar.avif&w=640&q=75">')
    out, _ = rewrite_page(html, 1, {}, "next")
    assert 'src="../projects/foo.jpg"' in out, out
    assert 'src="../bar.avif"' in out, out
    assert "_next/image" not in out, out

    # --- 7. CSS url() from css file location ------------------------------
    css = ".a{background:url(/_next/static/media/f.woff2) url('https://x/y.css')}"
    assert rewrite_css(css) == (
        ".a{background:url(../../_next/static/media/f.woff2) "
        "url('https://x/y.css')}"), rewrite_css(css)

    # --- 8. idempotency ----------------------------------------------------
    html = ('<a href="/about">a</a><img src="/x.png" '
            'srcset="/x.png 1x, /y.png 2x">'
            '<script src="/_next/static/chunks/turbopack-z.js"></script>')
    once, _ = rewrite_page(html, 0, {"/about": "about.html"}, "next-turbopack")
    twice, missing2 = rewrite_page(once, 0, {"/about": "about.html"}, "next-turbopack")
    assert once == twice, (once, twice)
    assert missing2 == [], missing2

    # --- 9. unmapped extensionless route: recorded + guessed -------------
    out, missing = rewrite_page('<a href="/mystery">m</a>', 0, {}, "next")
    assert missing == ["/mystery"], missing
    assert 'href="./mystery.html"' in out, out

    # --- 10. verify_paths: clean tree -> [], broken -> reported -----------
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "projects"), exist_ok=True)
        open(os.path.join(tmp, "about.html"), "w").write("x")
        open(os.path.join(tmp, "a.png"), "w").write("x")
        page = ('<a href="./about.html">a</a><img src="./a.png">'
                '<img src="./gone.png">'
                '<script src="/_next/static/chunks/turbopack-z.js"></script>')
        probs = verify_paths(page, tmp, "index.html", framework="next-turbopack")
        assert probs == ["./gone.png"], probs  # kept turbopack ref not flagged
        probs2 = verify_paths(page, tmp, "index.html")
        assert sorted(probs2) == sorted(
            ["./gone.png", "/_next/static/chunks/turbopack-z.js"]), probs2

    # --- 11. script bodies + comments are never treated as tags -----------
    html = ('<script>var u="/_next/image?url=%2Fa.png&w=64&q=75";</script>'
            '<!-- <a href="/about"> -->'
            '<a href="/about">x</a>')
    out, _ = rewrite_page(html, 0, {"/about": "about.html"}, "next")
    assert 'var u="/_next/image?url=%2Fa.png&w=64&q=75";' in out, out
    assert '<!-- <a href="/about"> -->' in out, out
    assert 'href="./about.html">x</a>' in out, out

    # --- 12. external / fragment / protocol-relative refs untouched -------
    html = ('<a href="https://x.io/y">e</a><a href="//cdn.io/z">p</a>'
            '<a href="#sec">f</a><a href="mailto:a@b.c">m</a>')
    out, missing = rewrite_page(html, 0, {}, "next")
    assert out == html, out
    assert missing == [], missing

    print("rewrite.self_test: all 12 regression groups passed")


if __name__ == "__main__":
    self_test()
