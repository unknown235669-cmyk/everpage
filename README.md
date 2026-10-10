<div align="center">

# Everpage

### Every page, forever. Paste a link → get a working 1:1 offline website clone.

[![License: MIT](https://img.shields.io/badge/License-MIT-cyan.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Deps](https://img.shields.io/badge/deps-requests%20%2B%20playwright-green.svg)](requirements.txt)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey.svg)](#quickstart)

*The open-source HTTrack alternative for the modern web. Download entire
websites — JavaScript-rendered SPAs, hashed bundles, web workers, WASM,
3D scenes, fonts, media — rewritten to local paths, served offline with
one double-click, and headless-verified. Where `wget` and HTTrack save
empty shells, Everpage saves the working site.*

`website-copier` · `offline-mirror` · `website-downloader` · `save-website-offline` · `httrack-alternative` · `mirror-website` · `spa-archiver`

</div>

---

## Why I built this

Hi — I clone websites for a living hobby, and I got tired of tools
that promised the whole site and handed me an empty shell. You know
the feeling: the download finishes, you open `index.html`, and it's a
blank page staring back because everything real lived inside JavaScript
bundles the copier never understood.

So I built the tool I wished existed. Everpage doesn't guess what a
site needs — it reads the bundles the way the browser does, follows
the URLs the app itself resolves at runtime, rewrites every path so
the copy boots offline, and then *proves it* by reloading the clone
headless and checking for errors. If a page was public, Everpage keeps
it. That's the whole philosophy, and the name is the promise:
*every page, forever.*

— Girivasan

---

## About Everpage

Most of the web is already gone. Sites redesign, startups die,
platforms rot — and the tools that promised to preserve them either
saved empty JavaScript shells or locked the copy inside an archive
only specialists can open.

Everpage exists to keep the whole thing: *every page, forever.* Not a
screenshot, not a folder of broken links — the complete working site,
every file, bootable offline with one double-click. It was built the
hard way, against production Three.js worlds and CMS sprawl, until
file-level parity and clean headless boots stopped being aspirations
and became the test suite.

---

## Quickstart

```bash
pip install everpage
playwright install chromium   # one-time, for the trace + verify stages
```

Interactive menu (just type `everpage` — the menu opens by itself):

```bash
everpage
```

Or go direct when you know what you want:

```bash
python -m everpage.cli https://example.com/ -o ./example-clone --port 8919
```

Useful flags: `--pages about,pricing` · `--max-pages 200`
(sitemap + link crawl cap) · `--no-crawl` · `--no-trace`
(skip the headless pass — faster, misses runtime-only assets).

Every offline mirror ships `OPEN-ME.bat`
(or `python serve_<name>.py <port>`) for instant local preview —
double-click it and the cloned site just opens in your browser.

## Proven where it matters

Everpage has cloned production sites end to end — Three.js 3D worlds,
WebGL portfolios, WordPress portals, Astro CMS sites — and every one
boots offline clean. Don't take my word for it: paste a link and
watch it happen.

## Everpage vs the rest

I researched this table from the tools' own docs (October 2026), not
from marketing pages:
[websnap](https://github.com/uirip/websnap),
[SingleFile FAQ](https://github.com/gildas-lormeau/SingleFile/blob/master/faq.md),
[Browsertrix docs](https://docs.browsertrix.com/),
[independent 2026 roundup](https://webdoner.com/best-website-copier-tools/).
Where we lose a row, the `❌` stays — I'd rather earn your star than
trick you out of it.

|  | **Everpage** | HTTrack (3.49, dormant since 2017) | SingleFile (+CLI) | websnap (2026) | Browsertrix | Commercial copiers |
|---|---|---|---|---|---|---|
| Full-site crawl (sitemap + links, pagination) | ✅ up to 200 pages | ✅ | ⚠️ URL list, flaky at scale | ✅ | ✅ | ⚠️ varies |
| JS-bundle / hashed-chunk apps that boot | ✅ verified | ❌ empty shell | ⚠️ single page only | ✅ snapshots | ✅ in replay | ⚠️ varies |
| Runtime-built asset URLs (JS templates) | ✅ mined + traced | ❌ | ❌ | ✅ observed | ✅ recorded | ⚠️ varies |
| Interactive states (modals, tabs, clicks) | ❌ passive trace only | ❌ | ❌ scripts stripped by default | ✅ state-tree crawl | ⚠️ replay only | ⚠️ varies |
| Workers / WASM / 3D that boot offline | ✅ verified | ❌ | ❌ | ⚠️ snapshot-oriented | ⚠️ inside WARC only | ⚠️ varies |
| Double-click working preview | ✅ `OPEN-ME.bat` | ⚠️ fix links yourself | ✅ single file | ✅ static HTML | ❌ replay stack needed | ⚠️ varies |
| Boot verification (errors + shots) | ✅ built in | ❌ | ❌ | ❌ | ⚠️ crawl reports | ❌ |
| Login-walled content | ❌ | ❌ | ❌ | ✅ documented | ✅ via profiles | ⚠️ varies |
| Backend logic / DB / payments | ❌ stubbed | ❌ | ❌ | ❌ | ❌ | ❌ |
| Setup weight | pip + one browser | preinstalled | extension / npm | npm + browser | docker / k8s | signup + $$$ |

One independent roundup concluded that *a working offline website*
is delivered by "none of the above." That's the gap I built Everpage
to close.

## How it works

| Stage | What it does |
|---|---|
| `fetch` | Homepage + manifest with real-browser headers, gzip handled |
| `detect` | Framework fingerprint (Next / Vite / Astro / WordPress / Three.js…) |
| `crawl` | Sitemap + same-origin links as first-class pages, recursion included |
| `trace` | One headless pass records URLs the app resolves at runtime |
| `assets` / `chunks` | Bundle mining: hashed chunks, loader roots, backtick templates (incl. multi-var `terrain/index` shapes), ID pools, ternary alternatives, LOD-gap interpolation, absolute same-origin worker URLs |
| `rewrite` | Root-absolute → depth-relative paths; absolute same-origin URLs folded to local in HTML/CSS/JS/JSON; query-page URLs (`?p=`, `?page=`) self-name instead of clobbering `index.html` |
| `serve` | Offline preview server with API/beacon stubs |
| `verify` | Headless reload: JS errors, failed requests, screenshots |

`python smoke_test.py` runs the offline self-checks (no network).

## Honest scope

I'll tell you straight what it can't do, so you never feel misled:

- **Backends aren't cloned** — APIs are stubbed with captured payloads.
- **Gated content isn't reachable** — logins, paywalls, DRM and aggressive anti-bot need a real session.
- **Live state isn't frozen** — websockets, personalization and per-user feeds snapshot to whatever loaded.
- **Interaction-gated assets can be missed** — the trace pass scrolls and idles; scripted clicking (à la websnap) is on the roadmap.

## Layout

```
everpage/
  cli.py        pipeline orchestration
  fetcher.py    sessions, retries, URL→file mapping, parallel downloads
  detect.py     framework + HTML asset/link extraction
  chunks.py     JS bundle mining (templates, pools, workers, frames)
  rewrite.py    offline path rewriting (+ regression self_test)
  trace.py      headless runtime URL capture
  serve.py      preview server with API stubs
  verify.py     headless boot/error/screenshot check
  tui.py        interactive menu
```

## Contributing

Found a site it chokes on? Open an issue with the URL and the probe
log tail — that's gold for me. If you're fixing a parser, please add a
`self_test` regression case next to it so it never breaks again. All
PRs get a real review from a human (me), usually within days.

## License

MIT — see [LICENSE](LICENSE). Do anything with it; a shout-out is
appreciated but never required.
