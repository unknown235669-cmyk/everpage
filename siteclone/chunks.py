"""JS-bundle chunk discovery (pipeline stage 3).

Finds assets the HTML never mentions by scanning downloaded JS/CSS
bundle text: webpack chunk maps, Next.js flight payloads, turbopack
manifests, vite dynamic imports, three.js / lottie / draco asset
literals, CSS url() refs, /_next/image originals and /frames/ video
sequences.

Public API:
    discover(bundle_text, framework=None) -> list
    crawl_bundles(session, origin, entry_js_urls, framework=None,
                  fetcher=None, max_depth=3, out_dir=None) -> list

``discover`` returns an order-stable, de-duplicated list whose items
are either root-relative/relative URL strings (query strings kept)
or ``frames`` expansion dicts::

    {"type": "frames", "template": "/frames/video1/video{}.webp",
     "start": 1000, "end": 1296, "pad": 0, "ext": "webp",
     "folder": "video1", "prefix": "video"}

``framework`` is a hint (``webpack`` | ``turbopack`` | ``vite`` |
``astro`` | ``svelte`` | ``generic`` | None).  ``None`` / ``generic``
runs every parser.  Framework parsers always run on top of the generic
pass so nothing is missed.

Regexes ported from the proven sibling scripts: scan_js*.py (quoted
asset literals per extension), dl_abeto.py (drc/icon/font/ogg/ktx2 +
worker/basis paths), repair_sarang.py (/_next/static chunks+css,
/_next/image?url=, /photo|skills|videos|... direct refs),
scan_chunks.py + dl_chunks.py (webpack id->hash maps),
dl_chunks_arif.py (static/chunks/ literal closure),
scan_frames.py + dl_frames.py (/frames/ sequences).

stdlib + requests only.  No browser automation, no site hardcodes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - stdlib fallback via urllib
    requests = None  # type: ignore

__all__ = ["discover", "crawl_bundles", "normalize_ref", "is_bundle_ref",
           "expand_deep_file"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# extension universe (superset of scan_js.py ext list + task spec)
# ---------------------------------------------------------------------------

_ASSET_EXTS = (
    "js|css|woff2?|ttf|otf|eot|fnt|font|icon|png|jpe?g|webp|avif|gif|svg|"
    "ico|mp4|webm|mp3|ogg|wav|glb|gltf|drc|ktx2?|ktx|basis|bin|wasm|hdr|exr|"
    "json|atlas|fnt|pbf|parquet|m3u8|mov|m4v|ply|splat|riv|fbx|obj|mtl|"
    "usdz|pvr|astc|dds|dae|stl|3ds|usd|usda|usdc|vrm|lottie|splinecode|"
    "tga|ttc|vtt"
)
_EXT_RE = _ASSET_EXTS  # alias, same alternation string

# path-safe charset for quoted literals: minified bundles have no spaces,
# so anything with (){}[];, is code, not a path (scan_js*.py used
# [A-Za-z0-9_./-] bodies; we add URL chars % + ? # & = @ : $ ~ -).
_PCH = r"A-Za-z0-9_./%+?#&=@:$~\-"
_RX_QUOTED_ASSET = re.compile(
    (r"""["'`]([PCH]{1,220}?\.(?:EXTS)((?:[?#][^"'`\s<>]*)?))["'`]"""
     .replace("[PCH]", "[" + _PCH + "]").replace("EXTS", _EXT_RE)),
    re.IGNORECASE,
)
# unquoted url(...) inside CSS chunks (incl. data/blob which we filter later)
_RX_CSS_URL = re.compile(
    r"""url\(\s*["']?([^)"'\s<>]{1,300})["']?\s*\)""", re.IGNORECASE
)
# webpack id -> hash maps: 3199:"3ee2c72262d10a0e" / 594603.js builder region
# (scan_chunks.py used (\d{3,5})["']:["']([0-9a-f]{16,22}); widen slightly)
_RX_ID_HASH = re.compile(r"""(\d{2,5})["']?\s*:\s*["']([0-9a-f]{8,64})["']""")
_RX_PREF_MAP = re.compile(r"""\(\{((?:\d+:["'][0-9a-f]{8}["'],?)+)\}\)""")
# explicit chunk filenames mentioned literally
_RX_CHUNK_FILE = re.compile(
    r"""(?:^|[^A-Za-z0-9_.-])((?:\d+\.)?[A-Za-z0-9_.$-]*[0-9a-f]{8,}\.[A-Za-z0-9_.$-]*\.?(?:js|css))"""
)
# static/chunks/ literal refs (dl_chunks_arif.py closure pattern)
_RX_STATIC_CHUNKS = re.compile(
    r"""static/chunks/([A-Za-z0-9_.$-]+\.(?:js|css))"""
)
# /_next/static/... asset paths
_RX_NEXT_STATIC = re.compile(
    r"""/_next/static/(?:chunks|css|media)/[^"'`\s\\<>]+?\.(?:js|css|woff2?|ttf|otf|png|jpe?g|webp|avif|svg|glb|gltf|wasm|json|mp4|webm|mov|m3u8|fbx|obj|ply|splat|riv)"""
    r"""(?:[?#][^"'`\s\\<>]*)?""",
    re.IGNORECASE,
)
# next flight payloads: self.__next_f.push / __next_f.push bodies
_RX_FLIGHT_CHUNK = re.compile(
    r"""(?:self\.)?__next_f\.push\(\s*\[(.*?)\]\s*\)\s*;?""", re.DOTALL
)
_RX_FLIGHT_REF = re.compile(
    r"""(?:\\?/)?_next/static/(?:chunks|css|media)/[^"'`\s\\<>]+?\.(?:js|css)"""
    r"""(?:[?#][^"'`\s\\<>]*)?""",
    re.IGNORECASE,
)
# turbopack: parseChunk manifest entries + hashed chunk files
_RX_TURBOPACK_MANIFEST = re.compile(
    r"""parseChunk[^;]{0,600}?["'`]([^"'`]{1,220}?\.js(?:[?#][^"'`]*)?)["'`]""",
    re.DOTALL,
)
_RX_TURBOPACK_CHUNK = re.compile(
    r"""(?:\\?/)?_next/static/chunks/[^"'`\s\\<>]*?[0-9a-f]{8,}[^"'`\s\\<>]*?\.(?:js|css)"""
    r"""(?:[?#][^"'`\s\\<>]*)?""",
    re.IGNORECASE,
)
# vite: dynamic import("...") / import(`...`)
_RX_DYNAMIC_IMPORT = re.compile(
    r"""import\(\s*["'`]([^"'`]{1,220}?)["'`]\s*\)"""
)
_RX_VITE_ASSETS = re.compile(
    (r"""["'`]([_PCH_]{1,120}?assets/[_PCH2_]{1,180})["'`]"""
     .replace("_PCH_", _PCH).replace("_PCH2_", r"A-Za-z0-9_./\-"))
)
_RX_VITE_LITERAL = re.compile(
    (r"""["'`]([_PCH_]{1,220}?\.(?:glb|gltf|drc|ktx2?|font|icon|webp|basis|hdr|bin|wasm|fnt|atlas|ply|splat|riv|fbx|obj|mtl|usdz)(?:[?#][^"'`\s<>]*)?)["'`]"""
     .replace("_PCH_", _PCH)),
    re.IGNORECASE,
)
# astro/svelte/three: lottie data.json, scene.glb, /common/, known asset dirs,
# draco/basis workers, loader setPath/transcoder hints
_RX_SCENE_DIRS = re.compile(
    (r"""["'`]([_PCH_]{1,220}?(?:/common/|/assets/(?:audio|fonts|images|geometries|libs)/|"""
     r"""lottie/|data\.json|scene\.glb|draco|basis|meshopt|transcoder|worker|setPath)[_PCH2_]{0,120})["'`]"""
     .replace("_PCH_", _PCH).replace("_PCH2_", r"A-Za-z0-9_./%+?#&=@:$~\-")),
    re.IGNORECASE,
)
_RX_DECODER_PATH = re.compile(
    r"""set(?:Decoder|Transcoder|MeshoptDecoder)Path\(\s*["'`]([^"'`()]{1,180})["'`]"""
)
_RX_TYPEFACE_JSON = re.compile(
    r"""["'`]([^"'`\s<>]{1,220}?typeface\.json(?:[?#][^"'`\s<>]*)?)["'`]""",
    re.IGNORECASE,
)
_RX_SPLINECODE = re.compile(
    r"""["'`]([^"'`\s<>]{1,220}?\.splinecode(?:[?#][^"'`\s<>]*)?)["'`]""",
    re.IGNORECASE,
)
_RX_IMPORTMAP = re.compile(
    r"""<script[^>]+type\s*=\s*["']importmap["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_RX_WB_MANIFEST_URL = re.compile(
    r"""["']url["']\s*:\s*["']([^"']+)["']"""
)
_RX_WORKER_FILE = re.compile(
    (r"""["'`]([_PCH_]{1,120}?(?:worker|transcoder|draco|basis)[_PCH2_]{0,100}\.(?:js|wasm))["'`]"""
     .replace("_PCH_", _PCH).replace("_PCH2_", r"A-Za-z0-9_./\-")),
    re.IGNORECASE,
)
_RX_SETPATH = re.compile(r"""setPath\(\s*[`"'`]([^`"'()]{1,180})[`"'`]\s*\)""")
# /_next/image?url= originals (repair_sarang.py)
_RX_NEXT_IMAGE = re.compile(r"""/_next/image\?url=([^&"'`\s<>]+)""")
# direct media dirs (repair_sarang.py / scan_frames.py)
_DIRECT_DIRS = (r"frames|photo|skills|videos?|images?|assets|fonts?|media|"
                r"models?|textures?|data|static|common|3d-elements|"
                r"projects?|blog|icons?|logos?")
_RX_DIRECT_DIRS = re.compile(
    (r"""["'`](/(?:_DD_)/[_PCH_]{0,200})["'`]"""
     .replace("_DD_", _DIRECT_DIRS).replace("_PCH_", _PCH))
)
# video-frame numeric files: /frames/<folder>/<prefix><num>.<ext>
_RX_FRAME_FILE = re.compile(
    r"""/frames/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]*?)(\d{1,6})\.([A-Za-z0-9]+)"""
)
_RX_BARE_ASSET_DIRS = re.compile(
    (r"""["'`]([_PCH_]{1,220}?(?:/common/|/assets/(?:audio|fonts|images|geometries|libs)/)[_PCH2_]{0,150})["'`]"""
     .replace("_PCH_", _PCH).replace("_PCH2_", r"A-Za-z0-9_./%+?#&=@:$~\-"))
)

_SKIP_PREFIXES = ("data:", "blob:", "about:", "chrome:", "javascript:",
                  "mailto:", "tel:", "wss:", "ws:")
_SKIP_CONTAINS = ("<", ">", "\n", "\r", "\t")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _js_unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    s = s.replace("\\u0026", "&").replace("\\u0026", "&")
    s = re.sub(r"\\u([0-9a-fA-F]{4})",
               lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("&amp;", "&")
    return s


def normalize_ref(ref: str) -> str:
    """Clean one raw literal into a fetchable URL path."""
    ref = (ref or "").strip().strip("\"'`").strip()
    if not ref:
        return ""
    ref = _js_unescape(ref)
    ref = ref.strip()
    # drop trailing sentence punctuation accidentally captured
    ref = ref.rstrip(".,;")
    # literal spaces -> %20 (handles "about me.webp" style originals)
    if " " in ref:
        ref = ref.replace(" ", "%20")
    # collapse accidental .// inside path (keep scheme:// intact)
    scheme_split = ref.split("://", 1)
    if len(scheme_split) == 2:
        scheme, rest = scheme_split
        rest = re.sub(r"/{2,}", "/", rest)
        ref = scheme + "://" + rest
    else:
        ref = re.sub(r"/{2,}", "/", ref)
        if ref.startswith("/") and not ref.startswith("//") and "//" in ref:
            pass
    ref = re.sub(r"^[^A-Za-z0-9_~/]+", "", ref)
    if ref.startswith("static/chunks/"):
        ref = "/_next/" + ref
    elif ref.startswith("_next/"):
        ref = "/" + ref
    return ref


def _looks_fetchable(ref: str) -> bool:
    if not ref or len(ref) > 300 or len(ref) < 2:
        return False
    low = ref.lower()
    if low.startswith(_SKIP_PREFIXES):
        return False
    if any(c in ref for c in _SKIP_CONTAINS):
        return False
    if "${" in ref:  # unresolved template placeholder
        return False
    if "/" not in ref:
        return False  # bare code fragments (e.url, 23b) never resolve
    if "node_modules/" in ref:
        return False  # dev-source paths are never served
    if ref in (".", "..", "/", "\"", "'"):
        return False
    return True


def _strip_qs_frag(ref: str) -> str:
    base = ref.split("#", 1)[0].split("?", 1)[0]
    return base


def _ext_of(ref: str) -> str:
    base = _strip_qs_frag(ref).lower()
    # %20 etc: unquote for ext detection only
    base = urllib.parse.unquote(base)
    m = re.search(r"\.([a-z0-9]{2,5})$", base)
    return m.group(1) if m else ""


def _add(out: list, seen: set, ref: str) -> None:
    ref = normalize_ref(ref)
    if not _looks_fetchable(ref):
        return
    # drop absolute http(s) to foreign CDNs? keep root-relative + relative;
    # same-origin absolute is converted by crawl step, here keep as-is
    # but drop obvious third-party hosts to avoid cloning the internet
    if re.match(r"^https?://", ref, re.IGNORECASE):
        return
    if ref.startswith("\\\\"):
        return
    key = ref
    if key in seen:
        return
    seen.add(key)
    out.append(ref)


def is_bundle_ref(ref: str) -> bool:
    """True when a discovered ref is itself a JS/CSS bundle worth recursing."""
    base = _strip_qs_frag(ref).lower()
    return base.endswith((".js", ".css"))


# ---------------------------------------------------------------------------
# framework parsers (each appends via _add)
# ---------------------------------------------------------------------------

def _parse_webpack(text: str, out: list, seen: set) -> None:
    pref: dict = {}
    for m in _RX_PREF_MAP.findall(text):
        for cid, h in re.findall(r"""(\d+):["']([0-9a-f]{8})["']""", m):
            pref.setdefault(cid, h)
    for cid, h in _RX_ID_HASH.findall(text):
        h = h.lower()
        if not re.fullmatch(r"[0-9a-f]+", h):
            continue
        if len(h) < 12:
            continue
        _add(out, seen, "/_next/static/chunks/%s.%s.js" % (pref.get(cid, cid), h))
    for m in _RX_STATIC_CHUNKS.findall(text):
        _add(out, seen, "/_next/static/chunks/" + m)
    # bare chunk filenames with content hashes: 1032.abc123.js
    for m in _RX_CHUNK_FILE.findall(text):
        m = m.strip("./")
        if re.search(r"[0-9a-f]{8,}", m):
            _add(out, seen, "/_next/static/chunks/" + m)
    # miniCssF css chunk maps: same id/hash pairs near miniCssF marker
    for m in re.finditer(r"miniCssF", text):
        seg = text[m.start():m.start() + 6000]
        for cid, h in _RX_ID_HASH.findall(seg):
            if len(h) >= 8 and re.fullmatch(r"[0-9a-f]+", h.lower()):
                _add(out, seen, "/_next/static/chunks/%s.%s.css" % (cid, h))
                break  # one css entry per marker region is enough signal
    # flight payloads: __next_f.push([...]) bodies carry chunk urls
    for body in _RX_FLIGHT_CHUNK.findall(text):
        body = _js_unescape(body)
        for m in _RX_FLIGHT_REF.findall(body):
            _add(out, seen, m if m.startswith("/") else "/" + m.lstrip("./"))
        for m in _RX_NEXT_STATIC.findall(body):
            _add(out, seen, m)


def _parse_turbopack(text: str, out: list, seen: set) -> None:
    for m in _RX_TURBOPACK_MANIFEST.findall(text):
        _add(out, seen, m)
    for m in _RX_TURBOPACK_CHUNK.findall(text):
        m = _js_unescape(m)
        _add(out, seen, m if m.startswith("/") else "/" + m.lstrip("./"))
    for m in _RX_NEXT_STATIC.findall(_js_unescape(text)):
        _add(out, seen, m)


def _parse_vite(text: str, out: list, seen: set) -> None:
    for m in _RX_DYNAMIC_IMPORT.findall(text):
        m = (m or "").strip()
        if m and "${" not in m and _looks_fetchable(normalize_ref(m)):
            _add(out, seen, m)
    for m in _RX_VITE_ASSETS.findall(text):
        _add(out, seen, m)
    for m in _RX_VITE_LITERAL.findall(text):
        _add(out, seen, m)


def _parse_scene(text: str, out: list, seen: set) -> None:
    # astro/svelte/three/vanilla-3d: scene dirs, workers, loader paths
    for m in _RX_SCENE_DIRS.findall(text):
        _add(out, seen, m)
    for m in _RX_WORKER_FILE.findall(text):
        _add(out, seen, m)
    for m in _RX_SETPATH.findall(text):
        p = (m or "").strip()
        if p and "${" not in p:
            _add(out, seen, p if p.endswith("/") else p)
    for m in _RX_BARE_ASSET_DIRS.findall(text):
        _add(out, seen, m)


def _parse_loaders(text: str, out: list, seen: set) -> None:
    """three.js decoder/transcoder paths, typeface.json, splinecode, importmap, SW manifest."""
    for m in _RX_DECODER_PATH.findall(text):
        base = (m or "").strip()
        if base and "${" not in base:
            if not base.endswith("/"):
                base += "/"
            for f in ("draco_decoder.js", "draco_decoder.wasm",
                      "draco_wasm_wrapper.js", "basis_transcoder.js",
                      "basis_transcoder.wasm", "meshopt_decoder.js",
                      "meshopt_decoder.wasm"):
                _add(out, seen, base + f)
    for m in _RX_TYPEFACE_JSON.findall(text):
        _add(out, seen, m)
    for m in _RX_SPLINECODE.findall(text):
        _add(out, seen, m)
    for m in _RX_IMPORTMAP.findall(text):
        try:
            imports = json.loads(m).get("imports", {})
        except Exception:
            continue
        if isinstance(imports, dict):
            for _k, v in imports.items():
                if isinstance(v, str) and v:
                    _add(out, seen, v)
    for m in _RX_WB_MANIFEST_URL.findall(text):
        _add(out, seen, m)


_RX_ID_ARRAY = re.compile(
    r"""\["([A-Za-z0-9_.$/%-]{1,60}(?:"\s*,\s*"[A-Za-z0-9_.$/%-]{1,60}){1,})"\]"""
)
_RX_DIR_BASE = re.compile(r"""["'`](/(?:[A-Za-z0-9_.$%-]+/)+)["'`]""")
_RX_TMPL = re.compile(
    r"`([^`]{0,200}?)\$\{([^}]+)\}([^`]*?\.[A-Za-z0-9]{2,5})`"
)
_RX_MTMPL = re.compile(
    r"`([^`$]*?)\$\{([^}]+?)\}([^`$]*?)\$\{([^}]+?)\}([^`]*?\.[A-Za-z0-9]{2,5})`"
)
_NUMERIC_VAR = re.compile(r"^(?:_|i|j|k|n|x|y|o|h|idx|num|index|count)$")
_RX_QUOTED_ALT = re.compile(r""""([^"]*)"|'([^']*)'""")
_RX_ABS_ASSET = re.compile(r"https?://[^\"'`\s<>\\]+")
_ABS_ASSET_EXTS = frozenset([
    "js", "css", "drc", "ktx2", "wasm", "json", "bin", "glb",
    "gltf", "exr", "avif", "webp", "png", "jpg", "ogg",
    "mp3", "wav", "woff2", "ktx", "basis", "data",
])
_NOMINAL_VAR = re.compile(
    r"terrain|scene|name|path|url|level|mode|type|key|label|title|atlas",
    re.IGNORECASE,
)


def _join_url(*parts: str) -> str:
    """Join URL parts, collapsing accidental double slashes (not scheme)."""
    return re.sub(r"(?<!:)//+", "/", "".join(parts))


def _var_alts(var_expr: str) -> list:
    """Quoted alternatives inside a template var (ternary ``c?"a":""``)."""
    alts = []
    for m in _RX_QUOTED_ALT.finditer(var_expr or ""):
        alt = m.group(1) if m.group(1) is not None else m.group(2)
        if alt is not None and "$" not in alt and "{" not in alt:
            if alt not in alts:
                alts.append(alt)
        if len(alts) >= 8:
            break
    return alts


def _is_numeric_var(v2: str) -> bool:
    v = (v2 or "").strip()
    if _NUMERIC_VAR.match(v):
        return True
    if len(v) <= 3 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v or ""):
        return not bool(_NOMINAL_VAR.search(v))
    return False


def _top_segments(text: str, cap=15) -> list:
    """Most frequent path segments in quoted literals (terrain words etc)."""
    from collections import Counter as _Counter
    c: dict = {}
    for m in re.finditer(r"""["'`]([^"'`\s<>]{1,120})["'`]""", text):
        s = m.group(1)
        if "/" not in s or "://" in s:
            continue
        for seg in s.split("/"):
            seg = seg.strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,23}", seg or ""):
                c[seg] = c.get(seg, 0) + 1
    return [k for k, _v in sorted(c.items(), key=lambda kv: -kv[1])[:cap]]


def _loader_bases(text: str) -> list:
    """Loader root paths from setPath literals/templates (root-absolute only)."""
    bases = []
    for rx in (_RX_SETPATH, _RX_DECODER_PATH):
        for m in rx.findall(text):
            b = (m or "").strip()
            if "${" in b:
                b = re.sub(r"^\$\{[^}]+\}", "", b)
            if b.startswith("/") and b not in bases:
                bases.append(b if b.endswith("/") else b + "/")
    return bases[:10]


def _parse_templates(text: str, bases) -> list:
    """Backtick literals with a ${var} ending in an asset ext.

    Relative prefixes are resolved against every loader base so the
    caller can probe them all (404s culled over HTTP).
    """
    specs = []
    for m in _RX_TMPL.finditer(text):
        prefix, var, suffix = m.group(1), m.group(2), m.group(3)
        if "${" in prefix or "}" in prefix:
            continue
        if not suffix or "$" in suffix or "{" in suffix or "}" in suffix:
            continue
        if prefix.startswith("/") or re.match(r"^https?://", prefix, re.IGNORECASE):
            cands = [prefix]
        elif not prefix:
            cands = list(bases or [])
        else:
            cands = [b + prefix.lstrip("./") for b in (bases or [])]
            cands.append(prefix)
        specs.append({"type": "template", "prefixes": cands[:12],
                      "suffix": suffix, "fillers": _top_segments(text)})
        for alt in _var_alts(var):
            joined = prefix + alt + suffix
            if prefix.startswith("/") or re.match(r"^https?://", prefix, re.IGNORECASE):
                specs.append(joined)
            elif not prefix:
                specs.extend("%s%s" % (b, joined.lstrip("./")) for b in (bases or []))
                specs.append(joined)
            else:
                specs.extend("%s%s" % (b, joined.lstrip("./")) for b in (bases or []))
                specs.append(joined)
        if len(specs) >= 120:
            break
    return specs


def _parse_mtemplates(text: str, bases) -> list:
    """Two-variable templates (terrain/index shapes).

    One spec per (middle, suffix) with every rope x filler pre-joined
    (fillers outer so the top filler hits every loader base first).
    Numeric tails probe 0..N; ternary middles (``low/`` vs ````) come from
    quoted alternatives; LOD digit gaps (lod-1/lod-3 -> lod-2) are filled.
    """
    specs = []
    fillers = _top_segments(text)
    seen_mid = set()
    groups: dict = {}
    order: list = []
    for m in _RX_MTMPL.finditer(text):
        prefix, var1, middle, v2, suffix = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        if not suffix or "$" in suffix or "{" in suffix or "}" in suffix:
            continue
        if "${" in prefix or "}" in prefix:
            continue
        if "${" in middle or "}" in middle:
            continue
        key = (prefix, middle, (v2 or "").strip(), suffix)
        if key in seen_mid:
            continue
        seen_mid.add(key)
        alts = _var_alts(var1)
        mids = list(alts) if alts else []
        if not alts:
            mids.extend(fillers[:8])
            mids.extend("%s/" % f for f in fillers[:8])
            mids.append("")
        numeric = _is_numeric_var(v2)
        gkey = (prefix, suffix)
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append((middle, mids, numeric))
        if len(seen_mid) >= 60:
            break
    for gkey in order:
        prefix, suffix = gkey
        middles = groups[gkey]
        by_root: dict = {}
        for middle, _m, _n in middles:
            mm = re.match(r"^(.*?)(\d+)([^/]*)$", middle)
            if mm:
                by_root.setdefault((mm.group(1), mm.group(3)), set()).add(int(mm.group(2)))
        extra: dict = {}
        for (root, tail), nums in by_root.items():
            if len(nums) >= 2:
                for n in range(min(nums), max(nums) + 1):
                    if n not in nums:
                        extra.setdefault("%s%d%s" % (root, n, tail), None)
                        if len(extra) >= 8:
                            break
        for middle, mids, numeric in middles:
            all_mids = [middle] + [e for e in extra if e != middle]
            for mid in all_mids:
                if prefix.startswith("/") or re.match(r"^https?://", prefix, re.IGNORECASE):
                    ropes = [prefix]
                elif not prefix:
                    ropes = list(bases or [])
                else:
                    ropes = ([b + prefix.lstrip("./") for b in (bases or [])]
                             + ([prefix] if prefix else []))
                if numeric:
                    prefixes = []
                    for f in mids:
                        for b in ropes:
                            prefixes.append(_join_url(b, f, mid))
                    specs.append({"type": "template", "prefixes": prefixes,
                                  "suffix": suffix, "fillers": [], "pcap": 64})
                else:
                    for b in ropes[:12]:
                        for f in mids[:17]:
                            specs.append(_join_url(b, f, mid, suffix))
                    if len((v2 or "").strip()) <= 4:
                        prefixes = []
                        for f in mids[:17]:
                            for b in ropes:
                                prefixes.append(_join_url(b, f, mid))
                        specs.append({"type": "template", "prefixes": prefixes,
                                      "suffix": suffix, "fillers": [], "pcap": 64})
        if len(specs) >= 400:
            break
    return specs


def _parse_id_arrays(text: str) -> list:
    """Quoted ID arrays -> {"type": "ids"} pools for template probing.

    Games/data-driven sites keep ``["hair1","hair2",...]`` ID lists while
    loaders build paths at runtime. The caller combines the pool with
    template prefixes discovered in the same bundle.
    """
    specs = []
    for m in _RX_ID_ARRAY.findall(text):
        ids = [s for s in re.split(r'"\s*,\s*"', m)
               if s and " " not in s and "://" not in s
               and not re.fullmatch(r"_+", s or "")][:300]
        if len(ids) >= 2 and any(
                (len(i) >= 3 and re.search(r"[a-zA-Z]", i)
                 and re.search(r"[0-9]", i)) or "/" in i for i in ids):
            specs.append({"type": "ids", "ids": ids})
        if len(specs) >= 30:
            break
    path_specs = []
    for m in re.finditer(r"""["'`]([^"'`\s<>]*\/[^"'`\s<>]*)["'`]""", text):
        p = (m.group(1) or "").strip()
        if not p or " " in p or "://" in p or len(p) > 120:
            continue
        if _ext_of(p) or p.startswith(("/", "./", "../")):
            continue  # full literals / absolute paths handled elsewhere
        if re.fullmatch(r"_+", p or ""):
            continue
        path_specs.append({"type": "ids", "ids": [p]})
        if len(path_specs) >= 400:
            break
    return path_specs + specs


def _resolve_relative(text: str, out: list, seen: set, bases) -> None:
    """Resolve relative asset literals against every loader base.

    Bundles routinely say ``planets/present/x.drc`` while the loader root
    is ``/assets/geometries/``. Emits base+literal variants (404s culled
    over HTTP downstream).
    """
    if not bases:
        return
    made = 0
    for m in _RX_QUOTED_ASSET.findall(_js_unescape(text)):
        path = m[0] if isinstance(m, tuple) else m
        if not path or path.startswith(("/", "http://", "https://",
                                       "data:", "blob:")):
            continue
        for b in bases:
            _add(out, seen, b + path.lstrip("./"))
            made += 1
            if made >= 3000:
                return


def _parse_generic(text: str, out: list, seen: set) -> dict:
    """Extension literals, css url(), next/image, direct dirs. Returns frames."""
    ute = _js_unescape(text)
    for m in _RX_QUOTED_ASSET.findall(ute):
        path = m[0] if isinstance(m, tuple) else m
        _add(out, seen, path)
    for m in _RX_CSS_URL.findall(ute):
        _add(out, seen, m)
    for m in _RX_NEXT_STATIC.findall(ute):
        _add(out, seen, m)
    for m in _RX_NEXT_IMAGE.findall(ute):
        try:
            orig = urllib.parse.unquote(m)
        except Exception:
            orig = m
        _add(out, seen, orig if orig.startswith("/") else "/" + orig)
        full = "/_next/image?url=" + m
        # keep a normalized full form too (query kept) for completeness
        key = full
        if key not in seen and _looks_fetchable(orig):
            seen.add(key)
            out.append(normalize_ref(key))
    for m in _RX_DIRECT_DIRS.findall(ute):
        _add(out, seen, m)
    for m in _RX_ABS_ASSET.findall(ute):
        u = (m or "").strip().rstrip(".,;")
        if not u or "${" in u or "{" in u or "}" in u:
            continue
        if (_ext_of(u).lower() or "") not in _ABS_ASSET_EXTS:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return _collect_frames(ute)


_RX_SEQ_OPEN = re.compile(r"""sequence\s*:\s*\{""")
_RX_SEQ_STR = lambda f: re.compile(
    f + r"""\s*:\s*[`"'`]([^`"'`]{1,80})[`"'`]""")
_RX_SEQ_NUM = lambda f: re.compile(
    f + r"""\s*:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)""")


def _parse_frame_seqs(text: str) -> list:
    specs = []
    for m in _RX_SEQ_OPEN.finditer(text):
        seg = text[m.end():m.end() + 600].split("}", 1)[0]
        try:
            folder = _RX_SEQ_STR("folder").search(seg).group(1)
            prefix = _RX_SEQ_STR("prefix").search(seg).group(1)
            start = int(float(_RX_SEQ_NUM("startIndex").search(seg).group(1)))
            end = int(float(_RX_SEQ_NUM("endIndex").search(seg).group(1)))
        except AttributeError:
            continue
        try:
            ext = _RX_SEQ_STR("extension").search(seg).group(1)
        except AttributeError:
            ext = "webp"
        try:
            pad = int(float(_RX_SEQ_NUM("padLength").search(seg).group(1)))
        except AttributeError:
            pad = 0
        if "${" in folder + prefix + ext:
            continue
        specs.append({"type": "frames",
                      "template": "/frames/%s/%s{}.%s" % (folder, prefix, ext),
                      "start": start, "end": end, "pad": pad,
                      "ext": ext, "folder": folder, "prefix": prefix})
    return specs


def _collect_frames(text: str) -> list:
    """Cluster /frames/<folder>/<prefix><num>.<ext> into expansion specs."""
    groups: dict = {}
    for folder, prefix, num, ext in _RX_FRAME_FILE.findall(text):
        key = (folder, prefix, ext.lower(), len(num))
        g = groups.setdefault(key, {"min": None, "max": None, "count": 0})
        n = int(num)
        g["count"] += 1
        g["min"] = n if g["min"] is None else min(g["min"], n)
        g["max"] = n if g["max"] is None else max(g["max"], n)
    specs = []
    for (folder, prefix, ext, width), g in sorted(groups.items()):
        if g["count"] < 1 or g["min"] is None:
            continue
        pad = width if width >= 2 and g["count"] >= 2 else 0
        template = "/frames/%s/%s{}.%s" % (folder, prefix, ext)
        specs.append({"type": "frames", "template": template,
                      "start": g["min"], "end": g["max"], "pad": pad,
                      "ext": ext, "folder": folder, "prefix": prefix})
    return specs


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

_FRAMEWORKS = ("webpack", "turbopack", "vite", "astro", "svelte",
               "generic", "next", "three")


def discover(bundle_text: str, framework=None) -> list:
    """Discover asset refs hidden inside one JS/CSS bundle's text.

    Args:
        bundle_text: decoded bundle source (str or bytes).
        framework: hint string; None/"generic" runs every parser.

    Returns:
        Order-stable de-duplicated list of URL-path strings plus zero
        or more ``frames`` expansion dicts (see module docstring).
        On a saved Next.js webpack chunk this includes the known
        dynamic ``<id>.<hash>.js`` chunk paths; on a Three.js bundle
        the ``.glb`` / ``.drc`` / ``.ktx2`` literal lists.
    """
    if isinstance(bundle_text, (bytes, bytearray)):
        text = bytes(bundle_text).decode("utf-8", errors="replace")
    else:
        text = bundle_text or ""
    if not text:
        return []
    fw = (framework or "generic").lower().strip() or "generic"
    out: list = []
    seen: set = set()
    run_all = fw in ("generic", "auto", "unknown", "")
    if fw in ("webpack", "next", "nextjs") or run_all:
        _parse_webpack(text, out, seen)
    if fw in ("turbopack",) or run_all:
        _parse_turbopack(text, out, seen)
    if fw in ("vite",) or run_all:
        _parse_vite(text, out, seen)
    if fw in ("astro", "svelte", "three", "threejs") or run_all:
        _parse_scene(text, out, seen)
    if run_all or fw not in _FRAMEWORKS:
        pass  # generic below still runs for every framework hint
    _parse_loaders(text, out, seen)
    out.extend(_parse_id_arrays(text))
    _lb = _loader_bases(text)
    out.extend(_parse_templates(text, _lb))
    out.extend(_parse_mtemplates(text, _lb))
    _resolve_relative(text, out, seen, _lb)
    specs = _parse_generic(text, out, seen)
    specs.extend(s for s in _parse_frame_seqs(text)
                 if (s.get("template"), s.get("start"), s.get("end"))
                 not in {(d.get("template"), d.get("start"), d.get("end"))
                         for d in specs if isinstance(d, dict)})
    out.extend(specs)
    return out


def expand_deep_file(url: str, blob) -> list:
    """Extract nested asset refs from a downloaded deep file.

    Handles ``.gltf`` JSON + ``.glb`` JSON chunk (buffers/images uris),
    lottie-shaped JSON (assets ``u`` + ``p``), ``.splinecode`` embedded URLs.
    Returns raw refs (caller resolves against the file URL); ``data:`` excluded.
    """
    refs: list = []
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return refs
    if isinstance(blob, (bytes, bytearray)):
        raw = bytes(blob)
    else:
        raw = str(blob or "").encode("utf-8", errors="replace")
    text = None
    if path.endswith(".glb"):
        try:
            import struct as _st
            (jlen,) = _st.unpack("<I", raw[12:16])
            text = raw[20:20 + jlen].decode("utf-8")
        except Exception:
            return refs
    else:
        try:
            text = raw.decode("utf-8")
        except Exception:
            return refs
    if path.endswith((".gltf", ".glb")):
        try:
            doc = json.loads(text)
        except Exception:
            return refs
        for buf in doc.get("buffers", []) or []:
            u = (buf.get("uri") or "").strip() if isinstance(buf, dict) else ""
            if u and not u.startswith("data:"):
                refs.append(u)
        for img in doc.get("images", []) or []:
            u = (img.get("uri") or "").strip() if isinstance(img, dict) else ""
            if u and not u.startswith("data:"):
                refs.append(u)
        return refs
    is_json = path.endswith((".json", ".splinecode")) or (text.lstrip()[:1] in ("{", "["))
    if not is_json:
        return refs
    try:
        doc = json.loads(text)
    except Exception:
        doc = None
    lottie = isinstance(doc, dict) and isinstance(doc.get("assets"), list)
    if lottie:
        for a in doc.get("assets", []) or []:
            if isinstance(a, dict):
                u = (a.get("u") or "").strip()
                p = (a.get("p") or "").strip()
                if p and not p.startswith("data:"):
                    refs.append(u + p)
    if path.endswith(".splinecode") or lottie:
        for m in re.finditer(r"https?://[^\"'\\s<>]+", text):
            refs.append(m.group(0))
    return refs


def _fetch(session, url: str, fetcher, timeout: int = 40):
    if fetcher is not None:
        return fetcher(url)
    headers = {"User-Agent": UA, "Accept": "*/*", "Referer": url}
    if session is not None:
        r = session.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.content
    if requests is not None:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.content
    import urllib.request as _u
    req = _u.Request(url, headers=headers)
    with _u.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _local_path(out_dir: str, origin: str, url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path or "/")
    if not path or path.endswith("/"):
        path = path + "index.html"
    rel = path.lstrip("/")
    if parsed.query and _strip_qs_frag(url).lower().startswith("http"):
        pass
    full = os.path.join(out_dir, rel)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return full


def _to_absolute(origin: str, base_url: str, ref: str) -> str:
    ref = normalize_ref(ref)
    if re.match(r"^https?://", ref, re.IGNORECASE):
        return ref
    base = base_url or (origin.rstrip("/") + "/")
    # site-absolute refs resolve against origin; relative against bundle url
    if ref.startswith("/"):
        return urllib.parse.urljoin(origin.rstrip("/") + "/", ref)
    return urllib.parse.urljoin(base, ref)


def _to_site_path(origin: str, abs_url: str) -> str:
    try:
        o = urllib.parse.urlparse(origin)
        u = urllib.parse.urlparse(abs_url)
    except Exception:
        return abs_url
    if o.netloc and u.netloc and o.netloc.lower() == u.netloc.lower():
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        return normalize_ref(path)
    return abs_url


def crawl_bundles(session, origin, entry_js_urls, framework=None,
                  fetcher=None, max_depth: int = 3, out_dir=None,
                  timeout: int = 40, save: bool = False) -> list:
    """Download bundles and discover assets recursively to closure.

    Args:
        session: requests.Session (or None when ``fetcher`` is given).
        origin: site origin, e.g. ``https://example.com``.
        entry_js_urls: starting bundle URLs (absolute or site-relative).
        framework: hint forwarded to :func:`discover`.
        fetcher: optional callable(url) -> str|bytes|None overriding
            network access (tests / offline closure).
        max_depth: recursion depth for JS/CSS bundles (default 3).
        out_dir: optional directory to mirror downloaded bundles into
            (parent dirs created as needed).
        timeout: per-request seconds.
        save: when True (or out_dir given) persist fetched bundles.

    Returns:
        Order-stable de-duplicated asset list (strings + frames specs).
    """
    origin = (origin or "").rstrip("/")
    queue: list = []
    seen_bundle: set = set()
    for u in (entry_js_urls or []):
        u = normalize_ref(str(u))
        if not u:
            continue
        abs_u = _to_absolute(origin, origin + "/", u)
        key = abs_u.split("#", 1)[0]
        if key not in seen_bundle:
            seen_bundle.add(key)
            queue.append((abs_u, 0))
    assets: list = []
    asset_seen: set = set()

    def _emit(ref_or_spec) -> None:
        if isinstance(ref_or_spec, dict):
            key = ("dict", json.dumps(ref_or_spec, sort_keys=True, default=str))
            if key not in asset_seen:
                asset_seen.add(key)
                assets.append(ref_or_spec)
            return
        ref = normalize_ref(str(ref_or_spec))
        if not _looks_fetchable(ref):
            return
        key = ref
        if key not in asset_seen:
            asset_seen.add(key)
            assets.append(ref)

    depth = 0
    idx = 0
    while idx < len(queue):
        abs_u, depth = queue[idx]
        idx += 1
        if depth > max_depth:
            continue
        try:
            raw = _fetch(session, abs_u, fetcher, timeout=timeout)
        except Exception:
            continue
        if raw is None:
            continue
        if isinstance(raw, (bytes, bytearray)):
            blob = bytes(raw)
            try:
                text = blob.decode("utf-8")
            except Exception:
                text = blob.decode("utf-8", errors="replace")
        else:
            text = str(raw)
            blob = text.encode("utf-8", errors="replace")
        if (save or out_dir) and out_dir:
            try:
                _local_path(out_dir, origin, abs_u)
                with open(_local_path(out_dir, origin, abs_u), "wb") as f:
                    f.write(blob)
            except Exception:
                pass
        for item in discover(text, framework):
            if isinstance(item, dict):
                _emit(item)
                continue
            _emit(item if item.startswith("/") or
                  re.match(r"^https?://", item, re.IGNORECASE)
                  else "/" + item.lstrip("./") if item.startswith(("_next/", "static/")) else item)
            # recurse into child JS/CSS bundles under the same origin
            base = _strip_qs_frag(item).lower()
            if base.endswith((".js", ".css")) and depth < max_depth:
                child_abs = _to_absolute(origin, abs_u, item)
                try:
                    host = urllib.parse.urlparse(child_abs).netloc.lower()
                    oh = urllib.parse.urlparse(origin).netloc.lower()
                except Exception:
                    continue
                if oh and host and host != oh:
                    continue
                ckey = child_abs.split("#", 1)[0]
                if ckey not in seen_bundle:
                    seen_bundle.add(ckey)
                    queue.append((child_abs, depth + 1))
    return assets
