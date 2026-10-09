<div align="center">

# WebDow

### Paste a link → get a working 1:1 offline clone.

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

## Quickstart

```bash
pip install -r requirements.txt
playwright install chromium   # one-time, for the trace + verify stages
```

Interactive menu:

```bash
python -m webdow.tui
```

Direct CLI:

```bash
python -m webdow.cli https://example.com/ -o ./example-clone --port 8919
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

## WebDow vs the rest

|  | WebDow | `wget` / HTTrack | SingleFile | Browser archivers (Browsertrix…) | Commercial copiers |
|---|---|---|---|---|---|
| JS-bundle / hashed-chunk sites | ✅ | ❌ shell only | ⚠️ partial | ✅ | ⚠️ varies |
| Runtime-built asset URLs | ✅ mined + traced | ❌ | ❌ | ✅ observed | ⚠️ varies |
| Web workers / WASM / 3D payloads | ✅ | ❌ | ❌ | ✅ | ⚠️ |
| Working local preview server | ✅ + `OPEN-ME.bat` | ❌ broken links | n/a (one file) | ❌ replay stack needed | ⚠️ varies |
| Headless boot verification | ✅ errors + shots | ❌ | ❌ | ⚠️ crawl stats only | ❌ |
| Single-file portability | ❌ (folder clone) | ❌ | ✅ best in class | ❌ | ❌ |
| Login-walled / paywalled content | ❌ | ❌ | ❌ | ⚠️ with scripted auth | ⚠️ varies |
| Backend logic / DB / payments | ❌ stubbed | ❌ | ❌ | ❌ | ❌ |
| Interaction crawling (clicks/tabs) | ❌ passive trace only | ❌ | ❌ | ✅ scripted behaviors | ⚠️ varies |
| Setup weight | pip + one browser | preinstalled | extension | docker / heavy | signup + $$$ |

No tool on that table clones a backend — server logic, sessions and
payments stay server-side everywhere. WebDow is honest about it: `/api/*`
and tracker calls get stubbed, and the README of every clone says so.

## Proven in the field

| Site | Stack | Result |
|---|---|---|
| 3D messenger world (Three.js, Draco, workers) | Vite + custom loaders | 0 files missing vs manual baseline, boots with 0 page errors |
| Creative portfolio (WebGL2, KTX2, Draco) | Vite + Three.js | DOM-identical to live (153/153 divs), 0 page errors |
| College portal (Elementor/WordPress) | WP + page builders | 1793/1793 divs, homepage + query-page handling verified |
| Studio site (Astro, CMS media) | Astro + headless CMS | Boots with full content, 0 page errors |

## Limitations (stated plainly)

- **Backends aren't cloned.** APIs are stubbed with captured payloads.
- **Gated content isn't reachable.** Logins, paywalls, DRM, aggressive anti-bot need a real session.
- **Live state isn't frozen.** Websockets, personalization, per-user feeds snapshot to whatever loaded.
- **Interaction-gated assets can be missed.** The trace pass scrolls and idles; it doesn't click through tabs or modals yet.
- **Upstream breakage reproduces.** A link that's 404 on live warns instead of failing; upstream JS bugs appear in the clone too.

## Layout

```
webdow/
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
