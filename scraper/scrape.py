#!/usr/bin/env python3
"""
Scrape thrivingtogetherpt.com (a Wix Thunderbolt site) into a fully self-
contained static snapshot suitable for hosting on GitHub Pages.

Why a headless browser?
    Wix renders content client-side (Wix Thunderbolt + React). A plain HTTP
    fetch only returns a JS bootstrap shell. We need to load each page in a
    real browser, let it hydrate, then save the post-render DOM.

What it does:
    1. Renders each page with headless Chromium and grabs the rendered HTML.
    2. Crawls by following same-origin <a href> links found on rendered pages.
    3. Downloads every referenced asset (img src/srcset, link href, font url(),
       CSS url(), inline style url(...)) into ./assets/, hashed by source URL.
    4. Recursively rewrites url() refs inside downloaded CSS so fonts/images
       resolve locally.
    5. Strips <script> tags, Wix preloads/preconnects, and <form> tags so the
       saved pages don't try to re-bootstrap Wix or POST to a dead backend.
    6. Rewrites every URL in the HTML to point at the local copies, so the
       resulting tree has zero runtime dependency on Wix.

Usage:
    cd scraper
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium
    python scrape.py --output ..             # writes site files to repo root
    python scrape.py --output ../site --max-pages 2   # smoke test
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from collections import deque
from os.path import relpath
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, unquote

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

START_URL = "https://www.thrivingtogetherpt.com/"

# Hosts treated as "this site" for crawling purposes.
SITE_HOSTS = {"www.thrivingtogetherpt.com", "thrivingtogetherpt.com"}

# Hosts whose assets we want to download and self-host. Anything from a host
# not in this set is left as an absolute URL (e.g. third-party embeds).
ASSET_HOSTS = SITE_HOSTS | {
    "static.wixstatic.com",
    "static.parastorage.com",
}

# Path suffixes we recognise as downloadable static assets. URLs whose path
# does not end in one of these are left alone — this avoids treating a
# <link rel="canonical"> back to the homepage, or an iframe src like
# .../googleMap.html, as a static asset to mirror.
ASSET_EXTS = {
    ".css", ".js", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif",
    ".ico", ".pdf", ".mp4", ".webm", ".mp3", ".json",
}

# url(...) inside CSS or inline style attributes.
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


def parse_srcset(srcset: str) -> list[tuple[str, str]]:
    """Parse an HTML srcset attribute into [(url, descriptor), ...] pairs.

    A naive ``srcset.split(",")`` is wrong because Wix's image URLs contain
    commas in the path (e.g. ``/v1/fill/w_640,h_480,al_c/img.jpg``). Instead
    we tokenize on whitespace — URLs and descriptors never contain whitespace,
    and the entry separator is ", " or ",\\n" etc.; the trailing comma always
    appears on the descriptor token (or, if there is no descriptor, on the
    URL token), never embedded in a Wix transform URL.
    """
    entries: list[tuple[str, str]] = []
    cur_url: str | None = None
    cur_desc: list[str] = []
    for tok in srcset.split():
        ends_entry = tok.endswith(",")
        if ends_entry:
            tok = tok[:-1]
        if tok:
            if cur_url is None:
                cur_url = tok
            else:
                cur_desc.append(tok)
        if ends_entry and cur_url is not None:
            entries.append((cur_url, " ".join(cur_desc)))
            cur_url = None
            cur_desc = []
    if cur_url is not None:
        entries.append((cur_url, " ".join(cur_desc)))
    return entries

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

logger = logging.getLogger("scrape")


# ---------------------------------------------------------------------------
# URL -> local path mapping
# ---------------------------------------------------------------------------

def normalize_page_url(url: str) -> str:
    """Canonicalize a same-site page URL so dedup works.

    Strips fragments, trailing-slashes from non-root paths, query strings,
    and forces the host to the canonical www form.
    """
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    netloc = parsed.netloc
    if netloc == "thrivingtogetherpt.com":
        netloc = "www.thrivingtogetherpt.com"
    return f"{parsed.scheme}://{netloc}{path}"


def page_local_path(url: str, output_dir: Path) -> Path:
    """Map a page URL to a local .html file under output_dir.

    /            -> output_dir/index.html
    /about       -> output_dir/about.html
    /a/b/c       -> output_dir/a/b/c.html
    """
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return output_dir / "index.html"
    if path.endswith(".html"):
        return output_dir / path
    return output_dir / f"{path}.html"


def asset_local_path(url: str, output_dir: Path) -> Path:
    """Deterministic local path for a remote asset.

    Wix asset URLs are gnarly (e.g. /v1/fill/w_640,h_480,al_c,.../filename.jpg)
    so we hash the canonical URL and bucket by file extension.
    """
    parsed = urlparse(url)
    path = unquote(parsed.path)
    ext = Path(path).suffix.lower()
    # Strip query strings out of the extension if any leaked in
    if "?" in ext:
        ext = ext.split("?", 1)[0]
    if not ext or len(ext) > 6:
        ext = ""
    h = hashlib.sha1(f"{parsed.scheme}://{parsed.netloc}{path}".encode()).hexdigest()[:16]

    if ext == ".css":
        sub = "assets/css"
    elif ext in {".woff", ".woff2", ".ttf", ".otf", ".eot"}:
        sub = "assets/fonts"
    elif ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".ico"}:
        sub = "assets/img"
    else:
        sub = "assets/misc"
    return output_dir / sub / f"{h}{ext}"


# ---------------------------------------------------------------------------
# Asset downloader (handles caching + recursive CSS processing)
# ---------------------------------------------------------------------------

class AssetDownloader:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )
        # canonical URL -> local path relative to output_dir (POSIX style)
        self.cache: dict[str, str] = {}
        # URLs currently being processed, to break CSS @import loops
        self.in_progress: set[str] = set()

    def close(self) -> None:
        self.client.close()

    def download(self, url: str, referer: str | None = None) -> str | None:
        """Download one asset. Returns its path relative to output_dir,
        or None if the asset is ineligible / failed."""
        url, _ = urldefrag(url)
        if url in self.cache:
            return self.cache[url]
        if url in self.in_progress:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if parsed.netloc not in ASSET_HOSTS:
            return None
        # Only mirror URLs that look like static assets. This filters out
        # canonical links pointing at HTML pages, iframe srcs, and other
        # non-asset URLs that happen to live on an asset host.
        ext = Path(parsed.path).suffix.lower()
        if ext not in ASSET_EXTS:
            return None

        self.in_progress.add(url)
        try:
            local = asset_local_path(url, self.output_dir)
            if not local.exists():
                logger.info("  asset: %s", url)
                try:
                    headers = {"Referer": referer} if referer else {}
                    r = self.client.get(url, headers=headers)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("    failed: %s", e)
                    return None
                content_type = r.headers.get("content-type", "").lower()
                # If the URL had no .css suffix but the server says CSS,
                # relocate the file into assets/css/ with a .css extension so
                # browsers (and our re-runs) handle it correctly.
                is_css = local.suffix == ".css" or "text/css" in content_type
                if is_css and local.suffix != ".css":
                    h = local.stem
                    local = self.output_dir / "assets" / "css" / f"{h}.css"
                local.parent.mkdir(parents=True, exist_ok=True)
                if is_css:
                    local.write_text(self._rewrite_css(r.text, base_url=url),
                                     encoding="utf-8")
                else:
                    local.write_bytes(r.content)
            rel = local.relative_to(self.output_dir).as_posix()
            self.cache[url] = rel
            return rel
        finally:
            self.in_progress.discard(url)

    def _rewrite_css(self, css: str, base_url: str) -> str:
        """Find every url() in a CSS file, download referenced assets, and
        rewrite the url() refs to be relative to the CSS file's location."""
        css_local = asset_local_path(base_url, self.output_dir)
        # Collect (start, end, replacement) so we can splice without
        # accidentally re-matching identical url() strings.
        replacements: list[tuple[int, int, str]] = []
        for m in CSS_URL_RE.finditer(css):
            ref = m.group(1).strip()
            if ref.startswith("data:"):
                continue
            absolute = urljoin(base_url, ref)
            local_rel = self.download(absolute, referer=base_url)
            if not local_rel:
                continue
            new_ref = relpath(self.output_dir / local_rel, css_local.parent)
            new_ref = new_ref.replace("\\", "/")
            replacements.append((m.start(), m.end(), f"url('{new_ref}')"))
        for start, end, new in reversed(replacements):
            css = css[:start] + new + css[end:]
        return css


