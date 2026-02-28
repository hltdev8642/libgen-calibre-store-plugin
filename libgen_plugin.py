# -*- coding: utf-8 -*-
# License: GPLv3 Copyright: 2024, poochinski9
#
# Enhanced multi-source plugin:
#   - Library Genesis  (HTML scraping, configurable mirrors)
#   - Z-Library        (official eAPI at z-lib.gl)
#   - Anna's Archive   (HTML scraping with domain failover)
#
# Cover-image robustness fix: _safe_image_url() handles all URL forms and
# guards against data-URIs / relative paths that previously produced the
# "Not a valid image (detected type: None)" error when libgen_url was
# naively prepended to an already-absolute or data: src.
from __future__ import absolute_import, division, print_function, unicode_literals

import json
import logging
import re
import sys
import time
import urllib.parse

from PyQt5.Qt import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QUrl,
    QVBoxLayout,
    QWidget,
)
from bs4 import BeautifulSoup, NavigableString, Tag
from calibre import browser, url_slash_cleaner
from calibre.gui2 import open_url
from calibre.gui2.store import StorePlugin
from calibre.gui2.store.basic_config import BasicStoreConfig
from calibre.gui2.store.search_result import SearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog
from calibre.utils.browser import Browser
from urllib.request import urlopen, Request as URLRequest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default LibGen mirrors — live status: https://open-slum.org
LIBGEN_MIRRORS_DEFAULT = [
    "https://libgen.bz",
    "https://libgen.vg",
    "https://libgen.gl",
    "https://libgen.la",
]

# A more modern UA reduces bot-detection false positives on LibGen / Z-Lib
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

ZLIBRARY_API_BASE_DEFAULT = "https://z-lib.gl/eapi"
ZLIBRARY_WEB_BASE_DEFAULT = "https://z-library.sk"

ANNAS_ARCHIVE_DOMAINS_DEFAULT = [
    "https://annas-archive.org",
    "https://annas-archive.se",
    "https://annas-archive.li",
]

# ---------------------------------------------------------------------------
# Module-level state  (initialised at the bottom of this file)
# ---------------------------------------------------------------------------

# LibGen HTML table column indices — populated by extract_indices()
title_index = None
image_index = None
author_index = None
year_index = None
pages_index = None
size_index = None
ext_index = None
mirrors_index = None

# Active LibGen mirror URL — None if all mirrors are down
libgen_url = None

# Z-Library endpoints — overridden from config by _init_source_urls()
zlibrary_api_base = ZLIBRARY_API_BASE_DEFAULT
zlibrary_web_base = ZLIBRARY_WEB_BASE_DEFAULT

# Anna's Archive domains (tried in order) — overridden from config
annas_archive_domains = list(ANNAS_ARCHIVE_DOMAINS_DEFAULT)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ===========================================================================
# Shared utilities
# ===========================================================================

def check_url(mirrors):
    """Return the first reachable mirror URL, or the first entry as a fallback."""
    for mirror in mirrors:
        try:
            if urlopen(mirror, timeout=8).code == 200:
                return mirror
        except Exception:
            continue
    return mirrors[0] if mirrors else None


# URL path suffixes Calibre's image loader will accept without raising NotImage.
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def _safe_image_url(base_url, src):
    """Build a valid, absolute image URL from any src form, or return None.

    Handles:
    * data: URIs              — None (not fetchable as a remote URL)
    * protocol-relative       — prepends https:
    * absolute HTTP(S)        — returned as-is
    * relative paths          — joined to base_url
    * non-image file paths    — None

    Extension guard: cover servers often return HTML error / rate-limit pages
    for missing or blocked images.  Those pages obviously fail Calibre's image
    signature check, producing «NotImage» errors in download_thread.py.
    Real cover files always have an image extension (.jpg, .png, …), so
    rejecting extension-less or non-image URLs is both safe and effective.
    """
    if not src:
        return None
    src = src.strip()
    if not src or src.startswith("data:"):
        return None

    if src.startswith("//"):
        url = "https:" + src
    elif src.startswith(("http://", "https://")):
        url = src
    elif base_url:
        url = base_url.rstrip("/") + "/" + src.lstrip("/")
    else:
        return None

    # Reject URLs whose path does not end with a known image extension.
    # Dynamic/proxy endpoints (e.g. /covers.php?id=…) lack extensions and
    # frequently return HTML for missing covers, causing NotImage in the log.
    path = urllib.parse.urlparse(url).path.lower()
    if not any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return None

    return url


