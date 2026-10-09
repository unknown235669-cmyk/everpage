"""Stage 1 download engine: session, retrying GET, disk layout, threaded fetch.

Ported from the proven logic in D:\\search\\webdow\\mirror.py and
D:\\search\\webdow\\mirror2.py, generalized (no hardcoded hosts/paths).

Public API used by sibling stages:
    fetch_page(session, url) -> str
    download_asset(session, origin, ref, outdir) -> str | None
"""

import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

# Full Chrome 126 header set. The Sec-Fetch-*/Sec-Ch-Ua/Upgrade-Insecure-Requests
# lines are what keep picky hosts (444-on-suspicion) answering instead of hanging up.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CHROME_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A/Brand";v="8"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# Statuses worth one more attempt (rate-limit / transient server errors).
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

# Refs that are never network assets.
_SKIP_PREFIXES = ("data:", "mailto:", "tel:", "javascript:", "blob:", "#")

_INVALID_SEGMENT_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def make_session(referer=None):
    """Build a requests.Session preloaded with full Chrome 126 headers."""
    session = requests.Session()
    session.headers.update(CHROME_HEADERS)
    if referer:
        session.headers["Referer"] = referer
    return session


def _is_dns_failure(exc):
    """True when the exception text smells like name resolution failure."""
    text = str(exc).lower()
    return (
        "getaddrinfo" in text
        or "failed to resolve" in text
        or "name or service not known" in text
        or "temporary failure in name resolution" in text
        or isinstance(exc, requests.exceptions.ConnectionError)
    )