# ---------------------------------------------------------------------------
# HTML processing: clean + rewrite a single page's rendered HTML
# ---------------------------------------------------------------------------

def process_page_html(
    html: str,
    page_url: str,
    output_dir: Path,
    downloader: AssetDownloader,
    discovered_pages: set[str],
) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Strip ALL <script> tags. The page is already rendered; we don't want
    #    Wix's bootstrap to run again on load (it would try to re-fetch state
    #    from Wix backends, fail, and likely overwrite our DOM with errors).
    for tag in soup.find_all("script"):
        tag.decompose()

    # 2. Strip preload/prefetch/preconnect link tags — they're optimisations
    #    for the Wix bundle that no longer exists.
    for tag in list(soup.find_all("link")):
        rel = tag.get("rel") or []
        rel_str = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
        if any(x in rel_str for x in ("preload", "modulepreload", "prefetch",
                                       "dns-prefetch", "preconnect")):
            tag.decompose()

    # 3. Replace <form> tags with a visible placeholder. The user will hand-
    #    edit these to drop in mailto:/WhatsApp links after the scrape.
    for tag in soup.find_all("form"):
        placeholder = soup.new_tag("div")
        placeholder["class"] = "tt-form-placeholder"
        placeholder["style"] = (
            "padding:1.5em;border:2px dashed #c47b00;background:#fffbe6;"
            "margin:1em 0;border-radius:8px;font-family:sans-serif;"
        )
        placeholder.string = (
            "[CONTACT FORM REMOVED — replace with mailto: link + WhatsApp group link]"
        )
        tag.replace_with(placeholder)

    # 4. Drop <base> so relative URL resolution stays predictable.
    for tag in soup.find_all("base"):
        tag.decompose()

    # 4b. Strip Wix-runtime UI widgets that depend on services we are about
    #     to cancel. Currently:
    #       - the floating "Wix Chat" bubble pinned to the bottom-right
    #       - Google Map embeds (Wix renders these via a parastorage iframe
    #         that won't work without Wix's runtime; replace with a static
    #         link by hand if you need a map)
    for chat_iframe in soup.find_all("iframe"):
        src = chat_iframe.get("src") or ""
        if "wixapps.net" in src or "engage.wix" in src:
            chat_iframe.decompose()
    for map_div in soup.find_all("div", class_="wixui-google-map"):
        map_div.decompose()

    page_path = page_local_path(page_url, output_dir)
    page_dir = page_path.parent

    # 5. Process inline <style> blocks. Wix ships most of its CSS this way,
    #    and the rules contain url(...) refs to fonts and background images
    #    on parastorage / wixstatic. Without this step those assets stay
    #    pointed at Wix's CDN.
    for style_tag in soup.find_all("style"):
        # Drop the data-url / data-href attrs Wix uses to remember which
        # source stylesheet got inlined — they're metadata and just leave
        # parastorage URLs visible in `view source`.
        for attr in ("data-url", "data-href"):
            if attr in style_tag.attrs:
                del style_tag[attr]
        css_text = style_tag.string
        if not css_text or "url(" not in css_text:
            continue
        style_tag.string = _rewrite_inline_style(
            css_text, page_url, output_dir, page_dir, downloader
        )

    # 6. Walk every element with a URL-bearing attribute, downloading assets
    #    and rewriting links.

    def to_rel_from_page(local_rel: str) -> str:
        return relpath(output_dir / local_rel, page_dir).replace("\\", "/")

    for el in soup.find_all(True):
        for attr in ("src", "href", "data-src", "xlink:href", "poster"):
            if attr not in el.attrs:
                continue
            val = el[attr]
            if not isinstance(val, str) or not val.strip():
                continue
            if val.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
                continue

            absolute = urljoin(page_url, val)
            parsed = urlparse(absolute)

            # Same-host page link from an <a> tag: queue and rewrite to local.
            if el.name == "a" and parsed.netloc in SITE_HOSTS:
                # Skip non-html-ish URLs masquerading as page links
                no_frag = absolute.split("#")[0]
                if any(no_frag.lower().endswith(ext) for ext in
                       (".jpg", ".jpeg", ".png", ".pdf", ".webp", ".svg")):
                    local_rel = downloader.download(absolute, referer=page_url)
                    if local_rel:
                        el[attr] = to_rel_from_page(local_rel)
                    continue
                canonical = normalize_page_url(no_frag)
                discovered_pages.add(canonical)
                target = page_local_path(canonical, output_dir)
                el[attr] = relpath(target, page_dir).replace("\\", "/")
                # Preserve fragment if any
                if "#" in absolute:
                    el[attr] += "#" + absolute.split("#", 1)[1]
                continue

            # Asset (img, stylesheet, font, etc.): download and rewrite.
            local_rel = downloader.download(absolute, referer=page_url)
            if local_rel:
                el[attr] = to_rel_from_page(local_rel)

        # srcset (img, source) — comma-separated list of "url descriptor" pairs.
        # Wix URLs contain commas in the path, so use parse_srcset rather than
        # a naive split(',').
        if "srcset" in el.attrs:
            new_parts: list[str] = []
            for u, descriptor in parse_srcset(el["srcset"]):
                if u.startswith("data:"):
                    new_parts.append(f"{u} {descriptor}".strip())
                    continue
                absolute = urljoin(page_url, u)
                local_rel = downloader.download(absolute, referer=page_url)
                if local_rel:
                    new_u = to_rel_from_page(local_rel)
                else:
                    new_u = u  # leave external URL untouched
                new_parts.append(f"{new_u} {descriptor}".strip())
            el["srcset"] = ", ".join(new_parts)

        # inline style="background-image: url(...)" etc.
        if "style" in el.attrs and "url(" in el["style"]:
            el["style"] = _rewrite_inline_style(
                el["style"], page_url, output_dir, page_dir, downloader
            )

    return str(soup)


