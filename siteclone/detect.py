"""siteclone.detect — pipeline stage 2: framework detection + HTML asset extraction.

Called by ``cli.py`` as ``detect_framework(html)`` / ``extract_assets(html)``.

Stdlib only (``html.parser``) — no bs4/lxml. No downloading here; that is
the fetcher's job. ``html`` is the raw page source (str).
"""

from __future__ import annotations

import html as _html
import re
import urllib.parse
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp"}
_FONT_EXTS = {"woff", "woff2", "ttf", "otf", "eot"}
_VIDEO_EXTS = {"mp4", "webm", "ogv", "mov", "m4v", "m3u8"}
_AUDIO_EXTS = {"mp3", "wav", "oga", "ogg", "flac", "aac"}
_TIMEDTEXT_EXTS = {"vtt", "srt"}
_MEDIA_EXTS = _IMAGE_EXTS | _VIDEO_EXTS | _AUDIO_EXTS | _TIMEDTEXT_EXTS
_SCRIPT_EXTS = {"js", "mjs", "cjs"}
# asset refs that belong in the misc "links" bucket (flight data, manifests…)
_LINK_EXTS = {"json", "wasm", "webmanifest", "map", "xml", "txt", "pdf",
              "glb", "gltf", "drc", "riv", "fbx", "obj", "ply", "splat",
              "usdz", "mtl"}

_SKIP_PREFIXES = ("data:", "blob:", "mailto:", "tel:", "javascript:")
_EXTERNAL_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:)?//", re.IGNORECASE)


def _clean(value: object) -> str:
    """Unescape entities (``&amp;`` → ``&``) and strip whitespace."""
    if value is None:
        return ""
    return _html.unescape(str(value)).strip()


def _is_same_origin(url: str, origin=None) -> bool:
    """Same-origin (fetchable) ref: relative, root-absolute, or absolute
    URL on ``origin``'s host. Bare absolute URLs without origin context
    are treated as external (conservative default)."""
    u = url.strip()
    if not u:
        return False
    low = u.lower()
    if low.startswith(_SKIP_PREFIXES) or u.startswith("#"):
        return False
    m = _EXTERNAL_RE.match(u)
    if m:
        if origin is None:
            return False
        try:
            host = urllib.parse.urlparse(origin).netloc.lower()
            uh = urllib.parse.urlparse(u if "://" in u else "https:" + u).netloc.lower()
        except Exception:
            return False
        return bool(host and uh == host)
    return True


def _ext_of(url: str) -> str:
    """Lowercased file extension of the path part (query/fragment ignored)."""
    path = url.split("#", 1)[0].split("?", 1)[0].strip().rstrip("\\")
    m = re.search(r"\.([A-Za-z0-9]+)$", path)
    return m.group(1).lower() if m else ""


def _split_srcset(value: str) -> list[str]:
    """Split a srcset value into URLs (comma-separated, ``url descriptor``).

    Filenames may contain literal spaces (``photo 1.jpg``), so only a
    trailing ``<n>w`` / ``<n>x`` density descriptor is stripped — the rest
    of the entry is kept verbatim as the URL.
    """
    out: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        if len(tokens) > 1 and re.match(r"^(\d+w|\d+(\.\d+)?x)$", tokens[-1]):
            url = " ".join(tokens[:-1])
        else:
            url = part
        if url:
            out.append(url)
    return out


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# framework detection
# ---------------------------------------------------------------------------