def _text_without_scripts(tag):
    """Extract text from a BS4 tag while skipping content inside <script> tags."""
    parts = []
    for node in tag.descendants:
        if isinstance(node, NavigableString):
            # Skip text nodes whose ancestors include a <script>
            if not any(
                isinstance(parent, Tag) and parent.name == "script"
                for parent in node.parents
            ):
                parts.append(str(node))
    return "".join(parts)


# ===========================================================================
# Library Genesis
# ===========================================================================

def extract_indices(soup):
    """Detect LibGen results-table column positions from the <th> header row."""
    elements = ["Author(s)", "Year", "Pages", "Size", "Ext", "Mirrors"]
    indices = {}
    for idx, th in enumerate(soup.find_all("th")):
        for element in elements:
            if element in th.get_text():
                indices[element] = idx

    global author_index, year_index, pages_index, size_index
    global ext_index, mirrors_index, title_index, image_index

    image_index = 0
    title_index = 1
    author_index = indices.get("Author(s)")
    year_index = indices.get("Year")
    pages_index = indices.get("Pages")
    size_index = indices.get("Size")
    ext_index = indices.get("Ext")
    mirrors_index = indices.get("Mirrors")


def _transform_download_url(url):
    """Convert an /ads... URL into the matching /get.php?md5=... form."""
    m1 = re.match(r"/ads([a-fA-F0-9]+)", url)
    if m1:
        return f"/get.php?md5={m1.group(1)}"
    m2 = re.match(r"/ads\.php\?md5=([a-fA-F0-9]+)", url)
    if m2:
        return f"/get.php?md5={m2.group(1)}"
    return url


def _build_libgen_result(tr):
    """Parse one <tr> row from the LibGen search-results table."""
    tds = tr.find_all("td")
    s = SearchResult()
    s.store_name = "LibGen"

    # Title — collapse multi-line text and de-duplicate fragments
    raw_parts = [
        p.strip()
        for p in tds[title_index].get_text(separator="\n", strip=True).split("\n")
        if p.strip()
    ]
    unique_parts = []
    for part in raw_parts:
        if part not in unique_parts:
            unique_parts.append(part)
    s.title = " - ".join(unique_parts)

    s.author = tds[author_index].text.strip()

    size = tds[size_index].text.strip()
    pages = tds[pages_index].text.strip()
    year = tds[year_index].text.strip()
    info = f"{size} · {year}" if pages == "0 pages" else f"{size} · {pages} pages · {year}"
    s.price = f"LibGen · {info}"

    s.formats = tds[ext_index].text.strip().upper()

    # Detail / download page URL
    try:
        first_link = tds[mirrors_index].find("a", href=True)
        detail = first_link["href"].replace("get.php", "ads.php")
        s.detail_item = detail if detail.startswith("http") else libgen_url + detail
    except Exception:
        s.detail_item = None

    s.drm = SearchResult.DRM_UNLOCKED

    # LibGen's cover CDN frequently returns HTML 404 pages for missing covers
    # (no per-request content-type checking is possible without an extra HTTP
    # round-trip). Setting cover_url = None lets Calibre show a placeholder
    # icon instead of raising NotImage in download_thread.py.
    s.cover_url = None

    return s


def search_libgen(query, max_results=10, timeout=60):
    """Scrape Library Genesis search results."""
    if not libgen_url:
        logger.warning("No accessible LibGen mirror found; skipping LibGen search.")
        return []

    res_count = "25" if max_results <= 25 else "50" if max_results <= 50 else "100"
    encoded = urllib.parse.quote(query)
    search_url = (
        f"{libgen_url}/index.php?req={encoded}"
        "&columns[]=t&columns[]=a&columns[]=s&columns[]=y&columns[]=p&columns[]=i"
        "&objects[]=f&objects[]=e&objects[]=s&objects[]=a&objects[]=p&objects[]=w"
        "&topics[]=l&topics[]=c&topics[]=f&topics[]=a&topics[]=m&topics[]=r&topics[]=s"
        f"&res={res_count}&covers=on&gmode=on&filesuns=all"
    )

    try:
        br = browser(user_agent=USER_AGENT)
        raw = br.open(search_url, timeout=timeout).read()
    except Exception as exc:
        logger.error(f"LibGen search request failed: {exc}")
        return []

    soup = BeautifulSoup(raw, "html5lib")
    extract_indices(soup)

    results = []
    for tr in soup.select('table[class="table table-striped"] > tbody > tr'):
        try:
            result = _build_libgen_result(tr)
            if result.title and result.author:
                results.append(result)
        except Exception as exc:
            logger.error(f"LibGen result parse error: {exc}")
        if len(results) >= max_results:
            break

    return results[:max_results]


