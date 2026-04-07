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

    # Wix serves a different DOM to mobile vs desktop, so the responsive
    # snapshot is built by running the scraper twice — once per variant.
    # The desktop run produces canonical names (index.html, contact-1.html);
    # the mobile run produces -m suffixed names (index-m.html, ...). A tiny
    # redirect shim injected into <head> picks between them at page load.
    python scrape.py --output .. --device desktop
    python scrape.py --output .. --device mobile

    python scrape.py --output ../site --max-pages 1   # smoke test
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

# Used by the asset downloader regardless of device mode. Assets are the
# same URL-wise on both desktop and mobile, and a desktop UA tends to get
# us the highest-resolution variant of responsive Wix images.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Page-rendering device profiles. Wix Thunderbolt serves a completely
# different DOM depending on UA (different element structure, different
# widths, different viewport meta), so we can't produce a responsive
# snapshot from one render — we scrape twice and ship both variants.
#
# `suffix` is appended to each page's local filename: the desktop scrape
# produces index.html / contact-1.html / ..., while the mobile scrape
# produces index-m.html / contact-1-m.html / .... A tiny redirect shim
# (REDIRECT_SHIM_JS below) picks between them at page-load time.
DEVICES: dict[str, dict] = {
    "desktop": {
        "user_agent": USER_AGENT,
        "viewport": {"width": 1440, "height": 900},
        "is_mobile": False,
        "has_touch": False,
        "suffix": "",
    },
    "mobile": {
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "suffix": "-m",
    },
}

# Site-specific: the Wix contact form gets stripped (GitHub Pages can't
# process POSTs) and replaced with this email + WhatsApp card. Baked in
# so re-running the scraper doesn't clobber it.
CONTACT_REPLACEMENT_HTML = (
    '<div class="tt-contact-card" '
    'style="padding:1.5em 1.25em;font-family:inherit;line-height:1.6;">'
    '<p style="margin:0 0 1em 0;font-size:18px;">'
    '<strong>Email:</strong> '
    '<a href="mailto:thrivingtogetherpt@gmail.com" '
    'style="color:inherit;text-decoration:underline;">'
    'thrivingtogetherpt@gmail.com</a></p>'
    '<p style="margin:0;font-size:18px;">'
    '<strong>WhatsApp group:</strong> '
    '<a href="https://chat.whatsapp.com/EeToU3U6PQw3PQbweD6Vas" '
    'target="_blank" rel="noopener noreferrer" '
    'style="color:inherit;text-decoration:underline;">join here</a></p>'
    '</div>'
)

# The mobile nav is Wix's hamburger pattern: a toggle <div id="MENU_AS_
# CONTAINER_TOGGLE"> that should open the drawer <div id="MENU_AS_CONTAINER">.
# Wix's base CSS hides the drawer with `opacity:0;visibility:hidden` and
# Wix's JS normally adds a class that flips it to visible. We stripped that
# JS, so the hamburger appears dead. This CSS + JS pair revives it using a
# new class name (`tt-menu-open`) so we don't depend on Wix's minified
# class (which could change on re-scrape if Wix rebuilds).
HAMBURGER_REVIVE_CSS = (
    # Reveal rules. All three !important overrides are necessary:
    #   - `display:block` beats `.EmyVop[data-undisplayed=true]{display:none}`
    #     (we never clear Wix's data-undisplayed attribute, so that rule
    #     would otherwise keep the drawer out of the layout entirely).
    #   - `opacity:1` / `visibility:visible` beat `.EmyVop{opacity:0;visibility:hidden}`.
    "#MENU_AS_CONTAINER.tt-menu-open{"
    "display:block !important;"
    "opacity:1 !important;"
    "visibility:visible !important;"
    "}"
    # The open drawer covers the hamburger button, so we inject a close (×)
    # button into the drawer itself. It's hidden by default and revealed
    # whenever the drawer is open. Absolute positioning inside the fixed-
    # position drawer puts it in the top-right corner of the visible panel.
    "#tt-menu-close{display:none;}"
    "#MENU_AS_CONTAINER.tt-menu-open #tt-menu-close{"
    "display:flex;"
    "align-items:center;"
    "justify-content:center;"
    "position:absolute;"
    "top:14px;"
    "right:14px;"
    "width:44px;"
    "height:44px;"
    "background:transparent;"
    "border:none;"
    "color:#fff;"
    "font-size:32px;"
    "line-height:1;"
    "cursor:pointer;"
    "z-index:1;"
    "font-family:-apple-system,system-ui,sans-serif;"
    "}"
)
HAMBURGER_REVIVE_JS = r"""
document.addEventListener('DOMContentLoaded', function() {
  var btn = document.getElementById('MENU_AS_CONTAINER_TOGGLE');
  var menu = document.getElementById('MENU_AS_CONTAINER');
  if (!btn || !menu) return;
  function close() {
    menu.classList.remove('tt-menu-open');
    btn.setAttribute('aria-expanded', 'false');
  }
  btn.addEventListener('click', function(e) {
    e.preventDefault();
    e.stopPropagation();
    var open = menu.classList.toggle('tt-menu-open');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  var closeBtn = document.getElementById('tt-menu-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
      close();
    });
  }
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') close();
  });
  menu.addEventListener('click', function(e) {
    if (e.target.closest('a')) close();
  });
});
""".strip()

