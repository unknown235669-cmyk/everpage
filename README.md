# siteclone

Paste a link → get a working 1:1 offline clone. Built for JS-heavy sites
where `wget`/HTTrack only capture a shell: hashed bundles, runtime-built
asset URLs, web workers, 3D/geometry payloads, fonts, media — downloaded,
rewritten to local paths, served, and headless-verified.

Proven on production Three.js/WebGL apps and WordPress/Elementor,
Astro and Vite/React single-page sites.

## Install

```bash
pip install -r requirements.txt
playwright install chromium   # needed for the trace + verify stages
```

Requires Python 3.10+.

## Use

Interactive menu:

```bash
python -m siteclone.tui
```

Direct CLI:

```bash
python -m siteclone.cli https://example.com/ -o ./example-clone --port 8919
```

Options: `--pages about,pricing` (extra routes), `--max-pages 200`
(auto-crawl cap, sitemap + links), `--no-crawl`, `--no-trace`
(skip the headless runtime pass; faster but misses runtime-only assets).

Each clone ships with `OPEN-ME.bat` (or run
`python serve_<name>.py <port>`) for instant local preview.

## How it works

1. **fetch** — homepage + manifest, real-browser headers, gzip handled.
2. **detect** — framework fingerprint (Next/Vite/Astro/WordPress/Three…).
3. **crawl** — sitemap + same-origin links as first-class pages
   (extensionless *and* `.html`), recursion included.
4. **trace** — one headless pass records URLs the app resolves at runtime.
5. **assets/chunks** — bundle mining: hashed chunks, `setPath` loader
   roots, backtick templates (incl. multi-var `terrain/index` shapes),
   ID pools, ternary alternatives, LOD-gap interpolation, absolute
   same-origin worker URLs.
6. **rewrite** — root-absolute → depth-relative paths; absolute
   same-origin URLs folded to local in HTML/CSS/JS/JSON (workers are
   strictly same-origin and break otherwise); query-page URLs
   (`?p=`, `?page=`) self-name instead of clobbering `index.html`.
7. **serve** — offline preview server with API/beacon stubs.
8. **verify** — headless reload: JS errors, failed requests, screenshots.
   Same-host requests for files already on disk don't count as failures
   (headless aborts streaming downloads; the bytes are what matter).

`python smoke_test.py` runs the offline self-checks (no network).

## Honest limitations

- **No backend cloning.** Server logic, databases, auth sessions,
  payments stay server-side; `/api/*` and tracker calls get stubbed.
- **No login walls, paywalls, DRM, or aggressive anti-bot.** Only what a
  public fetch + one headless pass can reach.
- **No interaction crawling yet.** Content behind clicks/tabs/logins is
  missed; the trace pass is passive (scroll + idle), not scripted play.
- **Live-only breakage reproduces.** Dead-upstream links (404 on live)
  warn instead of failing; upstream JS bugs appear in the clone too.
- Big 3D sites take minutes and thousands of requests; re-probes are
  cheap only because nothing is cached between runs yet.

## Layout

```
siteclone/
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

## License

MIT — see [LICENSE](LICENSE).