def _get_details_libgen(s, retries=3):
    """Fetch the LibGen detail/ads page and extract a direct download URL."""
    br = browser(user_agent=USER_AGENT)
    raw = None
    for _ in range(retries):
        try:
            raw = br.open(s.detail_item, timeout=30).read()
            break
        except Exception:
            logger.info(f"LibGen detail retry: {s.detail_item}")
            time.sleep(1)
    if not raw:
        return

    soup = BeautifulSoup(raw, "html5lib")
    download_a = soup.select_one("tr a")
    if download_a:
        dl_url = download_a.get("href", "")
        host = urllib.parse.urlparse(s.detail_item).hostname
        s.downloads[s.formats] = f"https://{host}/{dl_url}"


# ===========================================================================
# Z-Library
# ===========================================================================

def _zlib_api_request(url, payload=None):
    """POST (or GET) a Z-Library eAPI endpoint and return the parsed JSON."""
    if payload:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = URLRequest(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        })
    else:
        req = URLRequest(url, headers={"User-Agent": USER_AGENT})
    response = urlopen(req, timeout=30)
    return json.loads(response.read())


def search_zlibrary(query, max_results=10, timeout=60):
    """Search Z-Library via the public eAPI at z-lib.gl."""
    results = []
    page = 1
    total_pages = 1

    while page <= total_pages and len(results) < max_results:
        payload = {
            "message": query,
            "order": "popular",
        }
        if page > 1:
            payload["page"] = page

        try:
            resp = _zlib_api_request(f"{zlibrary_api_base}/book/search", payload)
            total_pages = resp.get("pagination", {}).get("total_pages", 1)

            for book in resp.get("books", []):
                s = SearchResult()
                s.store_name = "Z-Library"
                s.title = book.get("title", "")
                s.author = book.get("author", "")

                # Cover — run through _safe_image_url so extension guard applies
                # and CDN URLs without image extensions are silently dropped.
                s.cover_url = _safe_image_url(None, book.get("cover", ""))
                s.drm = SearchResult.DRM_UNLOCKED

                # Use the extension field from the search result directly.
                # The separate /formats API endpoint returns HTTP 400 for many
                # records (missing hash, auth-gated, or endpoint removed).
                extension = book.get("extension", "")
                s.formats = extension.upper() if extension else "EPUB/PDF"
                s.price = "Z-Library"

                href = book.get("href", "")
                # href is sometimes already absolute (https://z-lib.gl/book/…);
                # only prepend the web base when it is a relative path.
                if href:
                    s.detail_item = href if href.startswith("http") else zlibrary_web_base + href
                else:
                    s.detail_item = None

                if s.title:
                    results.append(s)
                if len(results) >= max_results:
                    break

            page += 1
        except Exception as exc:
            logger.error(f"Z-Library search page {page} error: {exc}")
            break

    return results[:max_results]


def _get_details_zlibrary(s):
    """Set a web-page download entry for a Z-Library result.

    Z-Library downloads require a user account, so we open the book's web
    page in the user's browser rather than attempting a direct download.
    Format information is already resolved during search from the API's
    ``extension`` field, so no secondary API call is needed here.
    """
    if not s.formats:
        s.formats = "EPUB/PDF"
    if s.detail_item:
        s.downloads[s.formats] = s.detail_item


# ===========================================================================
# Anna's Archive
# ===========================================================================

_AA_FORMAT_NAMES = frozenset(
    {"pdf", "epub", "mobi", "azw3", "djvu", "fb2",
     "cbr", "cbz", "doc", "docx", "txt", "rtf"}
)


def _parse_aa_metadata(text):
    """Parse 'FORMAT · size · language [code]' from an Anna's Archive metadata line."""
    fmt = size_str = lang = None
    for part in text.split("\u00b7"):   # middle dot separator
        part = part.strip()
        lower = part.lower()
        if lower in _AA_FORMAT_NAMES:
            fmt = part.upper()
        elif re.match(r"^[\d.]+\s*(kb|mb|gb|b)$", lower):
            size_str = part
        elif "[" in part and "]" in part:
            lang = part
    return fmt, size_str, lang