def _rewrite_inline_style(
    style: str,
    page_url: str,
    output_dir: Path,
    page_dir: Path,
    downloader: AssetDownloader,
) -> str:
    matches = list(CSS_URL_RE.finditer(style))
    for m in reversed(matches):
        ref = m.group(1).strip()
        if ref.startswith("data:"):
            continue
        absolute = urljoin(page_url, ref)
        local_rel = downloader.download(absolute, referer=page_url)
        if not local_rel:
            continue
        new_ref = relpath(output_dir / local_rel, page_dir).replace("\\", "/")
        style = style[:m.start()] + f"url('{new_ref}')" + style[m.end():]
    return style


# ---------------------------------------------------------------------------
# Crawl loop
# ---------------------------------------------------------------------------

# JS that scrolls the page to the bottom in chunks, triggering Wix's lazy
# image/section loading, then scrolls back to the top.
SCROLL_JS = """
() => new Promise(resolve => {
    let total = 0;
    const dist = 400;
    const timer = setInterval(() => {
        window.scrollBy(0, dist);
        total += dist;
        if (total >= document.body.scrollHeight) {
            clearInterval(timer);
            window.scrollTo(0, 0);
            setTimeout(resolve, 500);
        }
    }, 200);
});
"""


def scrape(output_dir: Path, start_urls: list[str], max_pages: int | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloader = AssetDownloader(output_dir)

    visited: set[str] = set()
    queue: deque[str] = deque(start_urls)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
            )

            while queue:
                url = queue.popleft()
                url, _ = urldefrag(url)
                url = normalize_page_url(url)
                if url in visited:
                    continue
                if max_pages is not None and len(visited) >= max_pages:
                    break
                visited.add(url)

                logger.info("page %d: %s", len(visited), url)
                page = context.new_page()
                try:
                    # First wait for DOM ready (fast). Then opportunistically
                    # wait a bit longer for network idle, but don't fail the
                    # page if it never quiets down — Wix occasionally keeps
                    # long-poll connections open which never reach idle.
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        logger.debug("  networkidle never reached, continuing")
                    try:
                        page.evaluate(SCROLL_JS)
                    except Exception as e:
                        logger.debug("  scroll failed: %s", e)
                    page.wait_for_timeout(1000)
                    html = page.content()
                except Exception as e:
                    logger.error("  failed to load: %s", e)
                    page.close()
                    continue
                page.close()

                discovered: set[str] = set()
                processed = process_page_html(
                    html, url, output_dir, downloader, discovered
                )

                local_path = page_local_path(url, output_dir)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(processed, encoding="utf-8")
                logger.info("  saved -> %s", local_path.relative_to(output_dir))

                for link in sorted(discovered):
                    if link not in visited:
                        queue.append(link)

            browser.close()
    finally:
        downloader.close()

    logger.info(
        "done. %d pages, %d assets, output at %s",
        len(visited), len(downloader.cache), output_dir,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot thrivingtogetherpt.com into a static site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", type=Path, default=Path("..").resolve(),
        help="Output directory (default: parent of scraper/, i.e. repo root)",
    )
    parser.add_argument("--start", default=START_URL, help="Start URL")
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Stop after N pages (useful for smoke-testing)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    scrape(args.output.resolve(), [args.start], max_pages=args.max_pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