def get(session, url, retries=3, backoff=1.0, timeout=30):
    """GET with retry on DNS/connection failures and transient HTTP statuses.

    Attempts = 1 initial + ``retries`` follow-ups, exponential backoff
    (backoff * 2**attempt). Raises the last exception / HTTPError when
    everything is exhausted.
    """
    last_exc = None
    attempts = max(1, 1 + int(retries))
    for attempt in range(attempts):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code in RETRY_STATUSES and attempt < attempts - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                # DNS failures, drops and 5xx all deserve another lap.
                _ = _is_dns_failure(exc)  # informational; retry either way
                time.sleep(backoff * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    raise requests.exceptions.RetryError("get() exhausted retries for %s" % url)


def fetch_page(session, url):
    """Fetch a page and return its decoded text. Raises on failure."""
    return get(session, url).text


def _sanitize_segment(segment):
    segment = urllib.parse.unquote(segment)
    segment = _INVALID_SEGMENT_CHARS.sub("_", segment)
    segment = segment.strip().rstrip(".")
    return segment or "_"


def local_path_for(outdir, urlpath):
    """Map a URL (or bare path) to a file path under ``outdir``.

    Preserves URL path structure. Query strings and fragments are stripped
    here — the one exception is ``/_next/image`` optimizer URLs, which the
    *caller* must flatten into a filename before calling (their query carries
    the real source path). Bare directories map to ``index.html``.
    """
    parsed = urllib.parse.urlparse(urlpath)
    path = parsed.path or "/"
    rel = path.lstrip("/")
    if not rel or rel.endswith("/"):
        rel = os.path.join(rel, "index.html") if rel else "index.html"
    query = (parsed.query or "").strip()
    if query:
        import re as _re
        orig_ext = os.path.splitext(path)[1].lower()
        if not orig_ext:
            # Page-like URL (?p=, ?page=, ?s=): each query variant is
            # different content, so keep it in the filename. Asset URLs
            # (?ver=, ?5.49.0 cache-busters) keep query-stripping below.
            base, _ext = os.path.splitext(rel)
            q = _re.sub(r"[^A-Za-z0-9_.-]+", "_", query).strip("._")[:80]
            if q:
                rel = "%s__%s.html" % (base or "index", q)
    parts = [_sanitize_segment(p) for p in rel.split("/") if p]
    if not parts:
        parts = ["index.html"]
    return os.path.join(outdir, *parts)


def save(content, outdir, urlpath):
    """Write ``content`` (bytes or str) to disk mirroring ``urlpath``.

    Creates parent dirs as needed. Returns the absolute local path written.
    """
    full = os.path.abspath(local_path_for(outdir, urlpath))
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if isinstance(content, str):
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    else:
        if bytes(content[:2]) == b"\x1f\x8b":
            try:
                import gzip as _gzip
                content = _gzip.decompress(bytes(content))
            except Exception:
                pass  # keep raw bytes when decompression fails
        with open(full, "wb") as fh:
            fh.write(content)
    return full


def _resolve_ref(origin, ref):
    """Resolve a raw HTML ref against ``origin``; None when not downloadable."""
    if not ref:
        return None
    ref = ref.strip()
    if not ref or ref.startswith(_SKIP_PREFIXES):
        return None
    if ref.startswith("//"):
        ref = urllib.parse.urlparse(origin).scheme + ":" + ref
    full = urllib.parse.urljoin(origin if origin.endswith("/") else origin + "/", ref)
    return full


def download_asset(session, origin, ref, outdir):
    """Download one asset ref (relative, absolute-path or full URL) to ``outdir``.

    Same-host only: absolute URLs on foreign hosts return None so the caller
    can decide (log SKIP, queue separately, ...). Returns the local path on
    success, None on skip/failure (never raises).
    """
    full = _resolve_ref(origin, ref)
    if full is None:
        return None
    origin_host = urllib.parse.urlparse(origin).netloc.lower()
    asset_host = urllib.parse.urlparse(full).netloc.lower()
    if asset_host and asset_host != origin_host:
        return None  # external — caller decides
    try:
        resp = get(session, full)
    except Exception:
        return None
    try:
        return save(resp.content, outdir, full)
    except OSError:
        return None


def _normalize_job(job, default_outdir):
    """Accept str | (url, outdir?) | {url, outdir?, urlpath?} -> (url, outdir, urlpath)."""
    if isinstance(job, str):
        return job, default_outdir, job
    if isinstance(job, (tuple, list)):
        url = job[0]
        outdir = job[1] if len(job) > 1 and job[1] else default_outdir
        urlpath = job[2] if len(job) > 2 and job[2] else url
        return url, outdir, urlpath
    if isinstance(job, dict):
        url = job.get("url")
        return url, job.get("outdir", default_outdir), job.get("urlpath", url)
    raise TypeError("unsupported job type: %r" % type(job))


def _download_one(job, default_outdir, referer, state):
    url, outdir, urlpath = _normalize_job(job, default_outdir)
    if not url or not outdir:
        line = "FAIL %s: missing url or outdir" % (url,)
        print(line, flush=True)
        with state["lock"]:
            state["fail"].append((url, "missing url or outdir"))
        return
    if state.get("session_factory") is not None:
        session = state["session_factory"]()
    else:
        thread_local = state["thread_local"]
        session = getattr(thread_local, "session", None)
        if session is None:
            session = make_session(referer)
            thread_local.session = session
    try:
        resp = get(session, url)
        path = save(resp.content, outdir, urlpath)
        line = "\u2713 %s (%db)" % (url, len(resp.content))
        print(line, flush=True)
        with state["lock"]:
            state["ok"].append((url, path))
    except Exception as exc:
        line = "\u2717 %s: %s" % (url, exc)
        print(line, flush=True)
        with state["lock"]:
            state["fail"].append((url, str(exc)))


def download_many(jobs, outdir=None, workers=8, referer=None, session_factory=None):
    """Fetch many URLs concurrently (one Session per thread).

    ``jobs``: list of URL strings, (url, outdir[, urlpath]) tuples, or
    {url, outdir?, urlpath?} dicts. ``outdir`` is the fallback output dir
    for jobs that don't carry their own.

    Returns {"ok": [(url, localpath)], "fail": [(url, error)]} and prints an
    OK/FAIL line per job plus a summary line.
    """
    jobs = list(jobs or [])
    state = {
        "ok": [],
        "fail": [],
        "lock": threading.Lock(),
        "thread_local": threading.local(),
        "session_factory": session_factory,
    }
    if referer is None and jobs:
        try:
            first_url, _, _ = _normalize_job(jobs[0], outdir)
            parsed = urllib.parse.urlparse(first_url)
            if parsed.scheme and parsed.netloc:
                referer = "%s://%s/" % (parsed.scheme, parsed.netloc)
        except Exception:
            pass
    max_workers = max(1, int(workers or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for job in jobs:
            pool.submit(_download_one, job, outdir, referer, state)
    print(
        "download_many: %d OK, %d FAIL out of %d" % (len(state["ok"]), len(state["fail"]), len(jobs)),
        flush=True,
    )
    return {"ok": state["ok"], "fail": state["fail"]}