def _parse_aa_result(div, domain):
    """Parse one Anna's Archive search-result <div> into a SearchResult."""
    s = SearchResult()
    s.store_name = "Anna's Archive"
    s.drm = SearchResult.DRM_UNLOCKED

    # MD5 link is the canonical identifier and detail-page URL
    md5_link = div.find("a", href=re.compile(r"^/md5/"))
    if not md5_link:
        return None
    s.detail_item = domain + md5_link.get("href", "")

    # Title
    title_tag = div.find("a", class_=re.compile(r"js-vim-focus"))
    s.title = (title_tag or md5_link).get_text(strip=True)

    # Author — the author link contains a user-edit icon span
    author_icon = div.find("span", class_=re.compile(r"icon-\[mdi--user"))
    if author_icon:
        author_link = author_icon.find_parent("a")
        if author_link:
            s.author = author_link.get_text(strip=True)
    if not s.author:
        # Fallback: first non-title, non-md5 link text
        for lnk in div.find_all("a", href=True):
            txt = lnk.get_text(strip=True)
            if txt and txt != s.title and "/md5/" not in lnk["href"]:
                s.author = txt
                break

    # Metadata line (format · size · language)
    meta_div = div.find(
        "div",
        class_=lambda c: c and "text-gray-800" in c and "font-semibold" in c,
    )
    if meta_div:
        meta_text = _text_without_scripts(meta_div)
        fmt, sz, lang = _parse_aa_metadata(meta_text)
        if fmt:
            s.formats = fmt
        price_parts = [x for x in (sz, lang) if x]
        source_tag = "Anna's Archive"
        if price_parts:
            s.price = source_tag + " · " + " \u00b7 ".join(price_parts)
        else:
            s.price = source_tag

    # Cover image
    img = div.find("img")
    if img:
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
        )
        s.cover_url = _safe_image_url(domain, src)

    return s


def search_annas_archive(query, max_results=10, timeout=60):
    """Scrape Anna's Archive search results with domain failover."""
    encoded = urllib.parse.quote(query)
    results = []

    for domain in annas_archive_domains:
        url = f"{domain}/search?q={encoded}&page=1"
        try:
            br = Browser()
            br.set_handle_robots(False)
            br.set_user_agent(USER_AGENT)
            raw = br.open(url, timeout=timeout).read()
            soup = BeautifulSoup(raw, "html5lib")

            # Primary selector matches the known result-row class combination
            result_divs = soup.select(
                "div.flex.gap-2.pt-3.pb-3.border-b, div.flex.pt-3.pb-3.border-b"
            )

            # Fallback: collect parent divs of any /md5/ link
            if not result_divs:
                seen_ids = set()
                fallback = []
                for a in soup.find_all("a", href=re.compile(r"^/md5/")):
                    parent = a.find_parent("div")
                    if parent and id(parent) not in seen_ids:
                        seen_ids.add(id(parent))
                        fallback.append(parent)
                result_divs = fallback

            for div in result_divs:
                try:
                    s = _parse_aa_result(div, domain)
                    if s and s.title:
                        results.append(s)
                except Exception as exc:
                    logger.debug(f"Anna's Archive result parse error: {exc}")
                if len(results) >= max_results:
                    break

            if results:
                break  # results found on this domain; no need for failover

        except Exception as exc:
            logger.warning(f"Anna's Archive: {domain} unreachable, trying next domain. ({exc})")

    return results[:max_results]


def _get_details_annas_archive(s):
    """Scrape the Anna's Archive detail page for a slow-download link."""
    try:
        br = Browser()
        br.set_handle_robots(False)
        br.set_user_agent(USER_AGENT)
        raw = br.open(s.detail_item, timeout=30).read()
        soup = BeautifulSoup(raw, "html5lib")

        parsed = urllib.parse.urlparse(s.detail_item)
        base = f"{parsed.scheme}://{parsed.netloc}"

        dl_links = soup.find_all("a", href=re.compile(r"/slow_download/"))
        if dl_links:
            href = dl_links[0].get("href", "")
            full_url = href if href.startswith("http") else base + href
            s.downloads[s.formats or "Download"] = full_url
        else:
            s.downloads["Browse"] = s.detail_item
    except Exception as exc:
        logger.error(f"Anna's Archive get_details failed: {exc}")
        s.downloads["Browse"] = s.detail_item