def detect_framework(html: str | None) -> str:
    """Return one of: next-webpack, next-turbopack, astro, vite-spa, svelte,
    static, unknown.

    Markers (checked in order):
      ``/_next/static/chunks/webpack-``            → next-webpack
      ``/_next/static/chunks/turbopack-``/``__turbopack`` → next-turbopack
      ``/_astro/`` (+ ``astro-`` hashed classes)   → astro
      ``/assets/index-*.js`` + ``<div id="root">`` → vite-spa
      ``.svelte`` chunk names                       → svelte
      none of the above                             → static (plain HTML) or
                                                      unknown (bundled JS we
                                                      cannot attribute).
    """
    if not html or not str(html).strip():
        return "unknown"
    t = str(html)

    # Turbopack first: both Next flavours contain /_next/ markers.
    if "/_next/static/chunks/turbopack-" in t or "__turbopack" in t:
        return "next-turbopack"
    # Webpack (default Next bundler): explicit chunk prefix, or generic Next
    # markers (__NEXT_DATA__ payload, /_next/static assets, #__next mount).
    if (
        "/_next/static/chunks/webpack-" in t
        or "__NEXT_DATA__" in t
        or "/_next/static/" in t
        or 'id="__next"' in t
        or "id='__next'" in t
    ):
        return "next-webpack"
    # Astro: compiled output lives under /_astro/, components carry
    # astro-XXXX hashed scopes.
    if "/_astro/" in t:
        return "astro"
    # SvelteKit: chunks named like Foo.svelte-abc123.js (+ /_app/immutable),
    # or svelte-<hash> scoped classes in server-rendered markup.
    if re.search(r"\.svelte[-.]", t) or (
        "sveltekit" in t.lower() and "/_app/immutable" in t
    ):
        return "svelte"
    # Vite SPA: hashed entry /assets/index-*.js mounting into <div id="root">.
    if re.search(r"/assets/index-[^\"'\s<>]*\.js", t):
        return "vite-spa"
    if re.search(r'id\s*=\s*["\']root["\']', t) and re.search(
        r"/assets/[^\"'\s<>]*\.js", t
    ):
        return "vite-spa"
    # Svelte SPA shell (e.g. abeto): Vite-built module entry + hashed
    # assets and/or modulepreload, but no app mount div and no index
    # entry. Svelte mounts on document.body directly, so its shell has an
    # empty body with no #root/#app — unlike vite-spa / React / Vue.
    if (
        re.search(r'<script[^>]+type\s*=\s*["\']module["\']', t, re.IGNORECASE)
        and (
            "modulepreload" in t.lower()
            or re.search(r"assets/[^\"'<>\s]*-[A-Za-z0-9_\-]{6,}\.(?:js|css)", t)
        )
        and not re.search(
            r'id\s*=\s*["\'](?:root|app|__next|main)["\']', t, re.IGNORECASE
        )
    ):
        return "svelte"
    # Bundled JS with no attributable markers → unknown, else plain static.
    if re.search(
        r"chunks?/|_app/immutable|__vite|parcel|gatsby|_nuxt|nuxt-|"
        r"bundle[.-]|app\.[a-f0-9]{6,}\.js",
        t.lower(),
    ):
        return "unknown"
    return "static"


# ---------------------------------------------------------------------------
# asset extraction
# ---------------------------------------------------------------------------

