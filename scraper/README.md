# scraper

One-shot script that snapshots `https://www.thrivingtogetherpt.com` into a
self-contained static site that can be served by GitHub Pages.

## Why a script (instead of `wget`)?

The Wix site is JavaScript-rendered (Wix Thunderbolt). A plain `wget --mirror`
or `curl` only gets back a JS bootstrap shell — none of the actual content.
This script uses Playwright to load each page in real headless Chromium, lets
the page hydrate, then captures the rendered DOM. It also rewrites every
asset URL to a local file, so the snapshot has zero runtime dependency on Wix.

## One-time setup

```bash
cd scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

Smoke test (just the homepage):

```bash
python scrape.py --max-pages 1 --output ../site-test
```

Full scrape into the repo root (where GitHub Pages will serve from):

```bash
python scrape.py --output ..
```

## What it does

1. Loads each page in headless Chromium and waits for `networkidle`.
2. Scrolls the page to trigger Wix's lazy-loaded images.
3. Saves the post-hydration HTML.
4. Crawls by following internal `<a href>` links it finds.
5. Downloads every referenced image / stylesheet / font into `assets/`,
   keyed by a hash of the source URL.
6. Recursively rewrites `url(...)` refs inside CSS so fonts/images resolve.
7. Strips `<script>` tags, Wix preloads, and `<form>` tags from the HTML.
8. Replaces forms with a visible orange placeholder so you can spot where
   to add the new mailto: + WhatsApp links by hand.

## Re-running

The script is safe to re-run; downloaded assets are cached on disk so a
second run is much faster. Delete the output directory to start fresh.

## Known limitations

- Anything that depends on Wix's runtime (member login, checkout, embedded
  Wix forms) will not work after the snapshot. The form-stripping step makes
  these obvious.
- Third-party embeds (Calendly, YouTube, etc.) are left as-is and continue
  to load from their original hosts.
- Wix serves multiple resolutions of the same image via `srcset`. Each
  resolution gets downloaded separately — there is no deduplication.