# ===========================================================================
# Configuration widget
# ===========================================================================

class StoreLibgenConfigWidget(QWidget):
    """Settings panel exposed through the Calibre store preferences."""

    def __init__(self, config):
        QWidget.__init__(self)
        self._config = config
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # ── LibGen Mirror URLs ───────────────────────────────────────
        mirror_box = QGroupBox(
            "Library Genesis — Mirror URLs  (tried in order; first reachable one is used)"
        )
        ml = QVBoxLayout()
        self._mirror_list = QListWidget()
        for m in self._config.get("mirrors", LIBGEN_MIRRORS_DEFAULT):
            self._mirror_list.addItem(m)
        ml.addWidget(self._mirror_list)

        btn_row = QHBoxLayout()
        self._btn_add = QPushButton("Add")
        self._btn_edit = QPushButton("Edit")
        self._btn_del = QPushButton("Remove")
        self._btn_up = QPushButton("\u25b2")    # ▲
        self._btn_down = QPushButton("\u25bc")  # ▼
        for btn in (self._btn_add, self._btn_edit, self._btn_del,
                    self._btn_up, self._btn_down):
            btn_row.addWidget(btn)
        ml.addLayout(btn_row)
        mirror_box.setLayout(ml)
        root.addWidget(mirror_box)

        # ── Z-Library URLs ───────────────────────────────────────────
        zlib_box = QGroupBox("Z-Library — Endpoints")
        zl = QVBoxLayout()

        zl.addWidget(QLabel("Search API base URL:"))
        self._zlib_api = QLineEdit(
            self._config.get("zlibrary_api_base", ZLIBRARY_API_BASE_DEFAULT)
        )
        self._zlib_api.setPlaceholderText(ZLIBRARY_API_BASE_DEFAULT)
        zl.addWidget(self._zlib_api)

        zl.addWidget(QLabel("Web base URL  (used for book-page links):"))
        self._zlib_web = QLineEdit(
            self._config.get("zlibrary_web_base", ZLIBRARY_WEB_BASE_DEFAULT)
        )
        self._zlib_web.setPlaceholderText(ZLIBRARY_WEB_BASE_DEFAULT)
        zl.addWidget(self._zlib_web)

        zlib_box.setLayout(zl)
        root.addWidget(zlib_box)

        # ── Anna's Archive Domain URLs ───────────────────────────────
        aa_box = QGroupBox(
            "Anna\u2019s Archive — Domain URLs  (tried in order; first reachable one is used)"
        )
        al = QVBoxLayout()
        self._aa_list = QListWidget()
        for d in self._config.get("annas_archive_domains", ANNAS_ARCHIVE_DOMAINS_DEFAULT):
            self._aa_list.addItem(d)
        al.addWidget(self._aa_list)

        aa_btn_row = QHBoxLayout()
        self._aa_btn_add  = QPushButton("Add")
        self._aa_btn_edit = QPushButton("Edit")
        self._aa_btn_del  = QPushButton("Remove")
        self._aa_btn_up   = QPushButton("\u25b2")
        self._aa_btn_down = QPushButton("\u25bc")
        for btn in (self._aa_btn_add, self._aa_btn_edit, self._aa_btn_del,
                    self._aa_btn_up, self._aa_btn_down):
            aa_btn_row.addWidget(btn)
        al.addLayout(aa_btn_row)
        aa_box.setLayout(al)
        root.addWidget(aa_box)

        # ── Source toggles ───────────────────────────────────────────
        src_box = QGroupBox("Book Sources  (all enabled sources are queried per search)")
        sl = QVBoxLayout()
        self._chk_libgen = QCheckBox("Library Genesis  (HTML scraping)")
        self._chk_zlib = QCheckBox("Z-Library  (eAPI — download requires an account)")
        self._chk_anna = QCheckBox(
            "Anna\u2019s Archive  (HTML scraping — free slow-download links)"
        )
        self._chk_libgen.setChecked(self._config.get("libgen_enabled", True))
        self._chk_zlib.setChecked(self._config.get("zlibrary_enabled", True))
        self._chk_anna.setChecked(self._config.get("annas_archive_enabled", True))
        for chk in (self._chk_libgen, self._chk_zlib, self._chk_anna):
            sl.addWidget(chk)
        src_box.setLayout(sl)
        root.addWidget(src_box)
        root.addStretch()

        # Connect LibGen mirror buttons
        self._btn_add.clicked.connect(self._add_mirror)
        self._btn_edit.clicked.connect(self._edit_mirror)
        self._btn_del.clicked.connect(self._remove_mirror)
        self._btn_up.clicked.connect(self._move_up)
        self._btn_down.clicked.connect(self._move_down)

        # Connect Anna's Archive domain buttons
        self._aa_btn_add.clicked.connect(self._aa_add)
        self._aa_btn_edit.clicked.connect(self._aa_edit)
        self._aa_btn_del.clicked.connect(self._aa_remove)
        self._aa_btn_up.clicked.connect(self._aa_move_up)
        self._aa_btn_down.clicked.connect(self._aa_move_down)

    # ── Button slots ─────────────────────────────────────────────────

    def _add_mirror(self):
        text, ok = QInputDialog.getText(self, "Add Mirror", "Mirror URL:")
        if ok and text.strip():
            url = text.strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            self._mirror_list.addItem(url)

    def _edit_mirror(self):
        row = self._mirror_list.currentRow()
        if row < 0:
            return
        text, ok = QInputDialog.getText(
            self, "Edit Mirror", "Mirror URL:",
            text=self._mirror_list.item(row).text(),
        )
        if ok and text.strip():
            self._mirror_list.item(row).setText(text.strip())

    def _remove_mirror(self):
        row = self._mirror_list.currentRow()
        if row >= 0:
            self._mirror_list.takeItem(row)

    def _move_up(self):
        row = self._mirror_list.currentRow()
        if row > 0:
            item = self._mirror_list.takeItem(row)
            self._mirror_list.insertItem(row - 1, item)
            self._mirror_list.setCurrentRow(row - 1)

    def _move_down(self):
        row = self._mirror_list.currentRow()
        if row < self._mirror_list.count() - 1:
            item = self._mirror_list.takeItem(row)
            self._mirror_list.insertItem(row + 1, item)
            self._mirror_list.setCurrentRow(row + 1)

    # ── Anna's Archive domain list helpers ───────────────────────────

    def _aa_add(self):
        text, ok = QInputDialog.getText(self, "Add Domain", "Domain URL:")
        if ok and text.strip():
            url = text.strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            self._aa_list.addItem(url)

    def _aa_edit(self):
        row = self._aa_list.currentRow()
        if row < 0:
            return
        text, ok = QInputDialog.getText(
            self, "Edit Domain", "Domain URL:",
            text=self._aa_list.item(row).text(),
        )
        if ok and text.strip():
            self._aa_list.item(row).setText(text.strip())

    def _aa_remove(self):
        row = self._aa_list.currentRow()
        if row >= 0:
            self._aa_list.takeItem(row)

    def _aa_move_up(self):
        row = self._aa_list.currentRow()
        if row > 0:
            item = self._aa_list.takeItem(row)
            self._aa_list.insertItem(row - 1, item)
            self._aa_list.setCurrentRow(row - 1)

    def _aa_move_down(self):
        row = self._aa_list.currentRow()
        if row < self._aa_list.count() - 1:
            item = self._aa_list.takeItem(row)
            self._aa_list.insertItem(row + 1, item)
            self._aa_list.setCurrentRow(row + 1)

    # ── Persist settings ─────────────────────────────────────────────

    def commit(self):
        """Save all settings and refresh module-level URLs immediately."""
        mirrors = [
            self._mirror_list.item(i).text()
            for i in range(self._mirror_list.count())
        ] or list(LIBGEN_MIRRORS_DEFAULT)

        aa_domains = [
            self._aa_list.item(i).text()
            for i in range(self._aa_list.count())
        ] or list(ANNAS_ARCHIVE_DOMAINS_DEFAULT)

        api_base = self._zlib_api.text().strip() or ZLIBRARY_API_BASE_DEFAULT
        web_base = self._zlib_web.text().strip() or ZLIBRARY_WEB_BASE_DEFAULT

        self._config["mirrors"] = mirrors
        self._config["zlibrary_api_base"] = api_base
        self._config["zlibrary_web_base"] = web_base
        self._config["annas_archive_domains"] = aa_domains
        self._config["libgen_enabled"] = self._chk_libgen.isChecked()
        self._config["zlibrary_enabled"] = self._chk_zlib.isChecked()
        self._config["annas_archive_enabled"] = self._chk_anna.isChecked()

        # Apply changes to module state immediately so in-flight searches see them
        global libgen_url, zlibrary_api_base, zlibrary_web_base, annas_archive_domains
        libgen_url = check_url(mirrors)
        zlibrary_api_base = api_base
        zlibrary_web_base = web_base
        annas_archive_domains = aa_domains
        logger.info(f"LibGen active mirror updated \u2192 {libgen_url}")
        return True


