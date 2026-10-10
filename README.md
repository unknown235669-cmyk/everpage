<div align="center">

# Everpage

### Every page, forever — paste a link, get a working 1:1 offline clone.

[![License: MIT](https://img.shields.io/badge/License-MIT-cyan.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![No heavy deps](https://img.shields.io/badge/deps-requests%20%2B%20playwright-green.svg)](requirements.txt)
[![Verified on live sites](https://img.shields.io/badge/verified-4%20production%20sites-magenta.svg)](#proven-in-the-field)

*Built for JS-heavy sites where classic mirrors only capture a shell —
hashed bundles, runtime-built asset URLs, web workers, 3D payloads,
fonts, media. Downloaded, rewritten to local paths, served, and
headless-verified.*

</div>

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

The name is the promise. If a page was public, Everpage keeps it.

---

## Quickstart

```bash
pip install -r requirements.txt
playwright install chromium   # one-time, for the trace + verify stages
```

Interactive menu:

```bash
python -m everpage.tui
```

Direct CLI:

```bash
python -m everpage.cli https://example.com/ -o ./example-clone --port 8919
```

Useful flags: `--pages about,pricing` · `--max-pages 200`
(sitemap + link crawl cap) · `--no-crawl` · `--no-trace`
(skip the headless pass — faster, misses runtime-only assets).

Every clone ships `OPEN-ME.bat` (or `python serve_<name>.py <port>`)
for instant local preview.

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
| `verify` | Headless reload: JS errors, failed requests, screenshots. Same-host requests for files already on disk don't count (headless aborts streaming downloads — the bytes are what matter) |

`python smoke_test.py` runs the offline self-checks (no network).

## Everpage vs the rest

Researched against the tools' own docs (October 2026): [websnap](https://github.com/uirip/websnap),
[SingleFile FAQ](https://github.com/gildas-lormeau/SingleFile/blob/master/faq.md),
[Browsertrix docs](https://docs.browsertrix.com/), [independent 2026 roundup](https://webdoner.com/best-website-copier-tools/).

|  | Everpage | HTTrack | SingleFile | websnap | Browsertrix | Commercial copiers |
|---|---|---|---|---|---|---|
| JS-bundle / hashed-chunk sites | ✅ | ❌ shell only | ⚠️ partial | ✅ | ✅ | ⚠️ varies |
| Runtime-built asset URLs | ✅ mined + traced | ❌ | ❌ | ✅ observed | ✅ recorded | ⚠️ varies |
| Interactive states (modals, tabs, clicks) | ❌ passive trace only | ❌ | ❌ scripts stripped by default | ✅ state-tree crawl | ⚠️ replay only | ⚠️ varies |
| Workers / WASM / 3D that boot offline | ✅ verified boots | ❌ | ❌ | ⚠️ snapshot-oriented | ⚠️ inside WARC only | ⚠️ varies |
| Double-click working preview | ✅ `OPEN-ME.bat` | ⚠️ fix links yourself | ✅ single file | ✅ static HTML | ❌ replay stack needed | ⚠️ varies |
| Boot verification (errors + shots) | ✅ | ❌ | ❌ | ❌ | ⚠️ crawl reports | ❌ |
| Login-walled content | ❌ | ❌ | ❌ | ✅ documented | ✅ via profiles | ⚠️ varies |
| Backend logic / DB / payments | ❌ stubbed | ❌ | ❌ | ❌ | ❌ | ❌ |
| Setup weight | pip + one browser | preinstalled | extension | npm + browser | docker | signup + $$$ |

One independent roundup concluded that *a working offline website*
is delivered by "none of the above." That's the gap Everpage was
built to close — measured file parity and clean headless boots on
production Three.js, Astro, Vite SPA and WordPress sites, and honest
`❌` marks everywhere else.

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

Issues with a failing URL + the probe log tail are gold. PRs that add a
`self_test` regression case alongside any parser change get merged fastest.

## License

MIT — see [LICENSE](LICENSE).