class _AssetParser(HTMLParser):
    """Single-pass collector of every URL-bearing attribute + data-* values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []       # <script src>
        self.link_hrefs: list[tuple[str, str]] = []  # (href, rel)
        self.srcs: list[str] = []          # img/source/video/audio/track/
                                           # embed/object/input src, poster,
                                           # meta content, data-src-ish
        self.srcsets: list[str] = []       # split srcset entries
        self.data_values: list[str] = []   # every data-* attribute value
        self.anchors: list[str] = []       # <a href>
        self._script_depth = 0
        self.script_texts: list[str] = []  # inline <script> bodies (JSON refs)

    # -- plumbing ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = (tag or "").lower()
        plain: dict[str, str] = {}
        data_vals: list[str] = []
        for name, val in attrs:
            name = (name or "").lower()
            text = _clean(val)
            if name.startswith("data-"):
                if text:
                    data_vals.append(text)
            elif name not in plain:
                plain[name] = text
            # NOTE: data-* names are intentionally NOT lowercased away —
            # HTMLParser already lowercases them; values kept verbatim.
        if tag == "script":
            self._script_depth += 1
            src = plain.get("src", "")
            if src:
                self.scripts.append(src)
        elif tag == "link":
            href = plain.get("href", "")
            if href:
                self.link_hrefs.append((href, plain.get("rel", "").lower()))
        elif tag == "a":
            href = plain.get("href", "")
            if href:
                self.anchors.append(href)
        elif tag == "meta":
            content = plain.get("content", "")
            if content and ("/" in content or re.search(r"\.(png|jpe?g|webp|avif|gif|svg|ico|mp4|webm)($|[?#])", content, re.IGNORECASE)):
                self.srcs.append(content)  # og:image etc; skip plain SEO text
        elif tag == "img":
            if plain.get("src"):
                self.srcs.append(plain["src"])
            if plain.get("srcset"):
                self.srcsets.extend(_split_srcset(plain["srcset"]))
        elif tag in ("source", "audio", "track", "embed"):
            if plain.get("src"):
                self.srcs.append(plain["src"])
            if plain.get("srcset"):
                self.srcsets.extend(_split_srcset(plain["srcset"]))
        elif tag == "video":
            if plain.get("src"):
                self.srcs.append(plain["src"])
            if plain.get("poster"):
                self.srcs.append(plain["poster"])
            if plain.get("srcset"):
                self.srcsets.extend(_split_srcset(plain["srcset"]))
        elif tag == "object":
            if plain.get("data"):
                self.srcs.append(plain["data"])
        elif tag == "input" and plain.get("type", "").lower() == "image":
            if plain.get("src"):
                self.srcs.append(plain["src"])
        else:
            # Any other tag: still honour src/srcset/href if present
            # (e.g. <use href>, <image href>, <model-viewer>, custom elements).
            if plain.get("src"):
                self.srcs.append(plain["src"])
            if plain.get("srcset"):
                self.srcsets.extend(_split_srcset(plain["srcset"]))
            if tag not in ("base", "body", "html") and plain.get("href"):
                self.srcs.append(plain["href"])
            for extra in ("xlink:href", "ios-src", "environment-image",
                          "skybox-image", "poster", "data-src", "data-poster"):
                if plain.get(extra):
                    self.srcs.append(plain[extra])
        self.data_values.extend(data_vals)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if (tag or "").lower() == "script":
            self._script_depth = max(0, self._script_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._script_depth > 0 and data and "/_next/" in data:
            self.script_texts.append(data)


_NEXT_IMAGE_RE = re.compile(r"/_next/image\?url=[^\"'\s<>\\]+")
# Quoted root-/dot-relative refs, e.g. inline JSON flight-data asset paths.
_QUOTED_REF_RE = re.compile(r"[\"']((?:/|\./|\.\./)[^\"'\s<>\\]+)[\"']")
# ../../.. — tolerate JSON-escaped slashes by normalising first.


def _scan_text_refs(raw: str) -> tuple[list[str], list[str]]:
    """Find asset refs embedded in inline JSON/text (Next flight data…).

    Returns (next_images, quoted_refs). Values are entity-unescaped.
    """
    norm = raw.replace("\\/", "/")
    next_images = [_clean(m) for m in _NEXT_IMAGE_RE.findall(norm)]
    quoted = [_clean(m) for m in _QUOTED_REF_RE.findall(norm)]
    return next_images, quoted


def _inner_next_url(ref: str) -> str:
    """Decode the ``url=`` target of a ``/_next/image?url=…`` ref."""
    m = re.search(r"[?&]url=([^&]+)", ref)
    if not m:
        return ""
    from urllib.parse import unquote as _unquote

    return _unquote(m.group(1))


def extract_assets(html: str | None, origin=None) -> dict[str, list[str]]:
    """Extract same-origin asset refs from page HTML.

    Returns dict of lists with keys: scripts, stylesheets, images, fonts,
    videos, links, data_attrs, next_images, routes.
    """
    raw = str(html or "")
    parser = _AssetParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass  # tolerant: keep whatever was collected before the error

    scripts: list[str] = []
    stylesheets: list[str] = []
    images: list[str] = []
    fonts: list[str] = []
    videos: list[str] = []
    links: list[str] = []
    data_attrs: list[str] = []
    next_images: list[str] = []
    routes: list[str] = []

    def _add_image(url: str) -> None:
        images.append(url)
        if "/_next/image?url=" in url and url not in next_images:
            next_images.append(url)
            inner = _inner_next_url(url)
            if inner and _is_same_origin(inner, origin) and inner not in images:
                images.append(inner)

    # -- <script src> ---------------------------------------------------
    for url in parser.scripts:
        if _is_same_origin(url, origin):
            scripts.append(url)
            if "/_next/image?url=" in url and url not in next_images:
                next_images.append(url)

    # -- <link href> -----------------------------------------------------
    for href, rel in parser.link_hrefs:
        if not _is_same_origin(href, origin):
            continue
        ext = _ext_of(href)
        if "stylesheet" in rel or ext == "css":
            stylesheets.append(href)
        elif ext in _IMAGE_EXTS:
            images.append(href)
        elif ext in _FONT_EXTS:
            fonts.append(href)
        elif ext in _VIDEO_EXTS | _AUDIO_EXTS | _TIMEDTEXT_EXTS:
            videos.append(href)
        elif "/_next/image?url=" in href:
            _add_image(href)
        else:
            links.append(href)  # icon w/o ext, manifest, canonical…

    # -- generic src / poster / meta content / split srcsets -------------
    for url in parser.srcs + parser.srcsets:
        if not _is_same_origin(url, origin):
            continue
        if "/_next/image?url=" in url:
            _add_image(url)
            continue
        ext = _ext_of(url)
        if ext in _SCRIPT_EXTS:
            scripts.append(url)
        elif ext == "css":
            stylesheets.append(url)
        elif ext in _IMAGE_EXTS:
            images.append(url)
        elif ext in _FONT_EXTS:
            fonts.append(url)
        elif ext in _VIDEO_EXTS | _AUDIO_EXTS | _TIMEDTEXT_EXTS:
            videos.append(url)
        elif ext in _LINK_EXTS:
            links.append(url)
        # else: page URLs (canonical/og:url) or ext-less refs — skip.

    # -- data-* attributes ending in media extensions --------------------
    for val in parser.data_values:
        if not _is_same_origin(val, origin):
            continue
        candidates = [val]
        if "," in val and _ext_of(val) not in _MEDIA_EXTS:
            candidates = _split_srcset(val)
        for cand in candidates:
            cand = cand.strip()
            if (
                cand
                and _is_same_origin(cand, origin)
                and _ext_of(cand) in _MEDIA_EXTS
                and cand not in data_attrs
            ):
                data_attrs.append(cand)

    # -- text scan: inline JSON / flight-data refs ------------------------
    text_next, quoted = _scan_text_refs(raw)
    for ref in text_next:
        if _is_same_origin(ref) and ref not in next_images:
            next_images.append(ref)
            inner = _inner_next_url(ref)
            if (
                inner
                and _is_same_origin(inner)
                and inner not in images
                and _ext_of(inner) in _MEDIA_EXTS
            ):
                images.append(inner)
    for ref in quoted:
        if not _is_same_origin(ref, origin):
            continue
        if "/_next/image?url=" in ref:
            if ref not in next_images:
                next_images.append(ref)
            continue
        ext = _ext_of(ref)
        if ext in _SCRIPT_EXTS:
            if ref not in scripts:
                scripts.append(ref)
        elif ext == "css":
            if ref not in stylesheets:
                stylesheets.append(ref)
        elif ext in _IMAGE_EXTS:
            if ref not in images:
                images.append(ref)
        elif ext in _FONT_EXTS:
            if ref not in fonts:
                fonts.append(ref)
        elif ext in _VIDEO_EXTS | _AUDIO_EXTS | _TIMEDTEXT_EXTS:
            if ref not in videos:
                videos.append(ref)
        elif ext in _LINK_EXTS:
            if ref not in links:
                links.append(ref)

    # -- routes: same-origin <a href> paths -------------------------------
    for href in parser.anchors:
        if not _is_same_origin(href):
            continue
        route = href.split("#", 1)[0].strip()
        if not route or route in routes:
            continue
        if route.startswith("?"):
            continue  # pure query — same-page state, not a route
        routes.append(route)

    return {
        "scripts": _dedupe(scripts),
        "stylesheets": _dedupe(stylesheets),
        "images": _dedupe(images),
        "fonts": _dedupe(fonts),
        "videos": _dedupe(videos),
        "links": _dedupe(links),
        "data_attrs": _dedupe(data_attrs),
        "next_images": _dedupe(next_images),
        "routes": _dedupe(routes),
    }