# ===========================================================================
# Calibre store plugin
# ===========================================================================

class LibgenStorePlugin(BasicStoreConfig, StorePlugin):

    def open(self, parent=None, detail_item=None, external=False):
        url = libgen_url or zlibrary_web_base
        target = detail_item if detail_item else url
        if external or self.config.get("open_external", False):
            open_url(QUrl(url_slash_cleaner(target)))
        else:
            d = WebStoreDialog(self.gui, url, parent, detail_item)
            d.setWindowTitle(self.name)
            d.set_tags(self.config.get("tags", ""))
            d.exec_()

    def config_widget(self):
        self._cfg_widget = StoreLibgenConfigWidget(self.config)
        return self._cfg_widget

    def save_settings(self, config_widget):
        config_widget.commit()

    @staticmethod
    def get_details(search_result, retries=3):
        """Dispatch to the appropriate per-source detail handler."""
        s = search_result
        item = s.detail_item or ""
        if "z-library" in item or "z-lib.gl" in item:
            _get_details_zlibrary(s)
        elif "annas-archive" in item:
            _get_details_annas_archive(s)
        else:
            _get_details_libgen(s, retries)

    @staticmethod
    def search(query, max_results=10, timeout=60):
        """Aggregate results from all enabled book sources."""
        # Read per-source toggles from the persisted plugin config
        from calibre.utils.config import JSONConfig
        cfg = JSONConfig("store/search/Library Genesis")

        all_results = []

        if cfg.get("libgen_enabled", True):
            try:
                all_results.extend(
                    search_libgen(query, max_results=max_results, timeout=timeout)
                )
            except Exception as exc:
                logger.error(f"LibGen search error: {exc}")

        if cfg.get("zlibrary_enabled", True):
            try:
                all_results.extend(
                    search_zlibrary(query, max_results=max_results, timeout=timeout)
                )
            except Exception as exc:
                logger.error(f"Z-Library search error: {exc}")

        if cfg.get("annas_archive_enabled", True):
            try:
                all_results.extend(
                    search_annas_archive(query, max_results=max_results, timeout=timeout)
                )
            except Exception as exc:
                logger.error(f"Anna's Archive search error: {exc}")

        for result in all_results[:max_results]:
            yield result