# Inline script injected as the very first child of <head> on every page.
# Picks between desktop and mobile variants based on UA/viewport, using
# location.replace so there's no back-button trap. Must run before the
# body renders to avoid a flash of the wrong layout.
#
# Override with ?desktop or ?mobile in the query string if you need to
# inspect a specific variant from any device.
REDIRECT_SHIM_JS = r"""
(function(){
  var ua = navigator.userAgent;
  var isMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(ua) || window.innerWidth < 768;
  var p = location.pathname;
  var isMobilePath = /-m\.html?$/.test(p);
  var qs = location.search || '';
  if (qs.indexOf('desktop') !== -1) return;
  if (qs.indexOf('mobile') !== -1) return;
  if (isMobile && !isMobilePath) {
    var target;
    if (p === '/' || p === '' || /\/index\.html?$/.test(p)) {
      target = '/index-m.html';
    } else if (/\.html?$/.test(p)) {
      target = p.replace(/\.html?$/, '-m.html');
    } else {
      target = p + '-m.html';
    }
    location.replace(target + location.search + location.hash);
  } else if (!isMobile && isMobilePath) {
    location.replace(p.replace(/-m\.html?$/, '.html') + location.search + location.hash);
  }
})();
""".strip()

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


def page_local_path(url: str, output_dir: Path, suffix: str = "") -> Path:
    """Map a page URL to a local .html file under output_dir.

    The device suffix ("", "-m", etc.) is inserted before .html so that a
    single URL like /contact-1 maps to contact-1.html on the desktop scrape
    and contact-1-m.html on the mobile scrape.

    /            -> output_dir/index{suffix}.html
    /about       -> output_dir/about{suffix}.html
    /a/b/c       -> output_dir/a/b/c{suffix}.html
    """
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return output_dir / f"index{suffix}.html"
    if path.endswith(".html"):
        return output_dir / (path[:-5] + f"{suffix}.html")
    return output_dir / f"{path}{suffix}.html"


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
    suffix: str = "",
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

    # 3. Strip <form> tags and replace with the baked-in email + WhatsApp
    #    card. (GitHub Pages can't POST, so the form has to go; the card
    #    content is the same on every page that had a form, which is fine
    #    for this site because only /contact-1 does.)
    for tag in soup.find_all("form"):
        replacement = BeautifulSoup(CONTACT_REPLACEMENT_HTML, "html.parser")
        tag.replace_with(replacement)

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

    # 4c. Mobile-only: inject a close (×) button into the mobile nav drawer.
    #     The drawer is fullscreen when open, which covers the hamburger
    #     toggle and leaves no way to close it. This button is hidden until
    #     the drawer opens (see HAMBURGER_REVIVE_CSS) and is wired up in
    #     HAMBURGER_REVIVE_JS.
    menu_container = soup.find(id="MENU_AS_CONTAINER")
    if menu_container is not None and not menu_container.find(id="tt-menu-close"):
        close_btn = soup.new_tag("button", type="button")
        close_btn["id"] = "tt-menu-close"
        close_btn["aria-label"] = "Close navigation menu"
        close_btn.string = "×"
        menu_container.insert(0, close_btn)

    page_path = page_local_path(page_url, output_dir, suffix)
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
                # Internal links point at the same-device variant of the
                # target page (desktop -> desktop, mobile -> mobile) so
                # navigation between pages doesn't bounce through the
                # redirect shim.
                target = page_local_path(canonical, output_dir, suffix)
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

    # 7. Inject the hand-written scripts we're putting back into <head>:
    #
    #    a) The redirect shim (very first, so it runs before anything else
    #       and doesn't render a flash of the wrong layout).
    #    b) The hamburger-menu CSS + JS pair. The JS is wrapped in a
    #       DOMContentLoaded handler so it's safe to live in <head>. These
    #       are no-ops on pages without the menu elements (e.g. the desktop
    #       variant, which uses an inline nav bar instead of a hamburger).
    head = soup.find("head")
    if head is not None:
        shim = soup.new_tag("script")
        shim.string = REDIRECT_SHIM_JS
        head.insert(0, shim)

        ham_css = soup.new_tag("style")
        ham_css.string = HAMBURGER_REVIVE_CSS
        head.append(ham_css)

        ham_js = soup.new_tag("script")
        ham_js.string = HAMBURGER_REVIVE_JS
        head.append(ham_js)

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


def scrape(
    output_dir: Path,
    start_urls: list[str],
    device_name: str = "desktop",
    max_pages: int | None = None,
) -> None:
    if device_name not in DEVICES:
        raise ValueError(f"unknown device {device_name!r}; expected one of {sorted(DEVICES)}")
    device = DEVICES[device_name]
    suffix = device["suffix"]

    output_dir.mkdir(parents=True, exist_ok=True)
    downloader = AssetDownloader(output_dir)

    visited: set[str] = set()
    queue: deque[str] = deque(start_urls)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                user_agent=device["user_agent"],
                viewport=device["viewport"],
                is_mobile=device["is_mobile"],
                has_touch=device["has_touch"],
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

                logger.info("page %d [%s]: %s", len(visited), device_name, url)
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
                    html, url, output_dir, downloader, discovered, suffix=suffix
                )

                local_path = page_local_path(url, output_dir, suffix)
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
        "done [%s]. %d pages, %d assets, output at %s",
        device_name, len(visited), len(downloader.cache), output_dir,
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
        "--device", choices=sorted(DEVICES), default="desktop",
        help="Which device profile to render with. Run once with --device "
             "desktop and once with --device mobile to produce both variants.",
    )
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
    scrape(
        args.output.resolve(),
        [args.start],
        device_name=args.device,
        max_pages=args.max_pages,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