# ===========================================================================
# Module initialisation
# ===========================================================================

def _init_source_urls():
    """Load all source URLs from saved config (or defaults) at import time."""
    global libgen_url, zlibrary_api_base, zlibrary_web_base, annas_archive_domains
    try:
        from calibre.utils.config import JSONConfig
        cfg = JSONConfig("store/search/Library Genesis")
        mirrors     = cfg.get("mirrors",               LIBGEN_MIRRORS_DEFAULT)
        api_base    = cfg.get("zlibrary_api_base",     ZLIBRARY_API_BASE_DEFAULT)
        web_base    = cfg.get("zlibrary_web_base",     ZLIBRARY_WEB_BASE_DEFAULT)
        aa_domains  = cfg.get("annas_archive_domains", ANNAS_ARCHIVE_DOMAINS_DEFAULT)
    except Exception:
        mirrors    = LIBGEN_MIRRORS_DEFAULT
        api_base   = ZLIBRARY_API_BASE_DEFAULT
        web_base   = ZLIBRARY_WEB_BASE_DEFAULT
        aa_domains = list(ANNAS_ARCHIVE_DOMAINS_DEFAULT)
    libgen_url          = check_url(mirrors)
    zlibrary_api_base   = api_base
    zlibrary_web_base   = web_base
    annas_archive_domains = list(aa_domains)


_init_source_urls()


if __name__ == "__main__":
    query_string = " ".join(sys.argv[1:])
    for result in search_libgen(query_string):
        print(result)

