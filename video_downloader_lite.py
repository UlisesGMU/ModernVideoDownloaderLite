# =========================================================
# MODERN VIDEO DOWNLOADER LITE (WINDOWS)
# PySide6 (Essentials only) + yt-dlp + FFmpeg
#
# Lighter version of video_downloader.py: signs in through the PC's
# own Edge/Chrome instead of a built-in QtWebEngine browser, so the
# packaged .exe doesn't carry a whole Chromium. Keeps its own data in
# app_data_lite, separate from the original app.
# =========================================================

# INSTALL:
#
# pip install PySide6-Essentials  (NOT plain "pyside6", which also
#                                   pulls in the large Addons package)
# pip install websockets          (talks to Edge/Chrome for Login & Capture)
# pip install yt-dlp
# pip install gallery-dl          (dedicated image/gallery downloader --
#                                   used as a fallback for Instagram/TikTok
#                                   photo posts yt-dlp can't handle)
# pip install requests
# pip install pyqtdarktheme       (provides "import qdarktheme")
#
# FFmpeg:
# https://www.gyan.dev/ffmpeg/builds/
#
# =========================================================

import sys
import os
import re
import json
import html
import shutil
import subprocess
import tempfile
import random
import webbrowser
import time
import base64
import hashlib
import io
import requests
import yt_dlp
import urllib.parse

from collections import defaultdict

from datetime import datetime
from queue import Queue, Empty
from threading import Thread, Event, Lock, RLock
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import (
    Qt,
    Signal,
    QObject,
    QTimer
)

from PySide6.QtGui import (
    QAction,
    QColor,
    QBrush,
    QTextCursor
)

from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFileDialog,
    QTextEdit,
    QLineEdit,
    QCheckBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QProgressBar,
    QMessageBox,
    QSystemTrayIcon,
    QMenu,
    QDialog,
    QInputDialog,
    QAbstractItemView,
    QPlainTextEdit,
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QStyle
)

try:
    import qdarktheme
except Exception:
    qdarktheme = None

# 'websockets' is only needed for Login & Capture (it talks to the
# sign-in browser), so the rest of the app still runs without it and
# that feature just reports a clear error instead.
try:
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed as WsConnectionClosed
    LOGIN_BROWSER_AVAILABLE = True
except ImportError:
    ws_connect = None
    WsConnectionClosed = OSError
    LOGIN_BROWSER_AVAILABLE = False


# =========================================================
# CONFIG
# =========================================================

DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.expanduser("~"),
    "Downloads",
    "Videos"
)

os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)

# Packaged (.exe): next to the .exe. From source: next to this script.
# (__file__ in a one-file build points into a temporary folder that's
# deleted when the app closes.)
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Separate from the original app's "app_data", so both can sit in the
# same folder without touching each other's settings or cookies.
APP_DIR = os.path.join(
    SCRIPT_DIR,
    "app_data_lite"
)

COOKIES_DIR = os.path.join(APP_DIR, "cookies")

BROWSER_PROFILES_DIR = os.path.join(APP_DIR, "browser_profiles")

SITES_FILE = os.path.join(COOKIES_DIR, "sites.json")

CONFIG_FILE = os.path.join(APP_DIR, "config.json")

LOG_FILE = os.path.join(APP_DIR, "download_log.jsonl")

FAILED_FILE = os.path.join(APP_DIR, "failed_downloads.txt")

os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(BROWSER_PROFILES_DIR, exist_ok=True)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"}

# Guards the log/failed-list files against concurrent writes from the
# worker threads, plus the in-memory dedupe sets below.
_log_lock = Lock()

_queue_dedupe_lock = Lock()

# In-memory set of already-downloaded URLs (normalized), loaded from
# LOG_FILE at startup and updated as downloads complete, so the same
# video is never queued/downloaded twice.
COMPLETED_URLS = set()

# "extractor:id" keys for completed downloads -- more reliable than
# URL-string matching, since short links / tracking query strings mean
# the same video can arrive under different-looking URLs.
COMPLETED_IDS = set()

# normalized_url -> the full record dict from its "Completed" log
# entry, so pasting an already-downloaded link can show it in the
# list as a Completed row (with Redownload available) instead of just
# a log line you could easily miss.
COMPLETED_RECORDS = {}

# Normalized URLs currently sitting in the queue or being downloaded
# right now, so pasting/importing the same URL twice in one go doesn't
# race both copies through before either finishes.
QUEUED_URLS = set()

# Sites shown by default in Manage Cookies / Site Settings, and used to
# collapse their subdomains (m.facebook.com -> facebook.com). This is
# NOT a whitelist anymore: any URL yt-dlp (or gallery-dl) can handle is
# accepted -- see is_supported_url() below.
SUPPORTED_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "x.com"
]

# Common link shorteners/share links that should be followed to their
# real destination before dedupe and download.
SHORT_LINK_HOSTS = (
    "vm.tiktok.com", "vt.tiktok.com", "fb.watch",
    "facebook.com/share/", "t.co/", "bit.ly/", "tinyurl.com/",
    "pin.it/", "redd.it/", "on.soundcloud.com/", "b23.tv/",
)

# Link wrappers that carry the real destination in a query parameter:
# (host, path prefix, parameter). Unwrapped locally -- no network
# request needed.
REDIRECT_WRAPPERS = [
    ("l.facebook.com", "/l.php", "u"),
    ("lm.facebook.com", "/l.php", "u"),
    ("l.messenger.com", "/l.php", "u"),
    ("l.instagram.com", "/", "u"),
    ("google.com", "/url", "q"),
    ("google.com", "/url", "url"),
    ("youtube.com", "/redirect", "q"),
    ("out.reddit.com", "/", "url"),
    ("t.umblr.com", "/redirect", "z"),
    ("href.li", "/", None),          # href.li/?https://real.url
]

# "Embed fixer" / mirror / mobile domains that show the same post as
# the real site. Rewritten to the real domain so yt-dlp's extractor
# for that site handles it and duplicates are recognized.
HOST_REWRITES = {
    "twitter.com": "x.com",
    "mobile.twitter.com": "x.com",
    "mobile.x.com": "x.com",
    "fxtwitter.com": "x.com",
    "vxtwitter.com": "x.com",
    "fixupx.com": "x.com",
    "fixvx.com": "x.com",
    "twittpr.com": "x.com",
    "nitter.net": "x.com",
    "ddinstagram.com": "www.instagram.com",
    "kkinstagram.com": "www.instagram.com",
    "instagramez.com": "www.instagram.com",
    "m.instagram.com": "www.instagram.com",
    "vxtiktok.com": "www.tiktok.com",
    "tnktok.com": "www.tiktok.com",
    "m.tiktok.com": "www.tiktok.com",
    "m.youtube.com": "www.youtube.com",
    "music.youtube.com": "www.youtube.com",
    "youtube-nocookie.com": "www.youtube.com",
    "m.facebook.com": "www.facebook.com",
    "mbasic.facebook.com": "www.facebook.com",
    "touch.facebook.com": "www.facebook.com",
    "web.facebook.com": "www.facebook.com",
    "old.reddit.com": "www.reddit.com",
    "new.reddit.com": "www.reddit.com",
    "np.reddit.com": "www.reddit.com",
    "m.reddit.com": "www.reddit.com",
}

# Share links that live on the main domain but still redirect to the
# real post (so they need resolving like a shortener).
SHORT_LINK_PATTERNS = [
    re.compile(r"reddit\.com/r/[^/]+/s/", re.IGNORECASE),
    re.compile(r"tiktok\.com/t/", re.IGNORECASE),
    re.compile(r"instagram\.com/share/", re.IGNORECASE),
]


def is_short_link(url):

    return (
        any(x in url for x in SHORT_LINK_HOSTS)
        or any(p.search(url) for p in SHORT_LINK_PATTERNS)
    )


def unwrap_redirect(url):
    """Pulls the real destination out of redirect wrappers like
    l.facebook.com/l.php?u=... or google.com/url?q=... (up to a few
    levels deep, since wrappers are sometimes nested)."""

    for _ in range(3):

        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return url

        host = parsed.netloc.lower()

        if host.startswith("www."):
            host = host[4:]

        target = None

        for w_host, w_path, param in REDIRECT_WRAPPERS:

            if host != w_host or not parsed.path.startswith(w_path):
                continue

            if param is None:
                target = urllib.parse.unquote(parsed.query)
            else:
                values = urllib.parse.parse_qs(parsed.query).get(param)
                target = values[0] if values else None

            break

        if not target or not target.startswith(("http://", "https://")):
            return url

        url = target

    return url


def canonicalize_url(url):
    """Rewrites alternate forms of the same post to the one yt-dlp and
    the duplicate check expect:
      - mirror / embed-fixer / mobile domains -> the real domain
      - youtu.be/ID, YouTube /live/ID, /embed/ID, /v/ID -> /watch?v=ID
      - Instagram /<user>/p/ID and /reels/ID -> /p/ID and /reel/ID
      - Google AMP links -> the original page"""

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url

    host = parsed.netloc.lower()

    bare = host[4:] if host.startswith("www.") else host

    path = parsed.path

    # Google AMP cache: google.com/amp/s/example.com/page
    amp = re.match(r"^/amp/s/(.+)$", path)

    if bare == "google.com" and amp:
        return canonicalize_url("https://" + amp.group(1))

    new_host = HOST_REWRITES.get(bare) or HOST_REWRITES.get(host)

    if new_host:
        host = new_host
        bare = host[4:] if host.startswith("www.") else host

    # youtu.be/ID carries the video ID itself -- no need to follow
    # the redirect over the network.
    if bare == "youtu.be":

        video_id = path.strip("/").split("/")[0]

        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    if bare == "youtube.com":

        m = re.match(r"^/(?:live|embed|v)/([A-Za-z0-9_-]{6,})", path)

        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"

    if bare == "instagram.com":

        # Newer share format puts the username first:
        # instagram.com/<user>/p/<code> or /<user>/reel/<code>
        m = re.match(r"^/[^/]+/(p|reel|reels|tv)/([^/]+)", path)

        if m and path.split("/")[1] not in ("p", "reel", "reels", "tv"):
            path = f"/{m.group(1)}/{m.group(2)}/"

        path = re.sub(r"^/reels/", "/reel/", path)

    return urllib.parse.urlunparse(parsed._replace(netloc=host, path=path))


# Query parameters that only track where a link was shared from. These
# are stripped; every OTHER query parameter is kept, because for many
# sites the query IS the video identity (facebook.com/watch/?v=...,
# youtube.com/watch?v=..., bilibili ?p=2, etc.).
TRACKING_PARAM_PREFIXES = ("utm_",)

TRACKING_PARAMS = {
    "fbclid", "gclid", "igsh", "igshid", "si", "feature", "pp",
    "ref", "ref_src", "ref_url", "share_id", "share_app_id",
    "is_from_webapp", "sender_device", "sender_web_id", "_t", "_r",
    "mibextid", "rdid", "s", "spm_id_from", "vd_source",
}

# Substrings that suggest a download failed because the site wants a
# logged-in session or wants a human to clear a verification step.
AUTH_ERROR_HINTS = [
    "login",
    "sign in",
    "log in",
    "private",
    "rate-limit",
    "429",
    "checkpoint",
    "confirm your identity",
    "verify",
    "captcha",
    "not authorized",
]

# A row's status text starting with any of these means it's done --
# not actively downloading anymore -- used by the "follow the active
# download" autoscroll to know when to move on to the next one, and
# by "Clear List"/context-menu actions to know what's safe to treat
# as finished.
TERMINAL_STATUS_PREFIXES = (
    "Completed",
    "Failed",
    "Skipped",
    "Opened in Browser",
)


# Error text containing any of these is a network/SSL problem, not a
# login wall, even if it also contains an AUTH_ERROR_HINTS word (e.g.
# "certificate verify failed" contains "verify").
NON_AUTH_ERROR_HINTS = [
    "certificate verify failed",
    "ssl:",
    "sslerror",
    "timed out",
]


def looks_like_auth_error(err_text):

    lower = err_text.lower()

    if any(hint in lower for hint in NON_AUTH_ERROR_HINTS):
        return False

    return any(hint in lower for hint in AUTH_ERROR_HINTS)


def is_terminal_status(text):

    return any(text.startswith(prefix) for prefix in TERMINAL_STATUS_PREFIXES)



APP_VERSION = "1.0.0"

DOWNLOAD_LOG_SEPARATOR = "\u2500" * 70

# Used to highlight text matching the current search in both the
# table filter and the log search -- a warm amber that stays legible
# against both light and dark themes.
SEARCH_HIGHLIGHT_COLOR = "#ffd54f"


def classify_log_line(msg):
    """Picks a text color for a log line based on its content, so
    failures/successes/warnings are visually distinct at a glance.
    Returns None for ordinary lines, which just keep the log box's
    normal (theme-appropriate) text color."""

    lower = msg.lower()

    if any(k in lower for k in ("failed", "error", "could not", "couldn't")):
        return "#e06c75"  # red

    if any(k in lower for k in ("completed", "downloaded via")):
        return "#98c379"  # green

    if any(k in lower for k in ("skipped", "warning", "note:")):
        return "#e5c07b"  # amber

    if any(k in lower for k in ("opened in browser", "waiting", "debug:")):
        return "#61afef"  # blue

    return None


# =========================================================
# SITE / COOKIE MAP HELPERS
# =========================================================

def domain_from_url(url):
    """Return a bare registrable-ish domain, e.g. 'instagram.com'."""

    netloc = urllib.parse.urlparse(url).netloc.lower()

    netloc = netloc.split("@")[-1]  # strip any userinfo
    netloc = netloc.split(":")[0]   # strip port

    if netloc.startswith("www."):
        netloc = netloc[4:]

    # Collapse known subdomains (m.facebook.com, vm.tiktok.com, etc.)
    # down to the domain used in SUPPORTED_DOMAINS / cookie map.
    for known in SUPPORTED_DOMAINS:
        if netloc == known or netloc.endswith("." + known):
            return known

    # Any other site: treat mobile subdomains as the main site, so
    # cookies / site settings saved for "example.com" also apply to
    # links from "m.example.com".
    for prefix in ("m.", "mobile."):
        if netloc.startswith(prefix) and netloc.count(".") >= 2:
            return netloc[len(prefix):]

    return netloc


_EXTRACTOR_CLASSES = None


def has_dedicated_extractor(url):
    """True if yt-dlp has a site-specific extractor for this URL
    (i.e. anything other than its catch-all 'Generic' extractor).
    Covers the ~1800 sites yt-dlp supports, and stays current as
    yt-dlp is updated, instead of a hand-maintained domain list."""

    global _EXTRACTOR_CLASSES

    if _EXTRACTOR_CLASSES is None:
        _EXTRACTOR_CLASSES = [
            ie for ie in yt_dlp.extractor.gen_extractor_classes()
            if ie.ie_key() != "Generic"
        ]

    for ie in _EXTRACTOR_CLASSES:

        try:
            if ie.suitable(url):
                return True
        except Exception:
            continue

    return False


def is_supported_url(url, strict=False):
    """Whether a URL should be queued.

    strict=False (paste box / Import from File): any http(s) URL is
    accepted -- you typed or chose it deliberately, and yt-dlp's
    generic extractor can often find a video even on sites it has no
    dedicated support for.

    strict=True (clipboard Auto Add): only URLs that yt-dlp has a
    dedicated extractor for, or sites you've set up cookies / site
    settings for -- otherwise every link you copy anywhere would get
    queued."""

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    if not strict:
        return True

    # Shorteners (t.co, bit.ly, ...) have no yt-dlp extractor of their
    # own; they're resolved and checked properly after queueing.
    if is_short_link(url):
        return True

    # Judge the real destination, not a wrapper or mirror of it.
    url = canonicalize_url(unwrap_redirect(url))

    if is_short_link(url):
        return True

    domain = domain_from_url(url)

    known = set(SUPPORTED_DOMAINS)
    known |= set(load_site_cookie_map().keys())
    known |= set(load_config().get("site_settings", {}).keys())

    if domain in known:
        return True

    return has_dedicated_extractor(url)


def load_site_cookie_map():

    if not os.path.exists(SITES_FILE):
        return {}

    try:
        with open(SITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_site_cookie_map(mapping):

    with open(SITES_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


# =========================================================
# COOKIE STORAGE (optional encryption)
# =========================================================
#
# Three protection modes, chosen in Manage Cookies:
#
#   plain    -- <domain>.txt, readable by anyone who can open the file
#               (the original behavior).
#   windows  -- encrypted with Windows DPAPI, the same system Windows
#               uses for saved Wi-Fi and Credential Manager passwords.
#               Only your Windows account on this PC can decrypt them;
#               a copied, synced, or backed-up file is useless elsewhere.
#               No password to type.
#   password -- DPAPI plus a master password (stretched with scrypt).
#               The files stay locked until you type the password each
#               time the app starts, even to your own account.
#
# Neither protected mode stops malware already running as you while the
# cookies are unlocked in memory -- nothing on a normal PC can.

IS_WINDOWS = sys.platform.startswith("win")

COOKIE_MODES = {
    "plain": "Plain text (not protected)",
    "windows": "Protected by your Windows account",
    "password": "Windows account + master password",
}

ENCRYPTED_MAGIC = b"VDCOOKIE1\n"

_WINDOWS_ENTROPY = b"ModernVideoDownloader.cookies.v1"

_VAULT_CHECK_TEXT = b"ModernVideoDownloader.vault-check.v1"

# Guards every read-modify-write of cookie files and sites.json, which
# worker threads (saving refreshed cookies) and the GUI both do.
_cookie_io_lock = RLock()


class CookieVaultLocked(Exception):
    """Password mode, and the password hasn't been entered yet."""


class CookieVaultError(Exception):
    """Encryption/decryption failed (wrong account, corrupt file...)."""


def _dpapi(data, entropy, protect):
    """CryptProtectData / CryptUnprotectData via ctypes (no extra
    packages). 'entropy' is an extra secret mixed into the encryption:
    decryption fails unless the exact same bytes are supplied."""

    if not IS_WINDOWS:
        raise CookieVaultError("Cookie protection is only available on Windows.")

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def to_blob(raw):
        buf = ctypes.create_string_buffer(raw, len(raw))
        blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        return blob, buf  # keep buf alive as long as blob is used

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob, _in_buf = to_blob(data)

    ent_blob, _ent_buf = to_blob(entropy)

    out_blob = DATA_BLOB()

    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    ok = func(
        ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )

    if not ok:
        action = "encrypt" if protect else "decrypt"
        raise CookieVaultError(
            f"Windows couldn't {action} the cookies "
            f"(error {ctypes.get_last_error()})."
        )

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _derive_password_key(password, salt):
    """scrypt makes each password guess deliberately slow and
    memory-hungry (~0.1-0.3 s, 32 MB), so brute-forcing a copied file
    is impractical for any reasonable password."""

    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32
    )


class CookieVault:

    def __init__(self):

        # Only set in password mode, after a successful unlock. Never
        # written to disk.
        self._password_key = None

    def mode(self):

        mode = load_config().get("cookie_security", "plain")

        return mode if mode in COOKIE_MODES else "plain"

    def is_protected(self):

        return self.mode() != "plain"

    def is_locked(self):

        return self.mode() == "password" and self._password_key is None

    def unlock(self, password):

        cfg = load_config()

        try:
            salt = bytes.fromhex(cfg["cookie_vault_salt"])
            check = base64.b64decode(cfg["cookie_vault_check"])
        except (KeyError, ValueError):
            raise CookieVaultError("The master password settings are missing or damaged.")

        key = _derive_password_key(password, salt)

        try:
            ok = _dpapi(check, key, protect=False) == _VAULT_CHECK_TEXT
        except CookieVaultError:
            ok = False

        if ok:
            self._password_key = key

        return ok

    def lock(self):

        self._password_key = None

    def _entropy(self, mode=None):

        mode = mode or self.mode()

        if mode == "windows":
            return _WINDOWS_ENTROPY

        if mode == "password":

            if self._password_key is None:
                raise CookieVaultLocked()

            return _WINDOWS_ENTROPY + self._password_key

        raise CookieVaultError("Cookies aren't set to be protected.")

    def encrypt(self, text):

        return ENCRYPTED_MAGIC + _dpapi(text.encode("utf-8"), self._entropy(), True)

    def decrypt(self, blob):

        if not blob.startswith(ENCRYPTED_MAGIC):
            raise CookieVaultError("Not an encrypted cookie file.")

        raw = _dpapi(blob[len(ENCRYPTED_MAGIC):], self._entropy(), False)

        return raw.decode("utf-8", errors="replace")


cookie_vault = CookieVault()


def is_managed_cookie_path(path):
    """True for files inside our own cookies folder -- the only ones
    this app ever deletes or rewrites on its own. A cookies.txt you
    picked with Browse... elsewhere is never deleted without asking."""

    return (
        bool(path)
        and os.path.normcase(os.path.dirname(os.path.abspath(path)))
        == os.path.normcase(os.path.abspath(COOKIES_DIR))
    )


def is_encrypted_cookie_file(path):

    try:
        with open(path, "rb") as f:
            return f.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
    except OSError:
        return False


def read_site_cookies(domain):
    """The saved cookies for a site as Netscape-format text, or None.
    Raises CookieVaultLocked if they're protected by a master password
    that hasn't been entered yet."""

    with _cookie_io_lock:

        path = load_site_cookie_map().get(domain)

        if not path or not os.path.exists(path):
            return None

        with open(path, "rb") as f:
            data = f.read()

    if data.startswith(ENCRYPTED_MAGIC):
        return cookie_vault.decrypt(data)

    return data.decode("utf-8", errors="replace")


def _write_atomic(path, data):

    tmp = path + ".tmp"

    with open(tmp, "wb") as f:
        f.write(data)

    os.replace(tmp, path)


def save_site_cookies(domain, text):
    """Stores a site's cookies using the current protection mode and
    updates sites.json. Returns the file path."""

    with _cookie_io_lock:

        site_map = load_site_cookie_map()

        old_path = site_map.get(domain) or None

        if cookie_vault.is_protected():

            path = os.path.join(COOKIES_DIR, f"{domain}.cookies.bin")

            _write_atomic(path, cookie_vault.encrypt(text))

        elif (
            old_path and os.path.exists(old_path)
            and not is_encrypted_cookie_file(old_path)
        ):

            # Plain mode: keep writing to the file you chose, even if
            # it lives outside the cookies folder.
            path = old_path

            _write_atomic(path, text.encode("utf-8"))

        else:

            path = os.path.join(COOKIES_DIR, f"{domain}.txt")

            _write_atomic(path, text.encode("utf-8"))

        site_map[domain] = path

        save_site_cookie_map(site_map)

        if (
            old_path and os.path.normcase(os.path.abspath(old_path))
            != os.path.normcase(os.path.abspath(path))
            and is_managed_cookie_path(old_path)
        ):
            try:
                os.remove(old_path)
            except OSError:
                pass

        return path


def delete_site_cookies(domain):
    """Forgets a site's cookies. Deletes the file only if it's one of
    ours (inside the cookies folder)."""

    with _cookie_io_lock:

        site_map = load_site_cookie_map()

        path = site_map.pop(domain, None)

        save_site_cookie_map(site_map)

        set_site_user_agent(domain, None)

        if path and is_managed_cookie_path(path):
            try:
                os.remove(path)
            except OSError:
                pass


def change_cookie_protection(new_mode, new_password=None):
    """Switches protection mode (or changes the master password) and
    re-saves every site's cookies in the new form. Everything is read
    into memory BEFORE the mode changes, so a failure while reading
    leaves the old setup untouched. Returns the list of plain-text
    cookie files outside the cookies folder that were imported (the
    caller offers to delete those originals)."""

    if new_mode != "plain" and not IS_WINDOWS:
        raise CookieVaultError("Cookie protection is only available on Windows.")

    with _cookie_io_lock:

        site_map = load_site_cookie_map()

        texts = {}

        for domain, path in site_map.items():

            if path and os.path.exists(path):
                texts[domain] = read_site_cookies(domain)

        cfg = load_config()

        if new_mode == "password":

            salt = os.urandom(16)

            key = _derive_password_key(new_password, salt)

            cfg["cookie_vault_salt"] = salt.hex()

            cfg["cookie_vault_check"] = base64.b64encode(
                _dpapi(_VAULT_CHECK_TEXT, key, True)
            ).decode("ascii")

            cookie_vault._password_key = key

        else:

            cfg.pop("cookie_vault_salt", None)

            cfg.pop("cookie_vault_check", None)

            cookie_vault._password_key = None

        cfg["cookie_security"] = new_mode

        save_config(cfg)

        external_plaintext = []

        for domain, text in texts.items():

            old_path = site_map[domain]

            if (
                new_mode != "plain"
                and not is_managed_cookie_path(old_path)
                and not is_encrypted_cookie_file(old_path)
            ):
                external_plaintext.append(old_path)

            save_site_cookies(domain, text)

        return external_plaintext


def reset_protected_cookies():
    """'Forgot password': deletes the encrypted cookie files (they
    can't be opened without it) and falls back to Windows-account
    protection. You then sign in to those sites again."""

    with _cookie_io_lock:

        site_map = load_site_cookie_map()

        for domain, path in list(site_map.items()):

            if path and is_encrypted_cookie_file(path):
                delete_site_cookies(domain)

        cfg = load_config()

        cfg.pop("cookie_vault_salt", None)

        cfg.pop("cookie_vault_check", None)

        cfg["cookie_security"] = "windows" if IS_WINDOWS else "plain"

        save_config(cfg)

        cookie_vault.lock()


def parse_netscape_cookies(text):
    """Yields one dict per cookie line of a Netscape cookies.txt."""

    for line in (text or "").splitlines():

        http_only = False

        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
            http_only = True
        elif not line.strip() or line.startswith("#"):
            continue

        parts = line.rstrip("\r\n").split("\t")

        if len(parts) != 7:
            continue

        domain, _flag, path, secure, expires, name, value = parts

        try:
            expires = int(float(expires))
        except ValueError:
            expires = 0

        yield {
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "expires": expires,
            "name": name,
            "value": value,
            "http_only": http_only,
        }


def load_config():

    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg):

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# One download at a time PER SITE by default -- different sites still
# download in parallel with each other. Several simultaneous requests to
# the same site is what tends to trigger rate limits / account checks;
# raise it per site in Site Settings where you know that's fine.
def get_site_user_agent(domain):
    """The browser identity the saved cookies for this site were
    created with (set when you sign in with Login & Capture)."""

    return load_config().get("site_user_agents", {}).get(domain)


def set_site_user_agent(domain, user_agent):

    cfg = load_config()

    agents = cfg.setdefault("site_user_agents", {})

    if user_agent:
        agents[domain] = user_agent
    else:
        agents.pop(domain, None)

    save_config(cfg)


DEFAULT_SITE_SETTINGS = {
    "max_concurrent": 1,
    "delay_seconds": 0,
    "random_delay": False,
    # "start": the delay separates when downloads START.
    # "finish": it counts from when the previous download for that
    #           site FINISHED (a real pause between downloads).
    "delay_from": "start"
}

DELAY_FROM_LABELS = {"start": "Start", "finish": "Finish"}


def get_site_settings(domain):
    """Per-site concurrency/delay settings, falling back to the
    '_default' entry, then to DEFAULT_SITE_SETTINGS."""

    cfg = load_config()

    all_sites = cfg.get("site_settings", {})

    default = {**DEFAULT_SITE_SETTINGS, **all_sites.get("_default", {})}

    return {**default, **all_sites.get(domain, {})}


def save_site_settings(
    domain, max_concurrent, delay_seconds, random_delay=False,
    delay_from="start"
):

    cfg = load_config()

    all_sites = cfg.setdefault("site_settings", {})

    all_sites[domain] = {
        "max_concurrent": max_concurrent,
        "delay_seconds": delay_seconds,
        "random_delay": random_delay,
        "delay_from": delay_from
    }

    save_config(cfg)


def remove_site_settings(domain):
    """Deletes a site's custom override so it falls back to '_default'
    (or, for a custom domain the user added themselves, removes it
    from the list entirely)."""

    cfg = load_config()

    all_sites = cfg.setdefault("site_settings", {})

    all_sites.pop(domain, None)

    save_config(cfg)


def format_size(num_bytes):

    if not num_bytes or num_bytes <= 0:
        return "?"

    size = float(num_bytes)

    for unit in ("B", "KB", "MB", "GB", "TB"):

        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"

        size /= 1024

    return f"{size:.1f} TB"


def reveal_in_file_manager(path):
    """Best-effort, cross-platform 'show this file in the system file
    manager'. Windows can select the exact file; macOS can too; Linux
    has no universal equivalent, so it just opens the containing
    folder with whatever the desktop environment registers for that."""

    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Path does not exist: {path}")

    folder = path if os.path.isdir(path) else os.path.dirname(path)

    if sys.platform.startswith("win"):

        if os.path.isfile(path):
            subprocess.run(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(folder)

    elif sys.platform == "darwin":

        if os.path.isfile(path):
            subprocess.run(["open", "-R", path])
        else:
            subprocess.run(["open", path])

    else:

        subprocess.run(["xdg-open", folder])


def open_file_with_default_app(path):

    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Path does not exist: {path}")

    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


def find_ffmpeg():
    """Look for ffmpeg: a manually-configured path first, then PATH."""

    cfg = load_config()

    custom = cfg.get("ffmpeg_path")

    if custom and os.path.exists(custom):
        return custom

    return shutil.which("ffmpeg")


_GALLERY_DL_AVAILABLE = None


def check_gallery_dl_available(refresh=False):
    """gallery-dl is a pip package installed into this same Python
    environment, so invoking it as 'python -m gallery_dl' is more
    reliable than hunting for a console-script on PATH (which is a
    common source of 'command not found' on Windows). The result is
    cached -- this used to start a new Python process on every
    download that needed the image fallback."""

    global _GALLERY_DL_AVAILABLE

    if _GALLERY_DL_AVAILABLE is not None and not refresh:
        return _GALLERY_DL_AVAILABLE

    _GALLERY_DL_AVAILABLE = _probe_gallery_dl()

    return _GALLERY_DL_AVAILABLE


GALLERY_DL_FLAG = "--run-gallery-dl"

# Keeps gallery-dl's process from flashing a console window on Windows
# when this app itself runs without one (pythonw / a --noconsole exe).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gallery_dl_command():
    """How to start gallery-dl as a separate process.

    Running from source, sys.executable is python.exe, so
    'python -m gallery_dl' works. In a PyInstaller build,
    sys.executable is this app's own .exe, which can't take '-m' --
    so the exe re-launches ITSELF with GALLERY_DL_FLAG, and the
    __main__ block hands those runs straight to the bundled gallery-dl
    instead of opening the GUI."""

    if getattr(sys, "frozen", False):
        return [sys.executable, GALLERY_DL_FLAG]

    return [sys.executable, "-m", "gallery_dl"]


def run_bundled_gallery_dl(args):
    """Body of a '<exe> --run-gallery-dl ...' child process."""

    # A --noconsole build may start with no stdout/stderr at all;
    # gallery-dl writes to both, so give it somewhere to write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    try:
        import gallery_dl
    except ImportError:
        print("gallery-dl is not bundled in this build.", file=sys.stderr)
        return 1

    sys.argv = ["gallery-dl"] + list(args)

    return gallery_dl.main()


def _probe_gallery_dl():

    try:

        result = subprocess.run(
            gallery_dl_command() + ["--version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_WINDOW
        )

        return result.returncode == 0

    except (OSError, subprocess.TimeoutExpired):
        return False


def run_gallery_dl(url, dest_dir, cookie_text, user_agent=None):
    """Run gallery-dl against a single post URL into an ISOLATED temp
    folder (so there's zero ambiguity about what it downloaded, even
    when dest_dir already has lots of files in it), then move the
    result -- folder structure and all -- into dest_dir. This keeps
    gallery-dl's own folder naming (no extra wrapper folder on our
    end) without relying on fragile filesystem-timestamp heuristics.
    Raises RuntimeError on failure. Returns the list of final file
    paths inside dest_dir."""

    os.makedirs(dest_dir, exist_ok=True)

    # gallery-dl only reads cookies from a file, so the (decrypted)
    # cookies go into a separate private temp folder for the duration
    # of this one run and are deleted as soon as it finishes.
    with tempfile.TemporaryDirectory(prefix="vd_ck_") as cookie_dir, \
            tempfile.TemporaryDirectory(prefix="gallery_dl_") as tmp_dir:

        cookie_file = None

        if cookie_text:

            cookie_file = os.path.join(cookie_dir, "cookies.txt")

            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_text)

        cmd = gallery_dl_command() + [
            "-d", tmp_dir,
            "-q"
        ]

        if cookie_file and os.path.exists(cookie_file):
            cmd += ["--cookies", cookie_file]

        if user_agent:
            cmd += ["--user-agent", user_agent]

        cmd.append(url)

        try:

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=_NO_WINDOW
            )

        except subprocess.TimeoutExpired:

            raise RuntimeError("gallery-dl timed out on this URL.")

        tmp_files = []

        for root, _dirs, files in os.walk(tmp_dir):

            for f in files:
                tmp_files.append(os.path.join(root, f))

        if not tmp_files:

            message = (result.stderr or result.stdout or "").strip()

            raise RuntimeError(
                f"gallery-dl found nothing to download "
                f"(exit {result.returncode}): {message[:300]}"
            )

        moved_files = []

        for full_path in tmp_files:

            rel_path = os.path.relpath(full_path, tmp_dir)

            final_path = os.path.join(dest_dir, rel_path)

            os.makedirs(os.path.dirname(final_path), exist_ok=True)

            # Avoid clobbering an existing file with the same name.
            if os.path.exists(final_path):

                base, ext = os.path.splitext(final_path)

                counter = 1

                while os.path.exists(f"{base}_{counter}{ext}"):
                    counter += 1

                final_path = f"{base}_{counter}{ext}"

            shutil.move(full_path, final_path)

            moved_files.append(final_path)

        return moved_files


def format_speed(bytes_per_second):

    value = float(bytes_per_second or 0)

    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):

        if value < 1024 or unit == "GB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"

        value /= 1024


def parse_speed_bytes(text):
    """Back from '3.2 MB/s' to bytes/s, so the Speed column sorts by
    real speed (NA / ? sort as 0)."""

    match = re.match(r"([\d.]+)\s*([KMG]?)B/s", text or "")

    if not match:
        return 0

    return int(float(match.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[match.group(2)])


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(text, max_length=80, fallback="post"):
    """Makes text safe as a Windows file or folder name:
      - Unicode normalized (NFC), so the same title always produces
        the same bytes on disk
      - line breaks, tabs and other control characters removed (TikTok
        and Instagram captions often contain line breaks, which are
        invalid in Windows file names)
      - \\ / : * ? " < > | replaced with _
      - repeated spaces collapsed, trailing dots/spaces removed (Windows
        silently drops them, which breaks later lookups)
      - reserved device names (CON, NUL, COM1...) prefixed with _
      - trimmed to max_length characters
    Emoji and non-Latin letters are kept -- Windows handles them."""

    import unicodedata

    text = unicodedata.normalize("NFC", str(text or ""))

    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text)

    text = re.sub(r'[\\/:*?"<>|]', "_", text)

    text = re.sub(r"\s+", " ", text).strip()

    text = text[:max_length].rstrip(" .")

    if not text:
        return fallback

    if text.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        text = "_" + text

    return text


def guess_ext(entry):
    """Best-effort file extension for a yt-dlp entry, falling back to
    the URL's path when 'ext' isn't populated (happens on unprocessed
    / partially-processed info dicts)."""

    ext = (entry.get("ext") or "").lower()

    if ext:
        return ext

    url = entry.get("url") or ""

    path = urllib.parse.urlparse(url).path

    return os.path.splitext(path)[1].lstrip(".").lower()


def normalize_url_for_dedupe(url):
    """Collapse a URL down to a stable identity key. Callers are
    expected to pass an already-resolved/cleaned URL (see
    resolve_and_clean_url) so this only has to worry about casing and
    trailing slashes -- it deliberately keeps the query string, since
    for some sites (YouTube's ?v=ID) the query IS the video identity."""

    # Only the host is lowercased. Paths and queries are
    # case-sensitive on most sites (YouTube ?v= IDs, Instagram /p/
    # shortcodes), so lowercasing them could make two different videos
    # look like the same one and wrongly skip the second as "already
    # downloaded".
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path.rstrip("/")
        query = parsed.query
        if query:
            return f"{netloc}{path}?{query}"
        return f"{netloc}{path}"
    except ValueError:
        return url.strip()


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def browser_headers(referer=None):
    """A fuller set of headers than just User-Agent -- some sites'
    anti-bot checks reject requests missing the other headers an
    ordinary browser navigation always sends, even with a convincing
    User-Agent string."""

    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }

    if referer:
        headers["Referer"] = referer

    return headers


def resolve_short_link(url):
    """Follow a short/share link's redirect using realistic browser
    headers. Some sites bot-detect the default 'python-requests' agent
    (or a bare User-Agent with nothing else) and bounce the request to
    their homepage, or reject it outright -- if resolution fails or
    collapses to a bare domain, fall back to the original URL rather
    than silently handing downstream tools a broken link."""

    try:

        # stream=True: only the redirect chain and headers are needed,
        # not the (possibly large) body of the final page.
        with requests.get(
            url,
            allow_redirects=True,
            timeout=8,
            headers=browser_headers(),
            stream=True
        ) as resp:
            resolved = resp.url

    except requests.RequestException:

        return url

    path = urllib.parse.urlparse(resolved).path.strip("/")

    if not path:
        # Bounced to a bare homepage -- not a real resolution.
        return url

    return resolved


def resolve_and_clean_url(url_original):
    """Resolve short links to their final destination and strip
    tracking query strings, producing the canonical URL that both the
    dedupe check and the actual download should use. Doing this once,
    up front, is what keeps 'already downloaded' checks consistent --
    previously the check ran on the pasted URL while the saved record
    was keyed on this cleaned form, so short links never matched."""

    url_clean = unwrap_redirect(url_original.strip())

    if is_short_link(url_clean):

        url_clean = unwrap_redirect(resolve_short_link(url_clean))

    url_clean = canonicalize_url(url_clean)

    if (
        "youtube.com" in url_clean
        and "/shorts/" not in url_clean
    ):

        match = re.search(r"[?&]v=([^&#]+)", url_clean)

        if match:

            video_id = match.group(1)

            return f"https://www.youtube.com/watch?v={video_id}"

    return strip_tracking_params(url_clean)


def strip_tracking_params(url):
    """Removes share-tracking query parameters (utm_*, fbclid, igsh,
    si, ...) and the #fragment, but keeps every other parameter.
    Previously the whole query string was dropped for every non-
    YouTube URL, which broke links like facebook.com/watch/?v=123
    where the query is what identifies the video."""

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url

    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
        and not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]

    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(kept), fragment="")
    )


def scrape_images_from_page(url, session):
    """Last-resort image fallback for posts yt-dlp can't extract at
    all. Reads the page's own Open Graph / Twitter Card preview tags --
    the same standard meta tags any messaging app or search engine
    reads to build a link preview. This only parses what the page
    already publishes to any visitor; it doesn't log in as anyone or
    get around any access control. Note: these tags usually expose
    only ONE image per post, so multi-photo carousels may only yield
    the cover image this way."""

    origin = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlparse(url))

    with session.get(url, timeout=15, headers={"Referer": origin}) as resp:

        resp.raise_for_status()

        page_html = resp.text

    # Many pages write content="..." BEFORE property="...", which the
    # old single regex (property first) silently missed -- so check
    # each <meta> tag's attributes independently of their order.
    meta_tag = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)

    key_attr = re.compile(
        r'(?:property|name)\s*=\s*["\']'
        r'(?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']',
        re.IGNORECASE
    )

    content_attr = re.compile(
        r'content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
    )

    seen = set()

    urls = []

    for tag in meta_tag.findall(page_html):

        if not key_attr.search(tag):
            continue

        content = content_attr.search(tag)

        if not content:
            continue

        candidate = html.unescape(content.group(1))

        if candidate not in seen:

            seen.add(candidate)

            urls.append(candidate)

    return urls



def load_completed_urls():

    global COMPLETED_URLS, COMPLETED_IDS, COMPLETED_RECORDS

    COMPLETED_URLS = set()

    COMPLETED_IDS = set()

    COMPLETED_RECORDS = {}

    if not os.path.exists(LOG_FILE):
        return

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:

            for line in f:

                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("status") == "Completed":

                    # Recompute the key from the stored cleaned URL, so
                    # entries logged by older versions (which lowercased
                    # the whole URL) still match links pasted now.
                    cleaned = record.get("cleaned_url")

                    norm = (
                        normalize_url_for_dedupe(cleaned) if cleaned
                        else record.get("normalized_url")
                    )

                    if norm:
                        COMPLETED_URLS.add(norm)
                        COMPLETED_RECORDS[norm] = record

                    uid = record.get("video_uid")

                    if uid:
                        COMPLETED_IDS.add(uid)

    except OSError:
        pass


def append_log_entry(record):

    with _log_lock:

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def append_failed_url(url):

    with _log_lock:

        existing = set()

        if os.path.exists(FAILED_FILE):

            with open(FAILED_FILE, "r", encoding="utf-8") as f:
                existing = {line.strip() for line in f if line.strip()}

        if url not in existing:

            with open(FAILED_FILE, "a", encoding="utf-8") as f:
                f.write(url + "\n")


def remove_failed_url(url):

    with _log_lock:

        if not os.path.exists(FAILED_FILE):
            return

        with open(FAILED_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if url in lines:

            lines = [line for line in lines if line != url]

            with open(FAILED_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))


def build_requests_session(cookie_text, user_agent=None):
    """Build a requests session carrying the given cookies (Netscape
    text, already decrypted), for downloading images directly."""

    session = requests.Session()

    session.headers.update(browser_headers())

    if user_agent:
        session.headers["User-Agent"] = user_agent

    for c in parse_netscape_cookies(cookie_text):

        session.cookies.set_cookie(
            requests.cookies.create_cookie(
                name=c["name"],
                value=c["value"],
                domain=c["domain"],
                path=c["path"],
                secure=c["secure"],
                expires=c["expires"] or None,
            )
        )

    return session


# =========================================================
# UPDATE CHECKER (helpers)
# =========================================================

UPDATE_RESULT_FILE = os.path.join(APP_DIR, "update_result.json")

RESUME_URLS_FILE = os.path.join(APP_DIR, "resume_after_update.txt")

UPDATE_HELPER_FILE = os.path.join(APP_DIR, "update_helper.py")

UPDATE_CHECK_INTERVAL_SECONDS = 24 * 3600

FFMPEG_BUILDS_PAGE = "https://www.gyan.dev/ffmpeg/builds/"

FFMPEG_LATEST_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"

# (label, import name, fallback pip name, loaded_in_process)
#
# loaded_in_process=True means the running app has the package
# imported, so a new version only takes effect after a restart (and on
# Windows, PySide6's DLLs are locked while the app runs, so pip can't
# even replace them). gallery-dl runs as a separate process for every
# download, so an update to it is used immediately.
PYTHON_COMPONENTS = [
    ("yt-dlp", "yt_dlp", "yt-dlp", True),
    ("gallery-dl", "gallery_dl", "gallery-dl", False),
    ("PySide6", "PySide6", "PySide6-Essentials", True),
    ("requests", "requests", "requests", True),
    ("pyqtdarktheme", "qdarktheme", "pyqtdarktheme", True),
    ("websockets", "websockets", "websockets", True),
]


def _norm_dist(name):

    return re.sub(r"[-_.]+", "-", name).lower()


def distribution_for_import(import_name, fallback):
    """The pip package name to check for an import. Fixed names only
    (yt-dlp, gallery-dl, PySide6, requests, pyqtdarktheme,
    websockets), so the
    update window always shows exactly these."""

    return fallback


def installed_version(dist, import_name=None):
    """Installed version of a package. Asks pip's records first; if
    those aren't there -- a PyInstaller build doesn't copy them, which
    made every package show as "(missing)" -- asks the package itself
    via its __version__."""

    from importlib.metadata import version, PackageNotFoundError

    try:
        return version(dist)
    except PackageNotFoundError:
        pass

    if not import_name:
        return None

    import importlib

    for module_name in (import_name, f"{import_name}.version"):

        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        found = getattr(module, "__version__", None)

        if found:
            return str(found)

    return None


def running_as_bundle():
    """True inside a PyInstaller build. The packages are frozen into
    the app there, so pip can't update them -- rebuilding can."""

    return bool(getattr(sys, "frozen", False))


def _packaging_tools():
    """(Version, SpecifierSet) from 'packaging', or pip's bundled copy
    of it, or (None, None) if neither is available."""

    try:
        from packaging.version import Version
        from packaging.specifiers import SpecifierSet
        return Version, SpecifierSet
    except ImportError:
        pass

    try:
        from pip._vendor.packaging.version import Version
        from pip._vendor.packaging.specifiers import SpecifierSet
        return Version, SpecifierSet
    except ImportError:
        return None, None


def latest_pypi_version(dist):
    """Returns (newest version installable on this Python, newest
    version overall).

    PyPI's "latest" can require a different Python than the one
    running this app, in which case pip installs an earlier release.
    Comparing against a version pip can never install would show an
    update forever, so this picks the same version pip would."""

    with requests.get(
        f"https://pypi.org/pypi/{dist}/json", timeout=10
    ) as resp:

        resp.raise_for_status()

        data = resp.json()

    newest = data["info"]["version"]

    Version, SpecifierSet = _packaging_tools()

    if Version is None:
        return newest, newest

    python_version = ".".join(str(x) for x in sys.version_info[:3])

    best = None

    for version_text, files in (data.get("releases") or {}).items():

        try:
            version = Version(version_text)
        except Exception:
            continue

        if version.is_prerelease or version.is_devrelease:
            continue

        usable_files = [f for f in files if not f.get("yanked")]

        if not usable_files:
            continue

        def fits(file_info):
            spec = file_info.get("requires_python")
            if not spec:
                return True
            try:
                return python_version in SpecifierSet(spec)
            except Exception:
                return True

        if any(fits(f) for f in usable_files) and (best is None or version > best):
            best = version

    return (str(best) if best else newest), newest


def is_newer(latest, installed):

    try:
        from packaging.version import Version
        return Version(latest) > Version(installed)
    except Exception:
        nums = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))
        return nums(latest) > nums(installed)


def ffmpeg_installed_version():
    """(path, version string) of the ffmpeg this app would use."""

    path = find_ffmpeg()

    if not path:
        return None, None

    try:

        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW
        ).stdout

    except (OSError, subprocess.TimeoutExpired):
        return path, None

    match = re.search(r"ffmpeg version (\S+)", out)

    return path, (match.group(1) if match else None)


def check_all_components():
    """Returns one dict per component. Does network requests, so call
    it from a background thread."""

    results = []

    for label, import_name, fallback, in_process in PYTHON_COMPONENTS:

        dist = distribution_for_import(import_name, fallback)

        item = {
            "label": label,
            "dist": dist,
            "installed": installed_version(dist, import_name),
            "latest": None,
            "newest": None,
            "status": "",
            "updatable": True,
            "outdated": False,
            "in_process": in_process,
        }

        try:
            item["latest"], item["newest"] = latest_pypi_version(item["dist"])
        except (requests.RequestException, ValueError, KeyError) as e:
            item["status"] = f"Couldn't check: {e}"
            item["updatable"] = False
            results.append(item)
            continue

        if running_as_bundle():
            # Nothing pip can do inside a built app.
            item["updatable"] = False

        if not item["installed"]:
            item["status"] = "Not installed"
            item["outdated"] = True
        elif is_newer(item["latest"], item["installed"]):
            item["status"] = (
                "Update available (rebuild the app to update)"
                if running_as_bundle() else "Update available"
            )
            item["outdated"] = True
        else:
            item["status"] = "Up to date"

        results.append(item)

    # FFmpeg isn't a pip package -- compare against gyan.dev's
    # current release and point at the download page instead.
    path, raw_version = ffmpeg_installed_version()

    item = {
        "label": "FFmpeg",
        "dist": None,
        "installed": raw_version or ("?" if path else None),
        "latest": None,
        "status": "",
        "updatable": False,
        "outdated": False,
        "in_process": False,
    }

    try:

        with requests.get(FFMPEG_LATEST_URL, timeout=10) as resp:
            resp.raise_for_status()
            item["latest"] = resp.text.strip()

    except requests.RequestException as e:

        item["status"] = f"Couldn't check: {e}"

    if not path:
        item["status"] = "Not found"
        item["outdated"] = True
    elif item["latest"] and raw_version:

        # Release builds look like "7.1.1-essentials_build-www.gyan.dev";
        # git builds ("2025-01-20-git-...", "N-...") have no release
        # number to compare.
        release = re.match(r"(\d+(?:\.\d+)+)", raw_version)

        if "git" in raw_version or not release:
            item["status"] = "Development build (can't compare)"
        elif is_newer(item["latest"], release.group(1)):
            item["status"] = "Update available (manual download)"
            item["outdated"] = True
        else:
            item["status"] = "Up to date"

    elif not item["status"]:
        item["status"] = "Unknown version"

    results.append(item)

    return results


# Written to app_data and run as a separate process by "Update &
# Restart". It waits for this app to close (so nothing is locked),
# runs pip, saves the result for the new instance to report, then
# starts the app again.
UPDATE_HELPER_SCRIPT = r'''
import json, os, subprocess, sys, time


def wait_for_exit(pid, timeout=120):

    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            kernel32.CloseHandle(handle)
        return

    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)


def detach_kwargs():

    if os.name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED | NEW_GROUP
    return {"start_new_session": True}


def main():

    args = json.loads(sys.argv[1])

    wait_for_exit(args["pid"])

    time.sleep(1.5)  # give Windows a moment to release file locks

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    output = ""

    try:
        proc = subprocess.run(
            [args["python"], "-m", "pip", "install", "--upgrade"]
            + args["packages"],
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=flags
        )
        output += proc.stdout + proc.stderr
        result = {
            "ok": proc.returncode == 0,
            "packages": args["packages"],
            "output": output[-4000:],
        }
    except Exception as e:
        result = {"ok": False, "packages": args["packages"], "output": str(e)}

    with open(args["result_file"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    subprocess.Popen(
        [args["python"], args["script"]],
        cwd=args["cwd"],
        close_fds=True,
        **detach_kwargs()
    )


main()
'''


def launch_update_helper(packages):
    """Starts the detached helper. Raises OSError if it can't."""

    with open(UPDATE_HELPER_FILE, "w", encoding="utf-8") as f:
        f.write(UPDATE_HELPER_SCRIPT)

    args = {
        "pid": os.getpid(),
        "python": sys.executable,
        "script": os.path.abspath(__file__),
        "cwd": os.getcwd(),
        "packages": list(packages),
        "result_file": UPDATE_RESULT_FILE,
    }

    kwargs = {"close_fds": True}

    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        [sys.executable, UPDATE_HELPER_FILE, json.dumps(args)],
        **kwargs
    )


def run_pip_upgrade(packages):
    """Upgrade packages right now, in this process's environment.
    Only for packages the app doesn't have loaded (gallery-dl)."""

    try:

        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade"]
            + list(packages),
            capture_output=True,
            text=True,
            timeout=900,
            creationflags=_NO_WINDOW
        )

        return proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]

    except (OSError, subprocess.TimeoutExpired) as e:

        return False, str(e)


# =========================================================
# MASTER PASSWORD DIALOGS
# =========================================================

class PasswordDialog(QDialog):
    """mode="unlock": one field (+ Skip / Forgot password).
    mode="new": password + confirm, minimum length enforced."""

    MIN_LENGTH = 8

    RESET = 2  # extra result code for "Forgot password"

    def __init__(self, mode, parent=None, message=None):

        super().__init__(parent)

        self.mode = mode

        self.password = None

        self.setWindowTitle(
            "Unlock saved cookies" if mode == "unlock" else "Set master password"
        )

        self.resize(420, 170)

        layout = QVBoxLayout(self)

        info = QLabel(message or (
            "Enter your master password to unlock the saved site cookies."
            if mode == "unlock" else
            f"Choose a master password (at least {self.MIN_LENGTH} "
            "characters). It can't be recovered -- if you forget it, the "
            "saved cookies are deleted and you sign in to those sites again."
        ))

        info.setWordWrap(True)

        layout.addWidget(info)

        self.pw1 = QLineEdit()

        self.pw1.setEchoMode(QLineEdit.Password)

        self.pw1.setPlaceholderText("Master password")

        layout.addWidget(self.pw1)

        self.pw2 = None

        if mode == "new":

            self.pw2 = QLineEdit()

            self.pw2.setEchoMode(QLineEdit.Password)

            self.pw2.setPlaceholderText("Repeat password")

            layout.addWidget(self.pw2)

        self.error_label = QLabel("")

        self.error_label.setStyleSheet("color: #e06c75;")

        layout.addWidget(self.error_label)

        btn_layout = QHBoxLayout()

        if mode == "unlock":

            forgot_btn = QPushButton("FORGOT PASSWORD...")

            forgot_btn.clicked.connect(self.forgot)

            btn_layout.addWidget(forgot_btn)

        btn_layout.addStretch()

        ok_btn = QPushButton("UNLOCK" if mode == "unlock" else "SET PASSWORD")

        ok_btn.setDefault(True)

        ok_btn.clicked.connect(self.try_accept)

        cancel_btn = QPushButton("SKIP" if mode == "unlock" else "CANCEL")

        cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(ok_btn)

        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def try_accept(self):

        pw = self.pw1.text()

        if self.mode == "new":

            if len(pw) < self.MIN_LENGTH:
                self.error_label.setText(
                    f"Use at least {self.MIN_LENGTH} characters."
                )
                return

            if pw != self.pw2.text():
                self.error_label.setText("The passwords don't match.")
                return

            self.password = pw

            self.accept()

            return

        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            ok = cookie_vault.unlock(pw)
        except CookieVaultError as e:
            ok = False
            self.error_label.setText(str(e))
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            self.accept()
        else:
            self.error_label.setText("Wrong password.")
            self.pw1.selectAll()
            self.pw1.setFocus()

    def forgot(self):

        reply = QMessageBox.warning(
            self,
            "Forgot master password",
            "The saved cookies can't be opened without the password.\n\n"
            "Reset deletes them and switches protection to your Windows "
            "account (no password). You'll need to sign in to those "
            "sites again with LOGIN & CAPTURE. You can set a new master "
            "password afterwards in MANAGE COOKIES.",
            QMessageBox.Reset | QMessageBox.Cancel
        )

        if reply == QMessageBox.Reset:

            reset_protected_cookies()

            signals.log.emit(
                "Master password reset: protected cookies were deleted."
            )

            self.done(self.RESET)


def ensure_cookie_vault_unlocked(parent):
    """Prompts for the master password if the cookies are locked.
    Returns True when cookies can be read/written afterwards."""

    if not cookie_vault.is_locked():
        return True

    dlg = PasswordDialog("unlock", parent)

    result = dlg.exec()

    return result in (QDialog.Accepted, PasswordDialog.RESET)


# =========================================================
# LOGIN / CAPTCHA BROWSER (the PC's own Edge or Chrome)
# =========================================================

# Sign-in happens in the Edge (or Chrome) already installed on the PC,
# started as a separate window with its own profile folder and a local
# DevTools port. The app only reads and writes cookies through that
# port -- it never clicks, types or solves anything; the human signs
# in / passes any verification themselves, exactly as before.
#
# Talking to the browser directly over the DevTools protocol (with the
# small 'websockets' package) instead of through Playwright avoids
# bundling Playwright's own Node.js runtime into the .exe.


class CdpError(Exception):
    """The browser answered a DevTools command with an error."""


class CdpClosed(Exception):
    """The browser (or its DevTools connection) went away."""


def purge_browser_profiles():
    """Deletes the sign-in browser's own saved data (its copy of your
    sessions, cache, etc.) for every site. Only safe while no sign-in
    browser is open."""

    shutil.rmtree(BROWSER_PROFILES_DIR, ignore_errors=True)

    os.makedirs(BROWSER_PROFILES_DIR, exist_ok=True)

    signals.log.emit("Cleared the sign-in browser's saved data.")


def find_login_browser():
    """(path, label) of the browser to sign in with, or (None, None).
    A manual "login_browser_path" in config.json wins, then Microsoft
    Edge (on every Windows PC), then Google Chrome."""

    custom = load_config().get("login_browser_path")

    if custom and os.path.isfile(custom):
        return custom, os.path.splitext(os.path.basename(custom))[0]

    browsers = (
        ("msedge.exe", "Microsoft Edge", ("Microsoft", "Edge", "Application")),
        ("chrome.exe", "Google Chrome", ("Google", "Chrome", "Application")),
    )

    if sys.platform == "win32":

        import winreg

        roots = [
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LOCALAPPDATA"),
        ]

        for exe, label, subdirs in browsers:

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):

                try:
                    with winreg.OpenKey(
                        hive,
                        rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
                    ) as key:
                        path = winreg.QueryValue(key, None).strip('"')
                except OSError:
                    continue

                if path and os.path.isfile(path):
                    return path, label

            for root in roots:

                if not root:
                    continue

                path = os.path.join(root, *subdirs, exe)

                if os.path.isfile(path):
                    return path, label

        return None, None

    for name, label in (
        ("microsoft-edge", "Microsoft Edge"),
        ("google-chrome", "Google Chrome"),
        ("chromium", "Chromium"),
        ("chromium-browser", "Chromium"),
    ):
        path = shutil.which(name)

        if path:
            return path, label

    mac_apps = (
        ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge"),
        ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
    )

    for path, label in mac_apps:
        if os.path.isfile(path):
            return path, label

    return None, None


def netscape_text_from_cdp_cookies(cookies):
    """Turns the browser's cookies (DevTools format) into Netscape
    cookies.txt text, the format yt-dlp, gallery-dl and requests all
    read."""

    lines = ["# Netscape HTTP Cookie File\n"]

    for c in cookies:

        name = c.get("name") or ""

        if not name:
            continue

        domain = c.get("domain") or ""
        path_ = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"

        expires = c.get("expires") or 0

        expiry = 0 if c.get("session") or expires <= 0 else int(expires)

        prefix = "#HttpOnly_" if c.get("httpOnly") else ""

        lines.append(
            f"{prefix}{domain}\t{include_subdomains}\t{path_}\t{secure}\t"
            f"{expiry}\t{name}\t{c.get('value') or ''}\n"
        )

    return "".join(lines)


def cdp_cookies_from_netscape_text(text):
    """The reverse, for loading saved cookies back into the sign-in
    browser so you're still logged in there."""

    result = []

    for c in parse_netscape_cookies(text):

        host = c["domain"].lstrip(".")

        if not host or not c["name"]:
            continue

        scheme = "https" if c["secure"] else "http"

        cookie = {
            "name": c["name"],
            "value": c["value"],
            "url": f"{scheme}://{host}{c['path']}",
            "path": c["path"],
            "secure": c["secure"],
            "httpOnly": c["http_only"],
        }

        # A leading dot means the cookie also covers subdomains. Without
        # one it's a host-only cookie, which is what the URL alone
        # creates (and the only form __Host- cookies accept).
        if c["domain"].startswith("."):
            cookie["domain"] = c["domain"]

        if c["expires"] > 0:
            cookie["expires"] = c["expires"]

        result.append(cookie)

    return result


class CdpConnection:
    """A minimal DevTools-protocol client over one websocket. Used only
    from the sign-in session's own thread."""

    def __init__(self, ws_url):

        try:
            # websockets 15+ would otherwise route through a system
            # proxy, even for 127.0.0.1.
            self.ws = ws_connect(ws_url, max_size=None, open_timeout=15, proxy=None)
        except TypeError:
            self.ws = ws_connect(ws_url, max_size=None, open_timeout=15)

        self._next_id = 0

        self._pending_events = []

    def _recv(self, timeout):

        try:
            raw = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except (WsConnectionClosed, OSError) as e:
            raise CdpClosed(str(e)) from e

        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    def call(self, method, params=None, session_id=None, timeout=15):

        self._next_id += 1

        msg_id = self._next_id

        message = {"id": msg_id, "method": method, "params": params or {}}

        if session_id:
            message["sessionId"] = session_id

        try:
            self.ws.send(json.dumps(message))
        except (WsConnectionClosed, OSError) as e:
            raise CdpClosed(str(e)) from e

        deadline = time.monotonic() + timeout

        while True:

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                raise CdpError(f"The browser didn't answer {method} in time.")

            data = self._recv(remaining)

            if not data:
                continue

            if data.get("id") == msg_id:

                if "error" in data:
                    raise CdpError(data["error"].get("message") or str(data["error"]))

                return data.get("result") or {}

            if "method" in data:
                self._pending_events.append(data)

    def poll_events(self, timeout):
        """Events that arrived so far, waiting up to 'timeout' seconds
        for one if none are queued."""

        if self._pending_events:
            events, self._pending_events = self._pending_events, []
            return events

        data = self._recv(timeout)

        if data and "method" in data:
            return [data]

        return []

    def close(self):

        try:
            self.ws.close()
        except Exception:
            pass


class BrowserSessionSignals(QObject):

    ready = Signal(str)            # browser name, once it's open
    page_status = Signal(int)      # HTTP status of a page load
    cookies = Signal(list)         # latest cookie snapshot
    save_data = Signal(list, str)  # cookies + user agent, for saving
    closed = Signal(str)           # "" = browser closed, else an error


# Domains with a sign-in browser open (or still shutting down). Two
# browsers can't share one profile folder.
_ACTIVE_LOGIN_SESSIONS = {}

_login_sessions_lock = Lock()


class LoginBrowserSession:
    """Runs one sign-in browser on its own thread and relays what
    happens there to the dialog through Qt signals. The dialog sends
    commands back through a queue: "save", "fresh", "close"."""

    SNAPSHOT_INTERVAL = 2.0

    def __init__(self, domain, url, saved_cookie_text, protected):

        self.domain = domain

        self.start_url = url

        self.saved_cookie_text = saved_cookie_text

        # Cookie protection on: use a throwaway profile that's deleted
        # afterwards, so the browser never keeps its own plain copy of
        # your sessions (saved cookies are loaded into it instead).
        self.protected = protected

        self.signals = BrowserSessionSignals()

        self.commands = Queue()

        self.label = ""

        self.user_agent = ""

        self.proc = None

        self.cdp = None

        self.profile_dir = None

        self.pages = {}  # DevTools session id -> page target id

        self._last_snapshot_key = None

        self._thread = Thread(target=self._run, daemon=True)

    def start(self):

        self._thread.start()

    def send(self, *command):

        self.commands.put(command)

    # ----- session thread -----

    def _run(self):

        error = ""

        try:
            self._launch()
            self._setup()
            self.signals.ready.emit(self.label)
            self._loop()
        except CdpClosed:
            pass
        except Exception as e:
            error = str(e) or e.__class__.__name__
        finally:
            self._shutdown()

            with _login_sessions_lock:
                if _ACTIVE_LOGIN_SESSIONS.get(self.domain) is self:
                    _ACTIVE_LOGIN_SESSIONS.pop(self.domain, None)

            self.signals.closed.emit(error)

    def _stop_requested(self):
        """True if the dialog asked to close before the browser was
        even up. Other commands are put back for later."""

        keep = []

        stop = False

        while True:
            try:
                command = self.commands.get_nowait()
            except Empty:
                break

            if command[0] == "close":
                stop = True
            else:
                keep.append(command)

        for command in keep:
            self.commands.put(command)

        return stop

    def _launch(self):

        exe, label = find_login_browser()

        if not exe:
            raise RuntimeError(
                "Couldn't find Microsoft Edge or Google Chrome on this PC. "
                "Install one of them, or set \"login_browser_path\" in "
                "config.json to a Chromium-based browser."
            )

        self.label = label

        if self.protected:

            self.profile_dir = tempfile.mkdtemp(prefix="vd_signin_")

        else:

            folder = re.sub(r"[^\w.-]", "_", self.domain) or "site"

            self.profile_dir = os.path.join(BROWSER_PROFILES_DIR, folder)

            os.makedirs(self.profile_dir, exist_ok=True)

        # The browser writes its DevTools port here once it's up.
        port_file = os.path.join(self.profile_dir, "DevToolsActivePort")

        try:
            os.remove(port_file)
        except FileNotFoundError:
            pass

        self.proc = subprocess.Popen([
            exe,
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ])

        deadline = time.monotonic() + 30

        exited_at = None

        while True:

            if self._stop_requested():
                raise CdpClosed()

            try:
                with open(port_file, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f.read().splitlines()]
            except OSError:
                lines = []

            if len(lines) >= 2 and lines[0].isdigit() and lines[1]:
                break

            now = time.monotonic()

            # The browser exiting straight away usually means a window
            # from an earlier sign-in for this site still has the
            # profile open and took over the request.
            if self.proc.poll() is not None:

                exited_at = exited_at or now

                if now - exited_at > 3:
                    raise RuntimeError(
                        f"{label} closed right away. If a browser window "
                        f"from an earlier sign-in for {self.domain} is "
                        "still open, close it and try again."
                    )

            if now > deadline:
                raise RuntimeError(f"{label} didn't start in time.")

            time.sleep(0.2)

        self.cdp = CdpConnection(f"ws://127.0.0.1:{lines[0]}{lines[1]}")

    def _setup(self):

        version = self.cdp.call("Browser.getVersion")

        # Downloads using these cookies present the same browser
        # identity (see save).
        self.user_agent = version.get("userAgent") or ""

        # Put the saved (decrypted) cookies into the browser, so you're
        # still signed in there. One at a time, so one the browser
        # rejects doesn't take the rest with it.
        for cookie in cdp_cookies_from_netscape_text(self.saved_cookie_text):
            try:
                self.cdp.call("Storage.setCookies", {"cookies": [cookie]})
            except CdpError:
                pass

        for info in self.cdp.call("Target.getTargets").get("targetInfos", []):
            if info.get("type") == "page":
                self._attach(info["targetId"])

        # From now on, new tabs and pop-ups (e.g. a "Sign in with ..."
        # window) are reported as events and watched too.
        self.cdp.call("Target.setDiscoverTargets", {"discover": True})

        self._navigate(self.start_url)

        self._snapshot()

    def _attach(self, target_id):

        if target_id in self.pages.values():
            return

        try:
            session_id = self.cdp.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True}
            )["sessionId"]

            # Needed to see each page load's HTTP status.
            self.cdp.call("Network.enable", session_id=session_id)

        except (CdpError, KeyError):
            return

        self.pages[session_id] = target_id

    def _navigate(self, url):

        for session_id in list(self.pages):

            try:
                self.cdp.call("Page.navigate", {"url": url}, session_id=session_id)
                self.cdp.call("Page.bringToFront", session_id=session_id)
                return
            except CdpError:
                self.pages.pop(session_id, None)

        # Every tab was closed -- open a new one.
        self.cdp.call("Target.createTarget", {"url": url})

    def _loop(self):

        last_snapshot = time.monotonic()

        while True:

            self._handle_commands()

            for event in self.cdp.poll_events(0.25):
                self._handle_event(event)

            if time.monotonic() - last_snapshot >= self.SNAPSHOT_INTERVAL:

                self._snapshot()

                last_snapshot = time.monotonic()

    def _handle_commands(self):

        while True:

            try:
                command = self.commands.get_nowait()
            except Empty:
                return

            name = command[0]

            if name == "close":
                raise CdpClosed()

            if name == "save":

                cookies = self.cdp.call("Storage.getCookies").get("cookies", [])

                self.signals.save_data.emit(cookies, self.user_agent)

            elif name == "fresh":

                # Each site has its own profile, so this only affects
                # this site's sign-in browser.
                self.cdp.call("Storage.clearCookies")

                for session_id in list(self.pages):
                    try:
                        self.cdp.call("Network.clearBrowserCache", session_id=session_id)
                        break
                    except CdpError:
                        continue

                self._last_snapshot_key = None

                self._navigate(self.start_url)

    def _handle_event(self, event):

        method = event.get("method")

        params = event.get("params") or {}

        if method == "Target.targetCreated":

            info = params.get("targetInfo") or {}

            if info.get("type") == "page":
                self._attach(info.get("targetId"))

        elif method == "Target.targetDestroyed":

            target_id = params.get("targetId")

            for session_id, page_id in list(self.pages.items()):
                if page_id == target_id:
                    self.pages.pop(session_id, None)

        elif method == "Target.detachedFromTarget":

            self.pages.pop(params.get("sessionId"), None)

        elif method == "Network.responseReceived":

            session_id = event.get("sessionId")

            # Only the page itself (its main frame, whose id is the
            # tab's), not images, scripts or embedded frames.
            if (
                session_id in self.pages
                and params.get("type") == "Document"
                and params.get("frameId") == self.pages[session_id]
            ):
                status = (params.get("response") or {}).get("status") or 0

                self.signals.page_status.emit(int(status))

    def _snapshot(self):
        """Keeps the dialog's copy of the cookies current, so they can
        still be saved if the browser window gets closed first."""

        cookies = self.cdp.call("Storage.getCookies").get("cookies", [])

        key = json.dumps(cookies, sort_keys=True)

        if key != self._last_snapshot_key:

            self._last_snapshot_key = key

            self.signals.cookies.emit(cookies)

    def _shutdown(self):

        if self.cdp is not None:

            try:
                self.cdp.call("Browser.close", timeout=5)
            except (CdpError, CdpClosed):
                pass

            self.cdp.close()

        if self.proc is not None:

            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        if self.protected and self.profile_dir:

            # The browser's helper processes can hold files for a
            # moment after it exits.
            for _ in range(20):

                shutil.rmtree(self.profile_dir, ignore_errors=True)

                if not os.path.exists(self.profile_dir):
                    break

                time.sleep(0.5)


class LoginBrowserDialog(QDialog):
    """A small always-on-top control window next to the real browser
    window. The human logs in / solves any verification challenge in
    the browser; this dialog only collects the cookies that get set and
    exports them for yt-dlp to reuse. It never automates or solves
    anything on its own."""

    def __init__(self, domain, url, parent=None):

        super().__init__(parent)

        self.setWindowTitle(f"Sign in / verify \u2014 {domain}")

        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.setMinimumWidth(520)

        self.domain = domain

        self.start_url = url

        self.saved = False

        # HTTP status of the last page load (e.g. 403 when the site's
        # bot protection refuses this browser).
        self.blocked_status = None

        self.last_cookies = []

        self.user_agent = ""

        self.browser_open = False

        self.was_ready = False

        self.saving = False

        self.session = None

        layout = QVBoxLayout(self)

        legend_label = QLabel(
            "A browser window opens for this site. Sign in there, then "
            "come back here and click 'Save Session & Close' once you "
            "can see your account."
        )

        legend_label.setWordWrap(True)

        layout.addWidget(legend_label)

        self.status_label = QLabel("Opening the browser...")

        self.status_label.setWordWrap(True)

        layout.addWidget(self.status_label)

        # Warning line, only shown when the site blocks the browser.
        self.info_label = QLabel("")

        self.info_label.setWordWrap(True)

        self.info_label.setStyleSheet("color: #e06c75; font-weight: bold;")

        self.info_label.hide()

        layout.addWidget(self.info_label)

        btn_layout = QHBoxLayout()

        self.fresh_btn = QPushButton("START FRESH")

        self.fresh_btn.setToolTip(
            "Clear the sign-in browser's cookies and cache for this site "
            "and reload -- useful if the site keeps showing an error."
        )

        self.fresh_btn.clicked.connect(self.start_fresh)

        self.save_btn = QPushButton("SAVE SESSION && CLOSE")

        self.save_btn.clicked.connect(self.save_and_close)

        cancel_btn = QPushButton("CANCEL")

        cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(self.fresh_btn)

        btn_layout.addStretch()

        btn_layout.addWidget(self.save_btn)

        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        self.fresh_btn.setEnabled(False)

        self.save_btn.setEnabled(False)

        # Falls back to the last snapshot if the browser doesn't
        # answer a save request.
        self.save_timer = QTimer(self)

        self.save_timer.setSingleShot(True)

        self.save_timer.setInterval(10000)

        self.save_timer.timeout.connect(
            lambda: self.on_save_data(self.last_cookies, self.user_agent)
        )

        if not LOGIN_BROWSER_AVAILABLE:
            QTimer.singleShot(0, self.report_missing_websockets)
            return

        try:
            saved_text = read_site_cookies(domain)
        except (OSError, CookieVaultLocked, CookieVaultError):
            saved_text = None

        with _login_sessions_lock:

            if domain not in _ACTIVE_LOGIN_SESSIONS:

                self.session = LoginBrowserSession(
                    domain, url, saved_text, cookie_vault.is_protected()
                )

                _ACTIVE_LOGIN_SESSIONS[domain] = self.session

        if self.session is None:
            QTimer.singleShot(0, self.report_already_open)
            return

        s = self.session.signals

        s.ready.connect(self.on_ready)

        s.page_status.connect(self.on_page_status)

        s.cookies.connect(self.on_cookies)

        s.save_data.connect(self.on_save_data)

        s.closed.connect(self.on_closed)

        self.session.start()

    def report_missing_websockets(self):

        QMessageBox.critical(
            self,
            "Sign-in not available",
            "Signing in needs the 'websockets' package.\n"
            "Install it with: pip install websockets"
        )

        self.reject()

    def report_already_open(self):

        QMessageBox.warning(
            self,
            "Sign-in already open",
            f"A sign-in browser for {self.domain} is still open or "
            "closing. Close it and try again."
        )

        self.reject()

    def set_buttons_enabled(self, enabled):

        self.save_btn.setEnabled(enabled)

        self.fresh_btn.setEnabled(enabled and self.browser_open)

    def on_ready(self, label):

        self.browser_open = True

        self.was_ready = True

        self.status_label.setText(
            f"Waiting for you in the {label} window."
        )

        self.set_buttons_enabled(True)

    def on_page_status(self, code):
        """Watches for the site refusing the browser itself -- e.g.
        TikTok's "Access denied / HTTP ERROR 403". The cookies such a
        page sets mark you as a blocked visitor, so saving them would
        make every download from that site fail too."""

        if code >= 400:

            self.blocked_status = code

            self.info_label.show()

            self.info_label.setText(
                f"{self.domain} refused the sign-in browser (HTTP {code}). "
                "Don't save this session. Click START FRESH to clear the "
                "browser's data for the site and try again, or try again "
                "later."
            )

        elif code:

            self.blocked_status = None

            self.info_label.hide()

            self.info_label.setText("")

    def on_cookies(self, cookies):

        self.last_cookies = cookies

    def on_closed(self, error):

        self.browser_open = False

        self.fresh_btn.setEnabled(False)

        if not self.was_ready:

            QMessageBox.critical(
                self,
                "Couldn't open the sign-in browser",
                error or "The browser closed before it was ready."
            )

            self.reject()

            return

        if self.saving:
            # The browser went away mid-save: use the last snapshot.
            self.on_save_data(self.last_cookies, self.user_agent)
            return

        if error:
            self.status_label.setText(f"The browser stopped responding: {error}")
        else:
            self.status_label.setText(
                "The browser window was closed. You can still save what "
                "was captured, or cancel."
            )

    def start_fresh(self):
        """Clears the sign-in browser's own cookies and cache for the
        site and reloads. Once a site flags a visit, the "blocked
        visitor" cookies it sets (plus any saved ones loaded in on open)
        make every later attempt start out flagged too. Your saved
        cookies in Manage Cookies are only replaced if you save again."""

        reply = QMessageBox.question(
            self,
            "Start fresh?",
            f"Clear the sign-in browser's cookies and cache for "
            f"{self.domain} and reload the page?\n\nYour saved cookies "
            "in Manage Cookies stay as they are unless you save a new "
            "session.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes or not self.browser_open:
            return

        self.session.send("fresh")

        self.last_cookies = []

        self.blocked_status = None

        self.info_label.hide()

        self.info_label.setText("")

    def save_and_close(self):

        if not self.browser_open:
            self.finish_save(self.last_cookies)
            return

        # Ask the browser for its current cookies; the answer arrives
        # in on_save_data.
        self.saving = True

        self.set_buttons_enabled(False)

        self.session.send("save")

        self.save_timer.start()

    def on_save_data(self, cookies, user_agent):

        if not self.saving and self.browser_open:
            return

        self.saving = False

        self.save_timer.stop()

        if user_agent:
            self.user_agent = user_agent

        self.last_cookies = cookies or self.last_cookies

        self.finish_save(self.last_cookies)

    def finish_save(self, cookies):

        self.set_buttons_enabled(True)

        if not cookies:

            QMessageBox.warning(
                self,
                "No cookies captured",
                "No cookies were captured yet. Make sure the page finished "
                "loading and you completed login before saving."
            )

            return

        if self.blocked_status:

            box = QMessageBox(self)

            box.setIcon(QMessageBox.Warning)

            box.setWindowTitle("The site blocked the sign-in browser")

            box.setText(
                f"{self.domain} answered the sign-in browser with HTTP "
                f"{self.blocked_status}, so you aren't actually signed in. "
                "Saving these cookies would make downloads from this site "
                "fail too."
            )

            box.setInformativeText(
                "Try START FRESH, try again later, or use BROWSE... in "
                "Manage Cookies with a cookies.txt you already have."
            )

            box.addButton("DON'T SAVE", QMessageBox.RejectRole)

            save_anyway = box.addButton("SAVE ANYWAY", QMessageBox.DestructiveRole)

            box.exec()

            if box.clickedButton() != save_anyway:
                return

        text = netscape_text_from_cdp_cookies(cookies)

        if not ensure_cookie_vault_unlocked(self):
            return

        try:

            save_site_cookies(self.domain, text)

            # Sites like TikTok tie a session to the browser that
            # created it and answer 403 if the cookies later arrive
            # with a different User-Agent -- so downloads using these
            # cookies present the same one as the sign-in browser.
            set_site_user_agent(self.domain, self.user_agent or None)

        except (OSError, CookieVaultLocked, CookieVaultError) as e:

            QMessageBox.warning(
                self, "Couldn't save the session",
                f"The cookies couldn't be saved:\n{e}"
            )

            return

        self.saved = True

        self.accept()

    def done(self, result):
        # Stop listening and close the browser once this window closes.
        self.save_timer.stop()

        if self.session is not None:

            s = self.session.signals

            for sig, slot in (
                (s.ready, self.on_ready),
                (s.page_status, self.on_page_status),
                (s.cookies, self.on_cookies),
                (s.save_data, self.on_save_data),
                (s.closed, self.on_closed),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

            self.session.send("close")

        super().done(result)


class LoginRequest:
    """Passed from a worker thread to the main thread to request a
    login window, then waited on until the user finishes (or cancels)."""

    def __init__(self, domain, url):

        self.domain = domain

        self.url = url

        self.event = Event()

        self.success = False


# =========================================================
# COOKIE MANAGER DIALOG
# =========================================================

class CookieManagerDialog(QDialog):

    def __init__(self, parent=None):

        super().__init__(parent)

        self.setWindowTitle("Cookie Manager")

        self.resize(950, 450)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Manage per-site cookies used for authenticated downloads. "
            "Use 'Login & Capture' to sign in through Edge or Chrome, "
            "or 'Browse...' for a cookies.txt you already have. Hover a "
            "file name to see its full location."
        )

        info.setWordWrap(True)

        layout.addWidget(info)

        folder_row = QHBoxLayout()

        folder_label = QLabel(f"Cookies folder: {COOKIES_DIR}")

        folder_label.setWordWrap(True)

        open_folder_btn = QPushButton("OPEN FOLDER")

        open_folder_btn.clicked.connect(self.open_cookies_folder)

        folder_row.addWidget(folder_label, stretch=1)

        folder_row.addWidget(open_folder_btn)

        layout.addLayout(folder_row)

        # =============================================
        # PROTECTION
        # =============================================

        protect_row = QHBoxLayout()

        protect_row.addWidget(QLabel("Protection:"))

        self.protect_combo = QComboBox()

        for key, label in COOKIE_MODES.items():
            self.protect_combo.addItem(label, key)

        if not IS_WINDOWS:

            # DPAPI is Windows-only; grey the protected modes out.
            model = self.protect_combo.model()

            for i in range(1, self.protect_combo.count()):
                model.item(i).setEnabled(False)

        protect_row.addWidget(self.protect_combo, 1)

        self.protect_apply_btn = QPushButton("APPLY")

        self.protect_apply_btn.clicked.connect(self.apply_protection)

        protect_row.addWidget(self.protect_apply_btn)

        self.change_pw_btn = QPushButton("CHANGE PASSWORD")

        self.change_pw_btn.clicked.connect(self.change_password)

        protect_row.addWidget(self.change_pw_btn)

        self.lock_btn = QPushButton("LOCK NOW")

        self.lock_btn.setToolTip(
            "Forget the master password until you enter it again."
        )

        self.lock_btn.clicked.connect(self.lock_or_unlock)

        protect_row.addWidget(self.lock_btn)

        layout.addLayout(protect_row)

        self.protect_info = QLabel()

        self.protect_info.setWordWrap(True)

        layout.addWidget(self.protect_info)

        self.table = QTableWidget()

        self.table.setColumnCount(4)

        self.table.setHorizontalHeaderLabels(
            ["Domain", "Cookie File", "Login & Capture", "Browse / Clear"]
        )

        header = self.table.horizontalHeader()

        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(1, QHeaderView.Stretch)

        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()

        add_btn = QPushButton("ADD SITE")

        add_btn.clicked.connect(self.add_site)

        close_btn = QPushButton("CLOSE")

        close_btn.clicked.connect(self.accept)

        btn_layout.addWidget(add_btn)

        btn_layout.addStretch()

        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        self.reload_table()

    PROTECTION_HELP = {
        "plain": (
            "Cookie files are plain text: anyone or anything that can "
            "read them (another user, malware, a cloud-synced or backed-"
            "up copy) can use your logged-in sessions."
        ),
        "windows": (
            "Cookies are encrypted with Windows (DPAPI). Only your Windows "
            "account on this PC can decrypt them -- copies elsewhere are "
            "useless. The sign-in browser uses a throwaway profile that's "
            "deleted when it closes."
        ),
        "password": (
            "Encrypted with Windows (DPAPI) plus your master password, "
            "asked once each time the app starts. Even your own account "
            "can't read them until it's entered."
        ),
    }

    def refresh_protection_ui(self):

        mode = cookie_vault.mode()

        self.protect_combo.setCurrentIndex(self.protect_combo.findData(mode))

        is_pw = mode == "password"

        self.change_pw_btn.setEnabled(is_pw)

        self.lock_btn.setEnabled(is_pw)

        self.lock_btn.setText(
            "UNLOCK..." if cookie_vault.is_locked() else "LOCK NOW"
        )

        text = self.PROTECTION_HELP[mode]

        if cookie_vault.is_locked():
            text += " Currently LOCKED -- downloads run without cookies."

        self.protect_info.setText(text)

    def apply_protection(self):

        new_mode = self.protect_combo.currentData()

        current = cookie_vault.mode()

        if new_mode == current:
            return

        if not ensure_cookie_vault_unlocked(self):
            self.refresh_protection_ui()
            return

        new_password = None

        if new_mode == "plain":

            reply = QMessageBox.warning(
                self, "Turn off protection?",
                "Your saved cookies will be stored as plain text files "
                "again, readable by anything that can open them. "
                "Continue?",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                self.refresh_protection_ui()
                return

        if new_mode == "password":

            dlg = PasswordDialog("new", self)

            if dlg.exec() != QDialog.Accepted:
                self.refresh_protection_ui()
                return

            new_password = dlg.password

        self.run_protection_change(new_mode, new_password, was_plain=current == "plain")

    def run_protection_change(self, new_mode, new_password, was_plain=False):

        QApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            external = change_cookie_protection(new_mode, new_password)
        except (OSError, CookieVaultError, CookieVaultLocked) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Couldn't change protection",
                f"Nothing was changed:\n{e}"
            )
            self.refresh_protection_ui()
            return
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

        signals.log.emit(f"Cookie protection set to: {COOKIE_MODES[new_mode]}")

        if external:
            self.offer_delete_originals(external)

        if was_plain and new_mode != "plain":
            self.offer_clear_browser_sessions()

        self.refresh_protection_ui()

        self.reload_table()

    def offer_delete_originals(self, paths):

        listing = "\n".join(paths)

        reply = QMessageBox.question(
            self, "Delete the plain-text originals?",
            "These cookie files were imported and are now stored "
            "encrypted, but the original plain-text files are still "
            f"where you picked them from:\n\n{listing}\n\n"
            "Delete the originals?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        for path in paths:
            try:
                os.remove(path)
            except OSError as e:
                signals.log.emit(f"Couldn't delete {path}:\n{e}")

    def offer_clear_browser_sessions(self):
        """The sign-in browser keeps its own copy of your sessions in
        browser_profiles (plain, on disk) in plain mode. Protected mode
        doesn't use that, so offer to delete it."""

        if not os.path.isdir(BROWSER_PROFILES_DIR) or not os.listdir(BROWSER_PROFILES_DIR):
            return

        reply = QMessageBox.question(
            self, "Clear the sign-in window's old sessions?",
            "The sign-in window kept its own unencrypted copy of your "
            "sessions from before. Protected mode doesn't use it anymore, "
            "so it's safer to delete it. Your saved (now encrypted) "
            "cookies are kept.\n\nDelete it?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        if _ACTIVE_LOGIN_SESSIONS:

            # A profile from this session still has those files open,
            # so finish the job at the next startup instead.
            cfg = load_config()

            cfg["purge_browser_profiles"] = True

            save_config(cfg)

            QMessageBox.information(
                self, "Will finish on restart",
                "Those files are in use right now; they'll be deleted "
                "the next time the app starts."
            )

            return

        purge_browser_profiles()

    def change_password(self):

        if not ensure_cookie_vault_unlocked(self):
            return

        dlg = PasswordDialog("new", self)

        if dlg.exec() != QDialog.Accepted:
            return

        self.run_protection_change("password", dlg.password)

    def lock_or_unlock(self):

        if cookie_vault.is_locked():
            ensure_cookie_vault_unlocked(self)
        else:
            cookie_vault.lock()
            signals.log.emit("Saved cookies locked.")

        self.refresh_protection_ui()

    def reload_table(self):

        self.refresh_protection_ui()

        site_map = load_site_cookie_map()

        domains = sorted(set(SUPPORTED_DOMAINS) | set(site_map.keys()))

        self.table.setRowCount(0)

        for domain in domains:

            row = self.table.rowCount()

            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(domain))

            cookie_path = site_map.get(domain, "")

            full_path = os.path.abspath(cookie_path) if cookie_path else ""

            display = os.path.basename(full_path) if full_path else ""

            if full_path and not os.path.exists(full_path):
                display += "  (missing)"
            elif full_path and is_encrypted_cookie_file(full_path):
                display += "  (encrypted)"
            elif full_path:
                display += "  (plain text)"

            path_item = QTableWidgetItem(display)

            # Full location on hover.
            path_item.setToolTip(full_path)

            self.table.setItem(row, 1, path_item)

            login_btn = QPushButton("LOGIN & CAPTURE")

            login_btn.clicked.connect(
                lambda checked=False, d=domain: self.login_capture(d)
            )

            self.table.setCellWidget(row, 2, login_btn)

            action_layout_widget = QWidget()

            action_layout = QHBoxLayout(action_layout_widget)

            action_layout.setContentsMargins(0, 0, 0, 0)

            browse_btn = QPushButton("BROWSE...")

            browse_btn.clicked.connect(
                lambda checked=False, d=domain: self.browse_file(d)
            )

            clear_btn = QPushButton("CLEAR")

            clear_btn.clicked.connect(
                lambda checked=False, d=domain: self.clear_site(d)
            )

            action_layout.addWidget(browse_btn)

            action_layout.addWidget(clear_btn)

            self.table.setCellWidget(row, 3, action_layout_widget)

    def add_site(self):

        domain, ok = QInputDialog.getText(
            self,
            "Add Site",
            "Domain (e.g. example.com):"
        )

        if not ok or not domain.strip():
            return

        domain = domain.strip().lower()

        site_map = load_site_cookie_map()

        site_map.setdefault(domain, "")

        save_site_cookie_map(site_map)

        self.reload_table()

    def login_capture(self, domain):

        if not ensure_cookie_vault_unlocked(self):
            self.reload_table()
            return

        start_url = f"https://{domain}/"

        dlg = LoginBrowserDialog(domain, start_url, self)

        dlg.exec()

        self.reload_table()

    def browse_file(self, domain):

        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select cookies.txt for {domain}",
            COOKIES_DIR,
            "Cookie files (*.txt);;All files (*)"
        )

        if not path:
            return

        if not cookie_vault.is_protected():

            # Plain mode: use the file where it is, as before.
            with _cookie_io_lock:

                site_map = load_site_cookie_map()

                site_map[domain] = path

                save_site_cookie_map(site_map)

            set_site_user_agent(domain, None)

            self.reload_table()

            return

        # Protected mode: import an encrypted copy instead.
        try:

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()

        except OSError as e:

            QMessageBox.warning(self, "Couldn't read file", str(e))

            return

        if not any(parse_netscape_cookies(text)):

            QMessageBox.warning(
                self, "Not a cookies.txt file",
                "No cookies were found in that file. It needs to be in "
                "the Netscape cookies.txt format (as exported by browser "
                "extensions like 'Get cookies.txt LOCALLY')."
            )

            return

        if not ensure_cookie_vault_unlocked(self):
            return

        try:
            save_site_cookies(domain, text)
            set_site_user_agent(domain, None)
        except (OSError, CookieVaultLocked, CookieVaultError) as e:
            QMessageBox.warning(self, "Couldn't import cookies", str(e))
            return

        signals.log.emit(f"Imported and encrypted cookies for {domain}.")

        if not is_managed_cookie_path(path):
            self.offer_delete_originals([path])

        self.reload_table()

    def clear_site(self, domain):

        site_map = load_site_cookie_map()

        path = site_map.get(domain)

        if not path:
            return

        note = (
            "The saved cookie file will be deleted."
            if is_managed_cookie_path(path)
            else "The file you picked stays where it is; it just won't "
                 "be used anymore."
        )

        reply = QMessageBox.question(
            self, "Clear saved cookies?",
            f"Forget the saved sign-in for {domain}? {note}",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        delete_site_cookies(domain)

        self.reload_table()

    def open_cookies_folder(self):

        try:
            os.startfile(COOKIES_DIR)
        except OSError as e:

            QMessageBox.warning(
                self,
                "Could not open folder",
                f"Couldn't open the folder automatically:\n{e}\n\n"
                f"Path: {COOKIES_DIR}"
            )


# =========================================================
# SITE SETTINGS DIALOG (per-site concurrency + pacing delay)
# =========================================================

class SiteSettingsDialog(QDialog):

    def __init__(self, parent=None):

        super().__init__(parent)

        self.setWindowTitle("Site Settings")

        self.resize(650, 400)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Limits per site: max concurrent downloads, and a delay "
            "between downloads ('Random' treats it as a max instead of "
            "fixed). 'Delay From' sets what the delay counts from: "
            "Start = when the previous download started, Finish = when "
            "it completed (a real pause between downloads; works best "
            "with Max Concurrent 1). '_default' applies to sites "
            "without their own row."
        )

        info.setWordWrap(True)

        layout.addWidget(info)

        self.table = QTableWidget()

        self.table.setColumnCount(6)

        self.table.setHorizontalHeaderLabels(
            ["Domain", "Max Concurrent", "Delay (sec)",
             "Random (0-max)", "Delay From", "Remove"]
        )

        header = self.table.horizontalHeader()

        header.setSectionResizeMode(0, QHeaderView.Stretch)

        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        layout.addWidget(self.table)

        # =============================================
        # APPLY TO ALL (bulk-set every row at once)
        # =============================================

        bulk_layout = QHBoxLayout()

        bulk_layout.addWidget(QLabel("Apply to all sites:"))

        self.bulk_concurrent_spin = QSpinBox()

        self.bulk_concurrent_spin.setRange(1, 20)

        self.bulk_concurrent_spin.setValue(DEFAULT_SITE_SETTINGS["max_concurrent"])

        bulk_layout.addWidget(QLabel("Max Concurrent:"))

        bulk_layout.addWidget(self.bulk_concurrent_spin)

        self.bulk_delay_spin = QDoubleSpinBox()

        self.bulk_delay_spin.setRange(0, 3600)

        self.bulk_delay_spin.setSingleStep(0.5)

        self.bulk_delay_spin.setValue(DEFAULT_SITE_SETTINGS["delay_seconds"])

        bulk_layout.addWidget(QLabel("Delay (sec):"))

        bulk_layout.addWidget(self.bulk_delay_spin)

        self.bulk_random_checkbox = QCheckBox("Random")

        self.bulk_random_checkbox.setChecked(DEFAULT_SITE_SETTINGS["random_delay"])

        bulk_layout.addWidget(self.bulk_random_checkbox)

        bulk_layout.addWidget(QLabel("From:"))

        self.bulk_delay_from_combo = self.make_delay_from_combo(
            DEFAULT_SITE_SETTINGS["delay_from"]
        )

        bulk_layout.addWidget(self.bulk_delay_from_combo)

        apply_all_btn = QPushButton("APPLY TO ALL ROWS")

        apply_all_btn.clicked.connect(self.apply_bulk_values)

        bulk_layout.addWidget(apply_all_btn)

        layout.addLayout(bulk_layout)

        btn_layout = QHBoxLayout()

        add_btn = QPushButton("ADD SITE")

        add_btn.clicked.connect(self.add_site)

        save_btn = QPushButton("SAVE && CLOSE")

        save_btn.clicked.connect(self.save_and_close)

        btn_layout.addWidget(add_btn)

        btn_layout.addStretch()

        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

        self.reload_table()

    def apply_bulk_values(self):
        """Copies the bulk max-concurrent/delay/random values into
        every already-listed site row (in the table, not yet saved to
        disk until 'Save & Close' is clicked -- same as any other edit
        here)."""

        concurrent_value = self.bulk_concurrent_spin.value()

        delay_value = self.bulk_delay_spin.value()

        random_value = self.bulk_random_checkbox.isChecked()

        delay_from_value = self.bulk_delay_from_combo.currentData()

        for row in range(self.table.rowCount()):

            delay_from_widget = self.table.cellWidget(row, 4)

            if delay_from_widget is not None:
                delay_from_widget.setCurrentIndex(
                    delay_from_widget.findData(delay_from_value)
                )


            concurrent_widget = self.table.cellWidget(row, 1)

            delay_widget = self.table.cellWidget(row, 2)

            random_widget = self.table.cellWidget(row, 3)

            if concurrent_widget is not None:
                concurrent_widget.setValue(concurrent_value)

            if delay_widget is not None:
                delay_widget.setValue(delay_value)

            if random_widget is not None:
                random_widget.setChecked(random_value)

    def reload_table(self):

        cfg = load_config()

        all_sites = cfg.get("site_settings", {})

        domains = sorted(
            {"_default"} | set(SUPPORTED_DOMAINS) | set(all_sites.keys())
        )

        self.table.setRowCount(0)

        for domain in domains:

            row = self.table.rowCount()

            self.table.insertRow(row)

            domain_item = QTableWidgetItem(domain)

            domain_item.setFlags(domain_item.flags() & ~Qt.ItemIsEditable)

            self.table.setItem(row, 0, domain_item)

            settings = get_site_settings(domain) if domain != "_default" else {
                **DEFAULT_SITE_SETTINGS, **all_sites.get("_default", {})
            }

            concurrent_spin = QSpinBox()

            concurrent_spin.setRange(1, 20)

            concurrent_spin.setValue(int(settings.get("max_concurrent", DEFAULT_SITE_SETTINGS["max_concurrent"])))

            self.table.setCellWidget(row, 1, concurrent_spin)

            delay_spin = QDoubleSpinBox()

            delay_spin.setRange(0, 3600)

            delay_spin.setSingleStep(0.5)

            delay_spin.setValue(float(settings.get("delay_seconds", 0)))

            self.table.setCellWidget(row, 2, delay_spin)

            random_checkbox = QCheckBox()

            random_checkbox.setChecked(bool(settings.get("random_delay", False)))

            self.table.setCellWidget(row, 3, random_checkbox)

            self.table.setCellWidget(
                row, 4,
                self.make_delay_from_combo(settings.get("delay_from", "start"))
            )

            remove_btn = QPushButton("REMOVE")

            remove_btn.clicked.connect(
                lambda checked=False, d=domain: self.remove_site(d)
            )

            self.table.setCellWidget(row, 5, remove_btn)

    @staticmethod
    def make_delay_from_combo(value):

        combo = QComboBox()

        for key, label in DELAY_FROM_LABELS.items():
            combo.addItem(label, key)

        combo.setToolTip(
            "Start: delay between download starts.\n"
            "Finish: delay after the previous download completes."
        )

        combo.setCurrentIndex(max(0, combo.findData(value)))

        return combo

    def remove_site(self, domain):

        remove_site_settings(domain)

        self.reload_table()

    def add_site(self):

        domain, ok = QInputDialog.getText(
            self, "Add Site", "Domain (e.g. example.com):"
        )

        if not ok or not domain.strip():
            return

        save_site_settings(
            domain.strip().lower(),
            DEFAULT_SITE_SETTINGS["max_concurrent"],
            DEFAULT_SITE_SETTINGS["delay_seconds"],
            DEFAULT_SITE_SETTINGS["random_delay"],
            DEFAULT_SITE_SETTINGS["delay_from"]
        )

        self.reload_table()

    def save_and_close(self):

        cfg = load_config()

        all_sites = cfg.setdefault("site_settings", {})

        for row in range(self.table.rowCount()):

            domain = self.table.item(row, 0).text()

            concurrent_spin = self.table.cellWidget(row, 1)

            delay_spin = self.table.cellWidget(row, 2)

            random_checkbox = self.table.cellWidget(row, 3)

            delay_from_combo = self.table.cellWidget(row, 4)

            all_sites[domain] = {
                "max_concurrent": concurrent_spin.value(),
                "delay_seconds": delay_spin.value(),
                "random_delay": random_checkbox.isChecked(),
                "delay_from": delay_from_combo.currentData() or "start"
            }

        save_config(cfg)

        self.accept()


# =========================================================
# REDOWNLOAD OPTIONS DIALOG
# =========================================================

class DeleteFileOptionsDialog(QDialog):
    """Confirms deleting a file, with an option to also drop the row
    from the list (unchecked by default -- most of the time you still
    want the row as a record that this was downloaded)."""

    def __init__(self, path, parent=None):

        super().__init__(parent)

        self.setWindowTitle("Delete file?")

        self.resize(420, 160)

        layout = QVBoxLayout(self)

        info = QLabel(
            f"Permanently delete this from disk?\n\n{path}\n\n"
            "This cannot be undone."
        )

        info.setWordWrap(True)

        layout.addWidget(info)

        self.remove_row_checkbox = QCheckBox(
            "Also remove this row from the list"
        )

        self.remove_row_checkbox.setChecked(False)

        self.remove_row_checkbox.setToolTip(
            "Off by default: keeps the row as a record it was downloaded."
        )

        layout.addWidget(self.remove_row_checkbox)

        btn_layout = QHBoxLayout()

        ok_btn = QPushButton("DELETE")

        ok_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("CANCEL")

        cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()

        btn_layout.addWidget(ok_btn)

        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)


class RedownloadOptionsDialog(QDialog):
    """Asks how to handle the previous file/row before redownloading
    something that already completed."""

    def __init__(self, url_original, parent=None):

        super().__init__(parent)

        self.setWindowTitle("Redownload")

        self.resize(420, 200)

        layout = QVBoxLayout(self)

        info = QLabel(
            f"Download this again?\n\n{url_original}"
        )

        info.setWordWrap(True)

        layout.addWidget(info)

        self.delete_file_checkbox = QCheckBox(
            "Delete the previous file first"
        )

        self.delete_file_checkbox.setChecked(True)

        self.delete_file_checkbox.setToolTip(
            "Removes the old file from disk before the new download starts."
        )

        layout.addWidget(self.delete_file_checkbox)

        self.keep_old_row_checkbox = QCheckBox("Keep this record")

        self.keep_old_row_checkbox.setChecked(False)

        self.keep_old_row_checkbox.setToolTip(
            "Adds a new row instead of reusing this one."
        )

        layout.addWidget(self.keep_old_row_checkbox)

        btn_layout = QHBoxLayout()

        ok_btn = QPushButton("REDOWNLOAD")

        ok_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("CANCEL")

        cancel_btn.clicked.connect(self.reject)

        btn_layout.addStretch()

        btn_layout.addWidget(ok_btn)

        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)


class NumericTableWidgetItem(QTableWidgetItem):
    """A table item that sorts by a numeric value stashed in UserRole
    instead of its displayed text, so 'Size'/'Progress' columns sort
    correctly (2 MB < 10 MB) instead of alphabetically."""

    SORT_ROLE = Qt.UserRole + 1

    def __lt__(self, other):

        try:
            return (
                float(self.data(self.SORT_ROLE))
                < float(other.data(self.SORT_ROLE))
            )
        except (TypeError, ValueError):
            return super().__lt__(other)


# =========================================================
# UPDATE CHECKER DIALOG
# =========================================================

class UpdateSignals(QObject):

    checked = Signal(list)

    pip_done = Signal(bool, str)


class UpdateDialog(QDialog):

    COL_SELECT = 0
    COL_NAME = 1
    COL_INSTALLED = 2
    COL_LATEST = 3
    COL_STATUS = 4

    def __init__(self, main_window):

        super().__init__(main_window)

        self.main_window = main_window

        self.setWindowTitle("Check for Updates")

        self.resize(760, 380)

        self.results = []

        self.sig = UpdateSignals(self)

        self.sig.checked.connect(self.show_results)

        self.sig.pip_done.connect(self.on_pip_done)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Compares what's in this built app with the latest releases. "
            "The Python packages are frozen inside the app, so update "
            "them by reinstalling them with pip and rebuilding the app. "
            "FFmpeg is separate: replace it from the download page."
            if running_as_bundle() else
            "Compares what's installed with the latest releases. "
            "gallery-dl and FFmpeg updates are used right away. The "
            "others are loaded while the app runs, so updating them "
            "closes the app, updates, and reopens it automatically."
        )

        info.setWordWrap(True)

        layout.addWidget(info)

        self.table = QTableWidget()

        self.table.setColumnCount(5)

        self.table.setHorizontalHeaderLabels(
            ["", "Component", "Installed", "Latest", "Status"]
        )

        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()

        for col in range(4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.Stretch)

        layout.addWidget(self.table)

        self.status_label = QLabel("")

        self.status_label.setWordWrap(True)

        layout.addWidget(self.status_label)

        self.startup_checkbox = QCheckBox(
            "Check automatically on startup (at most once a day)"
        )

        self.startup_checkbox.setChecked(
            load_config().get("check_updates_on_startup", True)
        )

        self.startup_checkbox.stateChanged.connect(self.save_startup_choice)

        layout.addWidget(self.startup_checkbox)

        btn_layout = QHBoxLayout()

        self.recheck_btn = QPushButton("CHECK AGAIN")

        self.recheck_btn.clicked.connect(self.start_check)

        self.ffmpeg_btn = QPushButton("FFMPEG DOWNLOAD PAGE")

        self.ffmpeg_btn.setToolTip(
            "FFmpeg isn't updated through pip -- download the new "
            "'essentials' build and replace your ffmpeg.exe."
        )

        self.ffmpeg_btn.clicked.connect(
            lambda: webbrowser.open(FFMPEG_BUILDS_PAGE)
        )

        self.update_btn = QPushButton("UPDATE SELECTED")

        self.update_btn.setVisible(not running_as_bundle())

        self.update_btn.clicked.connect(self.update_selected)

        close_btn = QPushButton("CLOSE")

        close_btn.clicked.connect(self.accept)

        btn_layout.addWidget(self.recheck_btn)

        btn_layout.addWidget(self.ffmpeg_btn)

        btn_layout.addStretch()

        btn_layout.addWidget(self.update_btn)

        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        self.start_check()

    def save_startup_choice(self):

        cfg = load_config()

        cfg["check_updates_on_startup"] = self.startup_checkbox.isChecked()

        save_config(cfg)

    def set_busy(self, busy, text=""):

        self.recheck_btn.setEnabled(not busy)

        self.update_btn.setEnabled(not busy)

        self.status_label.setText(text)

    def start_check(self):

        self.set_busy(True, "Checking...")

        self.table.setRowCount(0)

        def work():

            results = check_all_components()

            try:
                self.sig.checked.emit(results)
            except RuntimeError:
                pass  # dialog was closed while checking

        Thread(target=work, daemon=True).start()

    def show_results(self, results):

        self.results = results

        self.table.setRowCount(0)

        outdated = 0

        for item in results:

            row = self.table.rowCount()

            self.table.insertRow(row)

            checkbox = QCheckBox()

            can_update = item["updatable"] and item["outdated"]

            checkbox.setEnabled(can_update)

            checkbox.setChecked(can_update)

            self.table.setCellWidget(row, self.COL_SELECT, checkbox)

            self.table.setItem(row, self.COL_NAME, QTableWidgetItem(item["label"]))

            self.table.setItem(
                row, self.COL_INSTALLED,
                QTableWidgetItem(item["installed"] or "-")
            )

            self.table.setItem(
                row, self.COL_LATEST,
                QTableWidgetItem(item["latest"] or "?")
            )

            status_item = QTableWidgetItem(item["status"])

            status_item.setToolTip(item["status"])

            if item["outdated"]:
                status_item.setForeground(QBrush(QColor("#e5c07b")))
            elif item["status"] == "Up to date":
                status_item.setForeground(QBrush(QColor("#98c379")))

            self.table.setItem(row, self.COL_STATUS, status_item)

            if item["outdated"]:
                outdated += 1

        self.set_busy(
            False,
            f"{outdated} component(s) can be updated." if outdated
            else "Everything is up to date."
        )

    def selected_items(self):

        chosen = []

        for row, item in enumerate(self.results):

            checkbox = self.table.cellWidget(row, self.COL_SELECT)

            if checkbox is not None and checkbox.isChecked():
                chosen.append(item)

        return chosen

    def update_selected(self):

        chosen = self.selected_items()

        if not chosen:

            QMessageBox.information(
                self, "Nothing selected",
                "Tick the components you want to update first."
            )

            return

        packages = [item["dist"] for item in chosen]

        # Nothing the app has loaded -> update right now, no restart.
        if not any(item["in_process"] for item in chosen):

            self.set_busy(True, f"Updating {', '.join(packages)}...")

            def work():

                ok, output = run_pip_upgrade(packages)

                try:
                    self.sig.pip_done.emit(ok, output)
                except RuntimeError:
                    pass

            Thread(target=work, daemon=True).start()

            return

        if self.main_window.request_update_restart(packages, self):
            self.accept()

    def on_pip_done(self, ok, output):

        check_gallery_dl_available(refresh=True)

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        if ok:
            signals.log.emit("Update finished -- the new version is in use now.")
        else:
            signals.log.emit(f"Update failed:\n{output[-800:]}")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        if not ok:
            QMessageBox.warning(self, "Update failed", output[-1500:])

        self.start_check()


# =========================================================
# SIGNALS
# =========================================================

class AppSignals(QObject):

    log = Signal(str)

    queue_update = Signal(int)

    progress = Signal(int, int)

    status = Signal(int, str)

    title = Signal(int, str)

    resolution = Signal(int, str)

    size = Signal(int, str, int)

    user = Signal(int, str)

    output_path = Signal(int, str)

    speed = Signal(int, str)

    login_required = Signal(object)

    notify = Signal(str, str)

    # (original_url, cleaned_url) -- emitted by the background resolver
    # once a short link has been followed, so the dedupe check and
    # queueing happen back on the GUI thread.
    url_resolved = Signal(str, str)


signals = AppSignals()


# =========================================================
# MAIN WINDOW
# =========================================================

class MainWindow(QWidget):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            f"Modern Video Downloader Lite v{APP_VERSION}"
        )

        self.resize(1200, 700)

        # Remember the last folder the user picked, across restarts.
        # Falls back to the Downloads/Videos default if nothing was
        # saved yet, or if the saved folder no longer exists (e.g. a
        # removable/external drive that isn't plugged in right now).
        _cfg = load_config()

        _saved_output_dir = _cfg.get("output_dir")

        if _saved_output_dir and os.path.isdir(_saved_output_dir):
            self.output_dir = _saved_output_dir
        else:
            self.output_dir = DEFAULT_OUTPUT_DIR

        self.last_clip = ""

        self.download_id = 0

        # row_id -> table row, so finding a row doesn't mean scanning
        # the whole table (that scan ran for every progress update of
        # every download, which got slow with hundreds of rows).
        self._row_index_cache = {}

        self._row_index_count = -1

        # Links waiting to be added in small batches (see enqueue_bulk).
        self._pending_add_urls = []

        self._bulk_adding = False

        # Short links are followed by a few background threads at most,
        # instead of one new thread per pasted link.
        self._resolver_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="link-resolver"
        )

        # Tracks which row the "follow active download" autoscroll
        # should keep visible -- with sorting enabled, the newest item
        # isn't reliably the last row anymore, so autoscroll needs to
        # follow a specific row_id rather than just jumping to the
        # bottom.
        self.active_follow_row_id = None

        # Per-site queues + worker pools, instead of one shared queue,
        # so concurrency and pacing can be limited per site.
        self.site_queues = {}

        self.site_worker_counts = defaultdict(int)

        self.site_dispatch_lock = defaultdict(Lock)

        self.site_last_dispatch = defaultdict(float)

        # When the most recent download for each site finished (for the
        # "Delay From: Finish" setting).
        self.site_last_finish = defaultdict(float)

        self.queue_setup_lock = Lock()

        # row_id -> {url_original, url_clean, output_path, domain}
        self.row_records = {}

        load_completed_urls()

        self.setup_ui()

        self.setup_tray()

        self.connect_signals()

        self.add_log(f"Download log: {LOG_FILE}")

        self.add_log(f"Failed URLs list: {FAILED_FILE}")

        self.add_log(f"Cookies folder: {COOKIES_DIR}")

        self.check_ffmpeg()

        self.check_gallery_dl()

        Thread(
            target=has_dedicated_extractor,
            args=("https://example.invalid/",),
            daemon=True
        ).start()

        # "Update & Restart" support: report how the update went,
        # re-add anything that was still queued, and do the daily
        # background check.
        self.pending_update_packages = None

        self.update_wait_timer = QTimer(self)

        self.update_wait_timer.setInterval(3000)

        self.update_wait_timer.timeout.connect(self.check_scheduled_update)

        self.report_update_result()

        cfg = load_config()

        if cfg.pop("purge_browser_profiles", False):

            save_config(cfg)

            purge_browser_profiles()

        # Master-password mode: ask once, right after the window
        # appears. Skipping is fine -- downloads just run without the
        # saved cookies until you unlock them in Manage Cookies.
        if cookie_vault.is_locked():
            QTimer.singleShot(300, self.prompt_cookie_unlock)

        self.resume_urls_after_update()

        self.maybe_check_updates_on_startup()

    # =====================================================
    # UI
    # =====================================================

    def setup_ui(self):

        self.layout = QVBoxLayout()

        # =============================================
        # URL
        # =============================================

        top_layout = QHBoxLayout()

        self.url_input = QPlainTextEdit()

        self.url_input.setPlaceholderText(
            "Paste one or more URLs here (one per line)..."
        )

        self.url_input.setFixedHeight(64)

        buttons_layout = QVBoxLayout()

        self.download_btn = QPushButton(
            "ADD TO QUEUE"
        )

        self.download_btn.clicked.connect(
            self.manual_download
        )

        self.import_btn = QPushButton(
            "IMPORT FROM FILE"
        )

        self.import_btn.clicked.connect(
            self.import_from_file
        )

        buttons_layout.addWidget(
            self.download_btn
        )

        buttons_layout.addWidget(
            self.import_btn
        )

        top_layout.addWidget(
            self.url_input
        )

        top_layout.addLayout(
            buttons_layout
        )

        self.layout.addLayout(
            top_layout
        )

        # =============================================
        # FOLDER + COOKIES
        # =============================================

        folder_layout = QHBoxLayout()

        self.folder_label = QLabel(
            self.output_dir
        )

        self.open_download_folder_btn = QPushButton(
            "OPEN FOLDER"
        )

        self.open_download_folder_btn.setToolTip(
            "Opens the download folder in your file manager."
        )

        self.open_download_folder_btn.clicked.connect(
            self.open_download_folder
        )

        self.folder_btn = QPushButton(
            "SELECT FOLDER"
        )

        self.folder_btn.clicked.connect(
            self.select_folder
        )

        folder_layout.addWidget(
            self.folder_label, 1
        )

        folder_layout.addWidget(
            self.open_download_folder_btn
        )

        folder_layout.addWidget(
            self.folder_btn
        )

        self.layout.addLayout(
            folder_layout
        )

        tools_layout = QHBoxLayout()

        self.cookies_btn = QPushButton(
            "MANAGE COOKIES"
        )

        self.cookies_btn.clicked.connect(
            self.open_cookie_manager
        )

        self.ffmpeg_btn = QPushButton(
            "SET FFMPEG PATH"
        )

        self.ffmpeg_btn.clicked.connect(
            self.set_ffmpeg_path
        )

        self.site_settings_btn = QPushButton(
            "SITE SETTINGS"
        )

        self.site_settings_btn.clicked.connect(
            self.open_site_settings
        )

        self.open_logs_btn = QPushButton(
            "OPEN LOGS FOLDER"
        )

        self.open_logs_btn.clicked.connect(
            self.open_logs_folder
        )

        tools_layout.addWidget(
            self.cookies_btn
        )

        tools_layout.addWidget(
            self.ffmpeg_btn
        )

        tools_layout.addWidget(
            self.site_settings_btn
        )

        tools_layout.addWidget(
            self.open_logs_btn
        )

        self.updates_btn = QPushButton(
            "CHECK FOR UPDATES"
        )

        self.updates_btn.clicked.connect(
            self.open_update_dialog
        )

        tools_layout.addWidget(
            self.updates_btn
        )

        self.layout.addLayout(
            tools_layout
        )

        # =============================================
        # AUTO CLIPBOARD
        # =============================================

        checkbox_row = QHBoxLayout()

        self.auto_clipboard = QCheckBox(
            "Auto Add"
        )

        self.auto_clipboard.setToolTip(
            "Watches the clipboard and queues any supported link copied."
        )

        self.auto_clipboard.stateChanged.connect(
            self.toggle_clipboard
        )

        checkbox_row.addWidget(
            self.auto_clipboard
        )

        self.auto_clipboard.setChecked(True)

        self.autoscroll_checkbox = QCheckBox(
            "Auto-scroll"
        )

        self.autoscroll_checkbox.setToolTip(
            "Keeps the active download's row visible as things update."
        )

        self.autoscroll_checkbox.setChecked(True)

        self.autoscroll_checkbox.stateChanged.connect(
            self.on_autoscroll_toggled
        )

        checkbox_row.addWidget(
            self.autoscroll_checkbox
        )

        checkbox_row.addStretch()

        checkbox_row.addWidget(QLabel("Bulk:"))

        self.bulk_action_combo = QComboBox()

        self.bulk_action_combo.addItems(
            ["Remove Files", "Remove Record", "Redownload", "Retry"]
        )

        checkbox_row.addWidget(self.bulk_action_combo)

        self.bulk_action_btn = QPushButton("APPLY")

        self.bulk_action_btn.setToolTip(
            "Runs the action on every checked row it applies to."
        )

        self.bulk_action_btn.clicked.connect(self.run_bulk_action)

        checkbox_row.addWidget(self.bulk_action_btn)

        self.clear_list_combo = QComboBox()

        self.clear_list_combo.addItems(
            ["All", "Completed", "Failed", "Skipped", "Opened in Browser"]
        )

        checkbox_row.addWidget(QLabel("Clear:"))

        checkbox_row.addWidget(self.clear_list_combo)

        self.clear_list_btn = QPushButton("CLEAR LIST")

        self.clear_list_btn.setToolTip(
            "Removes matching rows below (not files or the log)."
        )

        self.clear_list_btn.clicked.connect(self.clear_download_list)

        checkbox_row.addWidget(self.clear_list_btn)

        self.layout.addLayout(checkbox_row)

        # =============================================
        # TABLE SEARCH / FILTER
        # =============================================

        table_search_layout = QHBoxLayout()

        self.table_search_input = QLineEdit()

        self.table_search_input.setPlaceholderText(
            "Search/filter the table..."
        )

        self.table_search_input.textChanged.connect(
            self.apply_table_filter
        )

        self.table_search_column = QComboBox()

        self.table_search_column.addItems(
            ["All Columns", "Status", "Title", "Resolution", "Size",
             "User", "Output File", "URL"]
        )

        self.table_search_column.currentIndexChanged.connect(
            self.apply_table_filter
        )

        table_search_layout.addWidget(self.table_search_input)

        table_search_layout.addWidget(self.table_search_column)

        self.table_search_clear_btn = QPushButton("CLEAR")

        self.table_search_clear_btn.setToolTip(
            "Clears the search box and shows every row again."
        )

        self.table_search_clear_btn.clicked.connect(
            lambda: self.table_search_input.setText("")
        )

        table_search_layout.addWidget(self.table_search_clear_btn)

        self.restore_order_btn = QPushButton("RESTORE ORDER")

        self.restore_order_btn.setToolTip(
            "Sort the table back to the order downloads were added in."
        )

        self.restore_order_btn.clicked.connect(self.restore_original_order)

        table_search_layout.addWidget(self.restore_order_btn)

        self.layout.addLayout(table_search_layout)

        # =============================================
        # TABLE
        # =============================================

        self.table = QTableWidget()

        self.table.setColumnCount(12)

        self.table.setHorizontalHeaderLabels([
            "#",
            "Status",
            "Title",
            "Progress",
            "Resolution",
            "Size",
            "User",
            "Output File",
            "URL",
            "Options",
            "Sel",
            "Speed"
        ])

        self.table.setColumnHidden(self.COL_ORDER, True)

        header = self.table.horizontalHeader()

        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(10, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SPEED, QHeaderView.ResizeToContents)

        # Move the "Sel" column (bulk-selection checkboxes) to just
        # after the hidden "#" column, visually -- this only changes
        # display order; every setItem/cellWidget call elsewhere still
        # uses the original logical column index (COL_SELECT etc.),
        # so nothing else needs to change.
        header.moveSection(self.COL_SELECT, 1)

        # Speed sits right after Size on screen.
        header.moveSection(
            header.visualIndex(self.COL_SPEED),
            header.visualIndex(self.COL_SIZE) + 1
        )

        # A real checkbox embedded in the header itself (instead of
        # a text label) to select/deselect every row's checkbox at
        # once. QHeaderView has no native slot for this, so it's a
        # small child widget kept positioned over that section.
        self.table.setHorizontalHeaderItem(self.COL_SELECT, QTableWidgetItem(""))

        self.select_all_checkbox = QCheckBox(header)

        self.select_all_checkbox.setToolTip("Select/deselect every row")

        self.select_all_checkbox.stateChanged.connect(self.on_select_all_toggled)

        header.sectionResized.connect(lambda *a: self.position_select_all_checkbox())

        header.sectionMoved.connect(lambda *a: self.position_select_all_checkbox())

        self.position_select_all_checkbox()

        self.table.setSortingEnabled(True)

        # QTableWidget only moves QTableWidgetItem DATA when you sort
        # by clicking a header -- cell WIDGETS (the progress bar,
        # the Retry button) are a separate storage and don't
        # automatically follow their row. Without this, sorting by
        # anything other than the original "#" order can leave a
        # row's progress bar / Retry button visually attached to a
        # different download than the one it's actually showing.
        header.sortIndicatorChanged.connect(self.reattach_row_widgets)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)

        self.table.customContextMenuRequested.connect(
            self.show_table_context_menu
        )

        self.table.itemSelectionChanged.connect(
            self.show_selected_cell_detail
        )

        self.layout.addWidget(
            self.table, 2
        )

        self.cell_detail_box = QLabel()

        self.cell_detail_box.setTextFormat(Qt.RichText)

        self.cell_detail_box.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.cell_detail_box.setWordWrap(True)

        # The native frame style barely showed up against the dark
        # theme -- an explicit colored border is much clearer.
        self.cell_detail_box.setStyleSheet(
            "border: 2px solid #4a90d9; border-radius: 3px; padding: 4px;"
        )

        self.cell_detail_box.setText(
            "Select a cell above to see its full text here..."
        )

        self.layout.addWidget(
            self.cell_detail_box
        )

        # =============================================
        # LOG (+ search)
        # =============================================

        log_search_layout = QHBoxLayout()

        self.log_search_input = QLineEdit()

        self.log_search_input.setPlaceholderText(
            "Search the log..."
        )

        self.log_search_input.returnPressed.connect(
            self.find_in_log
        )

        self.log_search_input.textChanged.connect(
            self.refresh_log_highlighting
        )

        self.log_search_btn = QPushButton("FIND NEXT")

        self.log_search_btn.clicked.connect(self.find_in_log)

        self.log_search_clear_btn = QPushButton("CLEAR SEARCH")

        self.log_search_clear_btn.setToolTip(
            "Clears the search box and removes the highlighting."
        )

        self.log_search_clear_btn.clicked.connect(
            lambda: self.log_search_input.setText("")
        )

        self.clear_log_btn = QPushButton("CLEAR LOG")

        self.clear_log_btn.setToolTip(
            "Clears the log on screen only -- files on disk untouched."
        )

        self.clear_log_btn.clicked.connect(self.clear_log_display)

        log_search_layout.addWidget(self.log_search_input)

        log_search_layout.addWidget(self.log_search_btn)

        log_search_layout.addWidget(self.log_search_clear_btn)

        log_search_layout.addWidget(self.clear_log_btn)

        self.layout.addLayout(log_search_layout)

        self.log_box = QTextEdit()

        self.log_box.setReadOnly(True)

        # Raw (color, text) pairs for every log line, kept separately
        # from the rendered HTML so the log can be fully re-rendered
        # with search-term highlighting applied whenever the search
        # box changes, without losing anything already logged.
        self.log_lines = []

        self.layout.addWidget(
            self.log_box, 1
        )

        self.setLayout(
            self.layout
        )

    def open_site_settings(self):

        dlg = SiteSettingsDialog(self)

        dlg.exec()

    def open_download_folder(self):

        try:
            reveal_in_file_manager(self.output_dir)
        except OSError as e:
            QMessageBox.warning(
                self,
                "Could not open folder",
                f"Couldn't open the folder automatically:\n{e}\n\n"
                f"Path: {self.output_dir}"
            )

    def open_logs_folder(self):

        try:
            reveal_in_file_manager(APP_DIR)
        except OSError as e:
            QMessageBox.warning(
                self,
                "Could not open folder",
                f"Couldn't open the folder automatically:\n{e}\n\n"
                f"Path: {APP_DIR}"
            )

    def open_cookie_manager(self):

        dlg = CookieManagerDialog(self)

        dlg.exec()

    def check_ffmpeg(self):

        path = find_ffmpeg()

        if path:

            self.add_log(f"ffmpeg found: {path}")

        else:

            self.add_log(
                "WARNING: ffmpeg not found. Video downloads that need "
                "audio+video merging will fail until it's installed."
            )

            QMessageBox.warning(
                self,
                "ffmpeg not found",
                "ffmpeg wasn't found on PATH, so merging video+audio will "
                "fail.\n\n"
                "1. Download a build from "
                "https://www.gyan.dev/ffmpeg/builds/ "
                "(the 'essentials' zip is enough).\n"
                "2. Extract it anywhere.\n"
                "3. Click 'SET FFMPEG PATH' here and pick the "
                "ffmpeg.exe inside its 'bin' folder.\n\n"
                "(Or add that bin folder to your Windows PATH and "
                "restart this app instead.)"
            )

    def check_gallery_dl(self):

        if check_gallery_dl_available():

            self.add_log("gallery-dl found (used as an image/gallery fallback).")

        else:

            self.add_log(
                "gallery-dl not found -- Instagram/TikTok photo posts "
                "that yt-dlp can't handle will fall back to a lower-"
                "quality method. Run: pip install gallery-dl"
            )

    def set_ffmpeg_path(self):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select ffmpeg executable",
            "",
            "ffmpeg executable (ffmpeg.exe ffmpeg);;All files (*)"
        )

        if not path:
            return

        cfg = load_config()

        cfg["ffmpeg_path"] = path

        save_config(cfg)

        self.add_log(f"ffmpeg path set to: {path}")


    # =====================================================
    # UPDATES
    # =====================================================

    def prompt_cookie_unlock(self):

        if ensure_cookie_vault_unlocked(self):

            if cookie_vault.mode() == "password":
                self.add_log("Saved cookies unlocked.")

        else:

            self.add_log(
                "Saved cookies are locked -- downloads will run without "
                "them. Unlock them any time in MANAGE COOKIES."
            )

    def open_update_dialog(self):

        dlg = UpdateDialog(self)

        dlg.exec()

    def maybe_check_updates_on_startup(self):
        """Quiet once-a-day check: only writes to the Log, no popup."""

        cfg = load_config()

        if not cfg.get("check_updates_on_startup", True):
            return

        if time.time() - cfg.get("last_update_check", 0) < UPDATE_CHECK_INTERVAL_SECONDS:
            return

        cfg["last_update_check"] = time.time()

        save_config(cfg)

        def work():

            outdated = [
                item for item in check_all_components() if item["outdated"]
            ]

            if not outdated:
                return

            parts = [
                f"{item['label']} {item['installed'] or '(missing)'} -> "
                f"{item['latest'] or '?'}"
                for item in outdated
            ]

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            how = (
                "Rebuild the app to include the new Python packages "
                "(FFmpeg is updated separately)."
                if running_as_bundle()
                else "Use CHECK FOR UPDATES to install them."
            )

            signals.log.emit(
                "Note: updates available -- " + "; ".join(parts) + "\n" + how
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        Thread(target=work, daemon=True).start()

    def unfinished_row_urls(self):
        """Original URLs of rows still waiting or downloading."""

        urls = []

        for row_id, record in self.row_records.items():

            status = self._row_status_text(row_id)

            if status is not None and not is_terminal_status(status):
                urls.append(record["url_original"])

        return urls

    def request_update_restart(self, packages, parent):
        """Asks how to handle unfinished downloads, then either
        restarts now or schedules it. Returns True if an update was
        started or scheduled."""

        unfinished = self.unfinished_row_urls()

        names = ", ".join(packages)

        if not unfinished:

            reply = QMessageBox.question(
                parent,
                "Update & restart",
                f"Updating {names} needs a restart. The app will close, "
                "update, and reopen by itself -- usually within a "
                "minute. Continue?",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                return False

            self.perform_update_restart(packages)

            return True

        box = QMessageBox(parent)

        box.setWindowTitle("Downloads in progress")

        box.setIcon(QMessageBox.Question)

        box.setText(
            f"{len(unfinished)} download(s) are still waiting or in "
            f"progress. Updating {names} needs a restart."
        )

        box.setInformativeText(
            "When downloads finish: the app updates and restarts by "
            "itself once the list is idle.\n\n"
            "Restart now: unfinished links are added back automatically "
            "after the restart, but ones already downloading start over "
            "from the beginning."
        )

        wait_btn = box.addButton("WHEN DOWNLOADS FINISH", QMessageBox.AcceptRole)

        now_btn = box.addButton("RESTART NOW", QMessageBox.DestructiveRole)

        box.addButton("CANCEL", QMessageBox.RejectRole)

        box.setDefaultButton(wait_btn)

        box.exec()

        clicked = box.clickedButton()

        if clicked == now_btn:
            self.perform_update_restart(packages)
            return True

        if clicked == wait_btn:

            self.pending_update_packages = list(packages)

            self.update_wait_timer.start()

            self.updates_btn.setText("UPDATE PENDING...")

            self.updates_btn.setToolTip(
                "Will update and restart once all downloads finish."
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(
                f"Update scheduled: {names} -- the app will restart "
                "once all downloads finish."
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            return True

        return False

    def check_scheduled_update(self):

        if not self.pending_update_packages:
            self.update_wait_timer.stop()
            return

        if self.unfinished_row_urls() or self.total_queue_size():
            return

        self.update_wait_timer.stop()

        self.perform_update_restart(self.pending_update_packages)

    def perform_update_restart(self, packages):

        unfinished = self.unfinished_row_urls()

        try:

            if unfinished:
                with open(RESUME_URLS_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(unfinished) + "\n")

            launch_update_helper(packages)

        except OSError as e:

            try:
                os.remove(RESUME_URLS_FILE)
            except OSError:
                pass

            QMessageBox.warning(
                self, "Couldn't start the update",
                f"The update helper couldn't be started:\n{e}"
            )

            return

        self.tray.hide()

        QApplication.quit()

    def report_update_result(self):
        """Runs at startup: logs what the last Update & Restart did."""

        if not os.path.exists(UPDATE_RESULT_FILE):
            return

        try:
            with open(UPDATE_RESULT_FILE, "r", encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, json.JSONDecodeError):
            result = None

        try:
            os.remove(UPDATE_RESULT_FILE)
        except OSError:
            pass

        if not result:
            return

        names = ", ".join(result.get("packages", []))

        self.add_log(DOWNLOAD_LOG_SEPARATOR)

        if result.get("ok"):

            self.add_log(f"Updated after restart: {names}")

        else:

            self.add_log(
                f"Update failed for {names}:\n"
                f"{result.get('output', '')[-800:]}"
            )

            QMessageBox.warning(
                self, "Update failed",
                f"Updating {names} didn't succeed -- the app is still "
                "running the previous version.\n\n"
                f"{result.get('output', '')[-1500:]}"
            )

        self.add_log(DOWNLOAD_LOG_SEPARATOR)

    def resume_urls_after_update(self):

        if not os.path.exists(RESUME_URLS_FILE):
            return

        try:
            with open(RESUME_URLS_FILE, "r", encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip()]
        except OSError:
            urls = []

        try:
            os.remove(RESUME_URLS_FILE)
        except OSError:
            pass

        if urls:

            self.add_log(
                f"Re-adding {len(urls)} unfinished download(s) from "
                "before the update."
            )

            for url in urls:
                self.add_download(url)

    # =====================================================
    # TRAY
    # =====================================================

    def setup_tray(self):

        self.tray = QSystemTrayIcon(self)

        menu = QMenu()

        show_action = QAction(
            "Show",
            self
        )

        quit_action = QAction(
            "Quit",
            self
        )

        show_action.triggered.connect(
            self.show
        )

        quit_action.triggered.connect(
            QApplication.quit
        )

        menu.addAction(show_action)

        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

        self.tray.show()

    # =====================================================
    # SIGNALS
    # =====================================================

    def connect_signals(self):

        signals.log.connect(
            self.add_log
        )

        signals.queue_update.connect(
            self.update_queue
        )

        signals.progress.connect(
            self.update_progress
        )

        signals.speed.connect(
            self.update_speed
        )

        signals.status.connect(
            self.update_status
        )

        signals.title.connect(
            self.update_title
        )

        signals.resolution.connect(
            self.update_resolution
        )

        signals.size.connect(
            self.update_size
        )

        signals.user.connect(
            self.update_user
        )

        signals.output_path.connect(
            self.update_output_path
        )

        signals.login_required.connect(
            self.handle_login_request
        )

        signals.notify.connect(
            self.tray.showMessage
        )

        signals.url_resolved.connect(
            self.enqueue_resolved_url
        )

    def handle_login_request(self, request):
        """Runs on the MAIN thread. Opens the real browser window,
        waits for the user to finish, then releases the worker thread
        that's blocked on request.event."""

        if not ensure_cookie_vault_unlocked(self):

            request.success = False

            request.event.set()

            return

        QMessageBox.information(
            self,
            "Sign-in required",
            f"{request.domain} needs you to log in or verify before "
            "this download can continue. A browser window will open \u2014 "
            "finish the check there, then click 'Save Session & Close'."
        )

        dlg = LoginBrowserDialog(request.domain, request.url, self)

        dlg.exec()

        request.success = dlg.saved

        request.event.set()

    # =====================================================
    # LOG
    # =====================================================

    def add_log(self, msg):

        scrollbar = self.log_box.verticalScrollBar()

        # "Smart" autoscroll: only follow new lines if the user was
        # already at (or very near) the bottom. If they've scrolled up
        # to read something, don't yank them back down on every line.
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        color = classify_log_line(msg)

        # Collapse a message's "\n"-separated parts onto a single
        # displayed line, joined with "|", instead of spanning
        # several lines in the log box -- keeps one download's
        # outcome scannable as a single line instead of three.
        parts = [p.strip() for p in msg.split("\n") if p.strip()]

        single_line = " | ".join(parts) if parts else msg

        # Timestamp every line except the plain visual separators --
        # a timestamp on a row of dashes would just be noise.
        if single_line != DOWNLOAD_LOG_SEPARATOR:

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            single_line = f"[{timestamp}] {single_line}"

        self.log_lines.append((color, single_line))

        self.log_box.append(self.render_log_line(color, single_line))

        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def render_log_line(self, color, text):
        """Escapes and colors one line, highlighting the current log
        search term (if any) within it. Always wraps in an explicit
        color -- the app's actual current text color for the default
        case, not the CSS keyword 'inherit' (which resolved to plain
        black regardless of the dark theme) -- rather than leaving
        color unset. QTextEdit.append() can otherwise carry the
        previous line's color forward into a line that doesn't
        specify its own, which is what caused plain lines to show up
        red after a red (failure) line had appeared earlier."""

        escaped = html.escape(text)

        term = self.log_search_input.text().strip()

        if term:
            escaped = self.highlight_html(escaped, term)

        default_color = self.log_box.palette().text().color().name()

        return f'<span style="color:{color or default_color};">{escaped}</span>'

    def highlight_html(self, escaped_text, term):
        """Wraps every case-insensitive match of term in a highlight
        span. Operates on already-HTML-escaped text, so the term is
        escaped the same way before matching against it."""

        escaped_term = html.escape(term)

        if not escaped_term:
            return escaped_text

        pattern = re.compile(re.escape(escaped_term), re.IGNORECASE)

        return pattern.sub(
            lambda m: (
                f'<span style="background-color:{SEARCH_HIGHLIGHT_COLOR};'
                f'color:#000;">{m.group(0)}</span>'
            ),
            escaped_text
        )

    def refresh_log_highlighting(self):
        """Fully re-renders the log from the stored raw lines, so
        typing in the log search box highlights every existing match
        immediately instead of only the next one 'Find Next' jumps to."""

        scrollbar = self.log_box.verticalScrollBar()

        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        self.log_box.clear()

        for color, text in self.log_lines:
            self.log_box.append(self.render_log_line(color, text))

        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def find_in_log(self):

        text = self.log_search_input.text().strip()

        if not text:
            return

        found = self.log_box.find(text)

        if not found:

            # Wrap around: no more matches searching forward from here
            # -- restart from the very top of the log and search
            # again, so "Find Next" cycles back to the first match
            # instead of just stopping once it hits the last one.
            cursor = self.log_box.textCursor()

            cursor.movePosition(QTextCursor.MoveOperation.Start)

            self.log_box.setTextCursor(cursor)

            self.log_box.find(text)

    def clear_log_display(self):
        """Clears the on-screen log box only -- the JSONL log file and
        failed_downloads.txt on disk are completely untouched."""

        self.log_box.clear()

        self.log_lines = []

    # =====================================================
    # TABLE
    # =====================================================

    ROW_ID_ROLE = Qt.UserRole + 100

    COL_ORDER = 0
    COL_STATUS = 1
    COL_TITLE = 2
    COL_PROGRESS = 3
    COL_RESOLUTION = 4
    COL_SIZE = 5
    COL_USER = 6
    COL_OUTPUT = 7
    COL_URL = 8
    COL_RETRY = 9
    COL_SELECT = 10
    # Added last (logical index) and moved next to Size visually, so no
    # other column index had to change.
    COL_SPEED = 11

    # Cell values that mean "not known yet" (waiting) vs "not provided
    # by the site" (once downloading or done).
    UNKNOWN_TEXT = "?"
    NOT_AVAILABLE_TEXT = "NA"

    def add_table_row(self, url):

        self.download_id += 1

        row_id = self.download_id

        self.row_records[row_id] = {
            "url_original": url,
            "url_clean": url,
            "output_path": None,
            "domain": None
        }

        row = self.table.rowCount()

        self.table.setSortingEnabled(False)

        self.table.insertRow(row)

        order_item = NumericTableWidgetItem(str(row_id))

        order_item.setData(NumericTableWidgetItem.SORT_ROLE, row_id)

        self.table.setItem(row, self.COL_ORDER, order_item)

        status_item = QTableWidgetItem("Waiting")

        status_item.setData(self.ROW_ID_ROLE, row_id)

        waiting_icon = self.get_status_icon("Waiting")

        if waiting_icon is not None:
            status_item.setIcon(waiting_icon)

        self.table.setItem(row, self.COL_STATUS, status_item)

        title_item = QTableWidgetItem("...")

        title_item.setToolTip("...")

        self.table.setItem(row, self.COL_TITLE, title_item)

        progress_item = NumericTableWidgetItem("")

        progress_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)

        self.table.setItem(row, self.COL_PROGRESS, progress_item)

        progress_bar = QProgressBar()

        progress_bar.setValue(0)

        self.table.setCellWidget(row, self.COL_PROGRESS, progress_bar)

        self.row_records[row_id]["progress_bar"] = progress_bar

        self.table.setItem(
            row, self.COL_RESOLUTION, QTableWidgetItem("?")
        )

        size_item = NumericTableWidgetItem("?")

        size_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)

        self.table.setItem(row, self.COL_SIZE, size_item)

        speed_item = NumericTableWidgetItem(self.UNKNOWN_TEXT)

        speed_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)

        self.table.setItem(row, self.COL_SPEED, speed_item)

        self.table.setItem(
            row, self.COL_USER, QTableWidgetItem("?")
        )

        self.table.setItem(
            row, self.COL_OUTPUT, QTableWidgetItem("?")
        )

        url_item = QTableWidgetItem(url)

        url_item.setToolTip(url)

        self.table.setItem(row, self.COL_URL, url_item)

        retry_btn = QPushButton("RETRY")

        retry_btn.setEnabled(False)

        retry_btn.setToolTip("Enabled once this download fails.")

        retry_btn.clicked.connect(
            lambda checked=False, rid=row_id: self.handle_retry_or_redownload(rid)
        )

        self.table.setCellWidget(row, self.COL_RETRY, retry_btn)

        self.row_records[row_id]["retry_btn"] = retry_btn

        select_checkbox = QCheckBox()

        self.table.setCellWidget(row, self.COL_SELECT, select_checkbox)

        self.row_records[row_id]["select_checkbox"] = select_checkbox

        # If nothing is currently being followed, this brand-new
        # ("Waiting") row becomes the one to follow.
        if (
            self.autoscroll_checkbox.isChecked()
            and self.active_follow_row_id is None
        ):
            self.active_follow_row_id = row_id

        # During a batch add, sorting/scrolling happen once at the end
        # of the batch instead of after every single row.
        if self._bulk_adding:
            return row_id

        self.finish_row_changes()

        return row_id

    def finish_row_changes(self):
        """Re-enables sorting after rows were added. Turning sorting back
        on re-sorts the table, and cell widgets (progress bar, buttons,
        checkbox) don't move with their row on their own, so they're
        re-attached afterwards."""

        self.table.setSortingEnabled(True)

        self.reattach_row_widgets()

        self.scroll_to_followed_row()

    def find_row_index(self, row_id):
        """Table row currently showing row_id, or None. Uses a cached
        map that's verified on every use and rebuilt only when rows were
        added, removed or re-sorted -- instead of scanning every row on
        every call."""

        row = self._row_index_cache.get(row_id)

        if row is not None and row < self.table.rowCount():

            item = self.table.item(row, self.COL_STATUS)

            if item is not None and item.data(self.ROW_ID_ROLE) == row_id:
                return row

        # Nothing changed since the last rebuild and this id wasn't in
        # it: the row doesn't exist (e.g. it was removed).
        if row is None and self.table.rowCount() == self._row_index_count:
            return None

        self._rebuild_row_index()

        return self._row_index_cache.get(row_id)

    def _rebuild_row_index(self):

        cache = {}

        for row in range(self.table.rowCount()):

            item = self.table.item(row, self.COL_STATUS)

            if item is not None:
                cache[item.data(self.ROW_ID_ROLE)] = row

        self._row_index_cache = cache

        self._row_index_count = self.table.rowCount()

    def reattach_row_widgets(self, *args):
        """Re-associates each row's progress bar, Retry/Options button
        and select checkbox (the actual same widget objects, so their
        current value/enabled-state/checked-state is preserved) with
        wherever that row_id physically ended up after a sort. See the
        comment where this is connected for why it's needed."""

        for row_id, record in self.row_records.items():

            row = self.find_row_index(row_id)

            if row is None:
                continue

            progress_bar = record.get("progress_bar")

            if progress_bar is not None:
                self.table.setCellWidget(row, self.COL_PROGRESS, progress_bar)

            retry_btn = record.get("retry_btn")

            if retry_btn is not None:
                self.table.setCellWidget(row, self.COL_RETRY, retry_btn)

            select_checkbox = record.get("select_checkbox")

            if select_checkbox is not None:
                self.table.setCellWidget(row, self.COL_SELECT, select_checkbox)

    def find_next_active_row_id(self):
        """The earliest-added row (by insertion/row_id order, not
        table position) that isn't in a terminal state -- used to pick
        what to follow next once the currently-followed row finishes."""

        for row_id in self.row_records:

            row = self.find_row_index(row_id)

            if row is None:
                continue

            item = self.table.item(row, self.COL_STATUS)

            if item is not None and not is_terminal_status(item.text()):
                return row_id

        return None

    def scroll_to_followed_row(self):

        if not self.autoscroll_checkbox.isChecked():
            return

        if self.active_follow_row_id is None:
            return

        row = self.find_row_index(self.active_follow_row_id)

        if row is None:
            return

        item = self.table.item(row, self.COL_STATUS)

        if item is not None:
            self.table.scrollToItem(item)

    def on_autoscroll_toggled(self):

        if self.autoscroll_checkbox.isChecked():

            if self.active_follow_row_id is None:
                self.active_follow_row_id = self.find_next_active_row_id()

            self.scroll_to_followed_row()

    def update_progress(self, row_id, value):

        row = self.find_row_index(row_id)

        if row is None:
            return

        widget = self.table.cellWidget(row, self.COL_PROGRESS)

        if widget:
            widget.setValue(value)

        item = self.table.item(row, self.COL_PROGRESS)

        if item:
            item.setData(NumericTableWidgetItem.SORT_ROLE, value)

        if row_id == self.active_follow_row_id:
            self.scroll_to_followed_row()

    def update_speed(self, row_id, speed_text):
        """Current download speed. The last reading simply stays in the
        cell once the download finishes."""

        if row_id in self.row_records:
            self.row_records[row_id]["last_speed"] = speed_text

        row = self.find_row_index(row_id)

        if row is None:
            return

        item = NumericTableWidgetItem(speed_text)

        item.setData(
            NumericTableWidgetItem.SORT_ROLE, parse_speed_bytes(speed_text)
        )

        self.table.setItem(row, self.COL_SPEED, item)

    NA_COLUMNS = ("COL_RESOLUTION", "COL_SIZE", "COL_SPEED", "COL_USER", "COL_OUTPUT")

    def fill_not_available(self, row):
        """Once a download is running or done, anything the site never
        provided shows NA instead of '?' (which means 'not known yet')."""

        for col_name in self.NA_COLUMNS:

            col = getattr(self, col_name)

            item = self.table.item(row, col)

            if item is not None and item.text() not in ("", self.UNKNOWN_TEXT):
                continue

            if col in (self.COL_SIZE, self.COL_SPEED):
                new_item = NumericTableWidgetItem(self.NOT_AVAILABLE_TEXT)
                new_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)
            else:
                new_item = QTableWidgetItem(self.NOT_AVAILABLE_TEXT)

            self.table.setItem(row, col, new_item)

    def get_status_icon(self, text):
        """Best-effort standard-icon lookup for a status prefix. Falls
        back gracefully (no icon) if a particular enum name isn't
        available in the installed Qt version, rather than crashing."""

        icon_map = {
            "Waiting for login": "SP_MessageBoxWarning",
            "Waiting": "SP_MessageBoxQuestion",
            "Processing": "SP_BrowserReload",
            "Downloading": "SP_ArrowDown",
            "Merging": "SP_BrowserReload",
            "Trying browser scrape": "SP_BrowserReload",
            "Completed": "SP_DialogApplyButton",
            "Failed": "SP_MessageBoxCritical",
            "Skipped": "SP_DialogDiscardButton",
            "Opened in Browser": "SP_DialogOpenButton",
        }

        for prefix, icon_name in icon_map.items():

            if text.startswith(prefix):

                for holder in (
                    getattr(QStyle, "StandardPixmap", None), QStyle
                ):

                    if holder is None:
                        continue

                    pixmap_enum = getattr(holder, icon_name, None)

                    if pixmap_enum is not None:
                        return self.style().standardIcon(pixmap_enum)

                return None

        return None

    def update_status(self, row_id, text):

        row = self.find_row_index(row_id)

        if row is None:
            return

        item = QTableWidgetItem(text)

        item.setData(self.ROW_ID_ROLE, row_id)

        icon = self.get_status_icon(text)

        if icon is not None:
            item.setIcon(icon)

        self.table.setItem(row, self.COL_STATUS, item)

        if text in ("Downloading", "Merging") or (
            is_terminal_status(text) and not text.startswith("Failed")
        ):
            self.fill_not_available(row)

        retry_widget = self.table.cellWidget(row, self.COL_RETRY)

        if retry_widget is not None:

            if text.startswith("Failed"):
                retry_widget.setText("RETRY")
                retry_widget.setToolTip("Try this download again.")
                retry_widget.setEnabled(True)
            elif is_terminal_status(text):
                retry_widget.setText("REDOWNLOAD")
                retry_widget.setToolTip("Download this again (with options).")
                retry_widget.setEnabled(True)
            else:
                retry_widget.setEnabled(False)

        if not self.autoscroll_checkbox.isChecked():
            return

        # Nothing being followed yet (e.g. autoscroll was just turned
        # back on) and this row is active -- start following it.
        if self.active_follow_row_id is None and not is_terminal_status(text):
            self.active_follow_row_id = row_id

        if row_id == self.active_follow_row_id:

            if is_terminal_status(text):
                # This one's done -- move on to whatever's next.
                self.active_follow_row_id = self.find_next_active_row_id()

            self.scroll_to_followed_row()

    def update_title(self, row_id, title):

        row = self.find_row_index(row_id)

        if row is None:
            return

        item = QTableWidgetItem(title)

        item.setToolTip(title)

        self.table.setItem(row, self.COL_TITLE, item)

    def update_user(self, row_id, user):

        row = self.find_row_index(row_id)

        if row is None:
            return

        self.table.setItem(
            row, self.COL_USER, QTableWidgetItem(user or self.NOT_AVAILABLE_TEXT)
        )

    def update_resolution(self, row_id, res):

        row = self.find_row_index(row_id)

        if row is None:
            return

        self.table.setItem(row, self.COL_RESOLUTION, QTableWidgetItem(res))

    def update_size(self, row_id, size_text, size_bytes=0):

        row = self.find_row_index(row_id)

        if row is None:
            return

        item = NumericTableWidgetItem(size_text)

        item.setData(NumericTableWidgetItem.SORT_ROLE, size_bytes)

        self.table.setItem(row, self.COL_SIZE, item)

        if row_id == self.active_follow_row_id:
            self.scroll_to_followed_row()

    def update_output_path(self, row_id, path):

        if row_id in self.row_records:
            self.row_records[row_id]["output_path"] = path

        row = self.find_row_index(row_id)

        if row is None:
            return

        # Just the filename (or folder name, for galleries) -- not the
        # full path, which is what "Show in Folder" / hover tooltips
        # are for.
        display_name = (
            os.path.basename(os.path.normpath(path)) if path
            else self.NOT_AVAILABLE_TEXT
        )

        item = QTableWidgetItem(display_name)

        item.setToolTip(path or "")

        self.table.setItem(row, self.COL_OUTPUT, item)

    def finalize_output(self, row_id, path):
        """Record the final file (or folder, for galleries) for a
        completed download -- powers Open File / Show in Folder and
        the Size column. Safe to call from a worker thread since it
        only emits signals; the actual table writes happen on the
        GUI thread via the connected slots. Returns (path, filename,
        size_text) for the caller to fold into its own log line (and
        pass along to record_success), rather than reading it back
        from row_records -- that would race against the signal above
        still being queued for the GUI thread to process."""

        if not path or not os.path.exists(path):
            return None, None, None

        signals.output_path.emit(row_id, path)

        total_bytes = 0

        if os.path.isdir(path):

            for root, _dirs, files in os.walk(path):

                for f in files:

                    try:
                        total_bytes += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass

        else:

            try:
                total_bytes = os.path.getsize(path)
            except OSError:
                total_bytes = 0

        size_text = format_size(total_bytes)

        signals.size.emit(row_id, size_text, total_bytes)

        filename = os.path.basename(os.path.normpath(path))

        return path, filename, size_text

    def update_queue(self, size):

        self.setWindowTitle(
            f"Modern Video Downloader Lite v{APP_VERSION} | Queue: {size}"
        )

    def restore_original_order(self):

        self.table.sortByColumn(self.COL_ORDER, Qt.AscendingOrder)

    def show_selected_cell_detail(self):
        """Shows the full text of whichever cell is currently
        selected -- columns can be narrower than their content
        (titles, URLs), and tooltips require hovering, so this gives
        a persistent, copyable view of the full value. Highlights the
        current table search term within it, the same way the log
        highlights matches, so you can see exactly where it matched."""

        items = self.table.selectedItems()

        if not items:
            self.cell_detail_box.setText(
                "Select a cell above to see its full text here..."
            )
            return

        escaped = html.escape(items[0].text())

        term = self.table_search_input.text().strip()

        if term:
            escaped = self.highlight_html(escaped, term)

        self.cell_detail_box.setText(escaped)

    def position_select_all_checkbox(self):
        """Keeps the header's select-all checkbox aligned over the
        'Sel' column's current position/width -- needed since Qt has
        no native way to put a real widget inside a header section,
        so this is a plain child widget manually kept in place."""

        try:

            header = self.table.horizontalHeader()

            x = header.sectionViewportPosition(self.COL_SELECT)

            w = header.sectionSize(self.COL_SELECT)

            h = header.height()

            cb_size = self.select_all_checkbox.sizeHint()

            self.select_all_checkbox.move(
                int(x) + max(0, (int(w) - cb_size.width()) // 2),
                max(0, (int(h) - cb_size.height()) // 2)
            )

        except (TypeError, ValueError):

            # Header geometry isn't meaningful yet (e.g. before the
            # window has been laid out/shown for the first time) --
            # harmless to skip; the next resize/move will retry.
            return

        self.select_all_checkbox.show()

    def on_select_all_toggled(self, state):

        checked = bool(state)

        for record in self.row_records.values():

            checkbox = record.get("select_checkbox")

            if checkbox is not None:
                checkbox.setChecked(checked)

    def get_checked_row_ids(self):

        checked = []

        for row_id, record in self.row_records.items():

            checkbox = record.get("select_checkbox")

            if checkbox is not None and checkbox.isChecked():
                checked.append(row_id)

        return checked

    def _row_status_text(self, row_id):

        row = self.find_row_index(row_id)

        if row is None:
            return None

        item = self.table.item(row, self.COL_STATUS)

        return item.text() if item is not None else None

    def run_bulk_action(self):

        action = self.bulk_action_combo.currentText()

        row_ids = self.get_checked_row_ids()

        if not row_ids:

            QMessageBox.information(
                self, "Nothing selected",
                "Check the 'Sel' box on the rows you want to act on first."
            )

            return

        if action == "Remove Files":

            reply = QMessageBox.question(
                self, "Delete files?",
                f"Permanently delete the file for each of the "
                f"{len(row_ids)} checked row(s) that has one on "
                "record? This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                return

            for row_id in row_ids:
                self._delete_output_file_silent(row_id)

        elif action == "Remove Record":

            reply = QMessageBox.question(
                self, "Remove records?",
                f"Remove {len(row_ids)} checked row(s) from the list? "
                "This won't delete any files or touch the log.",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                return

            for row_id in row_ids:
                self.remove_row(row_id, skip_confirm=True)

        elif action == "Redownload":

            eligible = [
                rid for rid in row_ids
                if self._row_status_text(rid) and is_terminal_status(self._row_status_text(rid))
            ]

            if not eligible:

                QMessageBox.information(
                    self, "Nothing to redownload",
                    "None of the checked rows are in a finished state."
                )

                return

            dlg = RedownloadOptionsDialog(f"{len(eligible)} selected item(s)", self)

            if dlg.exec() != QDialog.Accepted:
                return

            delete_first = dlg.delete_file_checkbox.isChecked()

            keep_old_row = dlg.keep_old_row_checkbox.isChecked()

            for row_id in eligible:

                if delete_first:
                    self._delete_output_file_silent(row_id)

                if keep_old_row:

                    record = self.row_records.get(row_id)

                    if record is None:
                        continue

                    url_original = record["url_original"]

                    url_clean = record.get("url_clean") or resolve_and_clean_url(url_original)

                    domain = record.get("domain") or domain_from_url(url_clean)

                    self.queue_fresh_download(url_original, url_clean, domain)

                else:

                    self.retry_row(row_id)

        elif action == "Retry":

            eligible = [
                rid for rid in row_ids
                if (self._row_status_text(rid) or "").startswith("Failed")
            ]

            if not eligible:

                QMessageBox.information(
                    self, "Nothing to retry",
                    "None of the checked rows are Failed."
                )

                return

            for row_id in eligible:
                self.retry_row(row_id)

    def apply_table_filter(self):

        text = self.table_search_input.text().strip().lower()

        column_label = self.table_search_column.currentText()

        column_map = {
            "Status": self.COL_STATUS,
            "Title": self.COL_TITLE,
            "Resolution": self.COL_RESOLUTION,
            "Size": self.COL_SIZE,
            "Speed": self.COL_SPEED,
            "User": self.COL_USER,
            "Output File": self.COL_OUTPUT,
            "URL": self.COL_URL
        }

        # Background gets overridden by the dark theme's own
        # stylesheet (a known QTableWidget/QSS quirk -- ::item style
        # rules win over setBackground()), and combining that with a
        # black foreground made text unreadable when the background
        # didn't actually change. Foreground alone -- a plain yellow
        # tint on the matching text, like the log's highlighting --
        # reliably shows up regardless of the theme.
        highlight_fg = QBrush(QColor(SEARCH_HIGHLIGHT_COLOR))

        clear_brush = QBrush()

        for row in range(self.table.rowCount()):

            if not text:

                self.table.setRowHidden(row, False)

                for col in column_map.values():

                    item = self.table.item(row, col)

                    if item is not None:
                        item.setForeground(clear_brush)

                continue

            if column_label == "All Columns":
                columns_to_check = column_map.values()
            else:
                columns_to_check = [column_map[column_label]]

            match = False

            for col in column_map.values():

                item = self.table.item(row, col)

                if item is None:
                    continue

                cell_matches = (
                    col in columns_to_check and text in item.text().lower()
                )

                if cell_matches:
                    item.setForeground(highlight_fg)
                    match = True
                else:
                    item.setForeground(clear_brush)

            self.table.setRowHidden(row, not match)

        self.show_selected_cell_detail()

    def show_table_context_menu(self, pos):

        row = self.table.rowAt(pos.y())

        if row < 0:
            return

        status_item = self.table.item(row, self.COL_STATUS)

        if status_item is None:
            return

        row_id = status_item.data(self.ROW_ID_ROLE)

        record = self.row_records.get(row_id)

        if record is None:
            return

        status_text = status_item.text()

        is_active = not is_terminal_status(status_text)

        is_failed = status_text.startswith("Failed")

        menu = QMenu(self)

        open_action = menu.addAction("Open File")

        show_action = menu.addAction("Show in Folder")

        menu.addSeparator()

        copy_action = menu.addAction("Copy URL")

        open_browser_action = menu.addAction("Open in Browser")

        menu.addSeparator()

        retry_action = menu.addAction("Retry")

        retry_action.setEnabled(is_failed)

        redownload_action = menu.addAction("Redownload")

        redownload_action.setEnabled(not is_active)

        menu.addSeparator()

        delete_action = menu.addAction("Delete File")

        output_path = record.get("output_path")

        delete_action.setEnabled(bool(output_path))

        menu.addSeparator()

        remove_action = menu.addAction("Remove Record")

        open_action.setEnabled(bool(output_path))

        show_action.setEnabled(bool(output_path))

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))

        if chosen == open_action:

            try:
                open_file_with_default_app(output_path)
            except OSError as e:
                QMessageBox.warning(self, "Could not open file", str(e))

        elif chosen == show_action:

            try:
                reveal_in_file_manager(output_path)
            except OSError as e:
                QMessageBox.warning(self, "Could not open folder", str(e))

        elif chosen == copy_action:

            self.copy_url_to_clipboard(record["url_original"])

        elif chosen == open_browser_action:

            webbrowser.open(record["url_original"])

        elif chosen == retry_action:

            self.retry_row(row_id)

        elif chosen == redownload_action:

            self.redownload_row(row_id)

        elif chosen == delete_action:

            self.delete_output_file(row_id)

        elif chosen == remove_action:

            self.remove_row(row_id)

    def remove_row(self, row_id, skip_confirm=False):
        """Removes a single row's RECORD from the list (not from
        disk, not from the log). Safe to call even while that
        download is still active -- the worker just won't find a
        matching row to update anymore, which is harmless. Confirms
        first unless skip_confirm is set (used when the confirmation
        already happened as part of a Delete File dialog)."""

        record = self.row_records.get(row_id)

        url_original = record.get("url_original", "") if record else ""

        if not skip_confirm:

            reply = QMessageBox.question(
                self,
                "Remove record?",
                "Remove this record from the list?\n\n"
                f"{url_original}\n\n"
                "This only removes it from the table -- it won't delete "
                "any downloaded file or touch the log.",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply != QMessageBox.Yes:
                return

        row = self.find_row_index(row_id)

        if row is None:
            return

        self.table.removeRow(row)

        self.row_records.pop(row_id, None)

        if self.active_follow_row_id == row_id:

            self.active_follow_row_id = (
                self.find_next_active_row_id()
                if self.autoscroll_checkbox.isChecked() else None
            )

    def clear_download_list(self):
        """Removes rows from the table matching the selected filter.
        Doesn't touch the log file or any downloaded files -- this is
        purely about tidying up the table view."""

        filter_choice = self.clear_list_combo.currentText()

        matches = []  # list of (row, row_id, status_text)

        for row in range(self.table.rowCount()):

            item = self.table.item(row, self.COL_STATUS)

            if item is None:
                continue

            row_id = item.data(self.ROW_ID_ROLE)

            status_text = item.text()

            if filter_choice == "All" or status_text.startswith(filter_choice):
                matches.append((row, row_id, status_text))

        if not matches:

            QMessageBox.information(
                self, "Nothing to clear",
                f"No rows match '{filter_choice}'."
            )

            return

        active_count = sum(
            1 for _, _, status_text in matches
            if not is_terminal_status(status_text)
        )

        if active_count:

            warning_line = (
                f"{active_count} of these are still in progress. "
                "Removing them from this list won't stop the "
                "download, but you'll lose track of its progress "
                "here.\n\n"
            )

        else:

            warning_line = ""

        reply = QMessageBox.question(
            self,
            "Remove from list?",
            f"{warning_line}Remove {len(matches)} row(s) matching "
            f"'{filter_choice}' from the list?\n\n"
            "This only affects the table -- it won't delete any "
            "downloaded files or touch the log.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        # Remove highest row index first so earlier indices don't
        # shift out from under the rest of the loop.
        for row, row_id, _status_text in sorted(matches, reverse=True):

            self.table.removeRow(row)

            self.row_records.pop(row_id, None)

            if self.active_follow_row_id == row_id:
                self.active_follow_row_id = None

        if (
            self.active_follow_row_id is None
            and self.autoscroll_checkbox.isChecked()
        ):
            self.active_follow_row_id = self.find_next_active_row_id()

    def handle_retry_or_redownload(self, row_id):
        """The Retry/Redownload column button does one or the other
        depending on the row's current status -- Retry for a Failed
        row, Redownload (with its options dialog) for any other
        finished row. update_status keeps the button's label and
        enabled-state in sync with which of these it would do."""

        row = self.find_row_index(row_id)

        status_text = self.table.item(row, self.COL_STATUS).text() if row is not None else ""

        if status_text.startswith("Failed"):
            self.retry_row(row_id)
        elif is_terminal_status(status_text):
            self.redownload_row(row_id)

    def retry_row(self, row_id):

        record = self.row_records.get(row_id)

        if record is None:
            return

        url_original = record["url_original"]

        url_clean = record.get("url_clean") or resolve_and_clean_url(url_original)

        domain = record.get("domain") or domain_from_url(url_clean)

        norm = normalize_url_for_dedupe(url_clean)

        video_uid = record.get("video_uid")

        with _queue_dedupe_lock:

            # Explicit retry overrides the normal "already downloaded"
            # dedupe checks -- the user is deliberately asking for
            # this one again (this same path also powers Redownload,
            # for a row that already completed). Re-queueing into the
            # SAME row (rather than calling add_download, which would
            # add a new one) is what keeps it from ending up as two
            # rows.
            COMPLETED_URLS.discard(norm)

            if video_uid:
                COMPLETED_IDS.discard(video_uid)

            already_queued = norm in QUEUED_URLS

            if not already_queued:
                QUEUED_URLS.add(norm)

        if already_queued:

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(
                f"Already queued/downloading:\n{url_original}"
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            return

        self.reset_row_for_retry(row_id)

        site_queue = self.get_or_create_site_queue(domain)

        site_queue.put((row_id, url_original, url_clean))

        signals.queue_update.emit(self.total_queue_size())

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(f"Retrying:\n{url_original}")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    def reset_row_for_retry(self, row_id):
        """Puts an existing row back into 'Waiting' state for a retry,
        instead of add_table_row creating a brand new one -- so a
        retried link stays as one row, not two."""

        row = self.find_row_index(row_id)

        if row is None:
            return

        status_item = QTableWidgetItem("Waiting")

        status_item.setData(self.ROW_ID_ROLE, row_id)

        icon = self.get_status_icon("Waiting")

        if icon is not None:
            status_item.setIcon(icon)

        self.table.setItem(row, self.COL_STATUS, status_item)

        progress_bar = self.table.cellWidget(row, self.COL_PROGRESS)

        if progress_bar is not None:
            progress_bar.setValue(0)

        progress_item = self.table.item(row, self.COL_PROGRESS)

        if progress_item is not None:
            progress_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)

        retry_btn = self.table.cellWidget(row, self.COL_RETRY)

        if retry_btn is not None:
            retry_btn.setEnabled(False)

        if (
            self.autoscroll_checkbox.isChecked()
            and self.active_follow_row_id is None
        ):
            self.active_follow_row_id = row_id

        self.scroll_to_followed_row()

    def redownload_row(self, row_id):
        """Forces a fresh download even for a row that already
        completed (e.g. you deleted the file and want it back, or
        just want another copy). Offers two independent choices:
        whether to delete the previous file first (default: yes), and
        whether to reuse this row in place or keep it untouched and
        start a brand new row for the fresh copy (default: reuse in
        place) -- the latter is how you avoid losing the old file's
        reference if you chose not to delete it."""

        record = self.row_records.get(row_id)

        if record is None:
            return

        url_original = record["url_original"]

        dlg = RedownloadOptionsDialog(url_original, self)

        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.delete_file_checkbox.isChecked():
            self._delete_output_file_silent(row_id)

        if dlg.keep_old_row_checkbox.isChecked():

            url_clean = record.get("url_clean") or resolve_and_clean_url(url_original)

            domain = record.get("domain") or domain_from_url(url_clean)

            self.queue_fresh_download(url_original, url_clean, domain)

        else:

            self.retry_row(row_id)

    def queue_fresh_download(self, url_original, url_clean, domain):
        """Starts a brand new row/download for a URL, bypassing the
        completed-dedupe checks (this is only called from an explicit
        user action -- Redownload with 'keep the old row' checked) but
        still respecting the in-flight QUEUED_URLS check so it can't
        race a second copy of itself."""

        norm = normalize_url_for_dedupe(url_clean)

        with _queue_dedupe_lock:

            already_queued = norm in QUEUED_URLS

            if not already_queued:
                QUEUED_URLS.add(norm)

        if already_queued:

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(
                f"Already queued/downloading:\n{url_original}"
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            return

        row_id = self.add_table_row(url_original)

        self.row_records[row_id]["domain"] = domain

        self.row_records[row_id]["url_clean"] = url_clean

        site_queue = self.get_or_create_site_queue(domain)

        site_queue.put((row_id, url_original, url_clean))

        signals.queue_update.emit(self.total_queue_size())

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(f"Redownloading (new row):\n{url_original}")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    def delete_output_file(self, row_id):
        """Deletes the actual downloaded file (or gallery folder) from
        disk. Does not touch the row/log history -- the row stays as
        a record that it WAS downloaded, just with nothing left on
        disk to open."""

        record = self.row_records.get(row_id)

        if record is None:
            return

        path = record.get("output_path")

        if not path or not os.path.exists(path):

            QMessageBox.information(
                self,
                "Nothing to delete",
                "No downloaded file is on record for this row (or it's "
                "already gone)."
            )

            return

        dlg = DeleteFileOptionsDialog(path, self)

        if dlg.exec() != QDialog.Accepted:
            return

        self._delete_output_file_silent(row_id)

        if dlg.remove_row_checkbox.isChecked():
            self.remove_row(row_id, skip_confirm=True)

    def _delete_output_file_silent(self, row_id):
        """The actual deletion, with no confirmation dialog of its
        own -- used both by delete_output_file (which confirms first)
        and by Redownload's own 'delete previous file' checkbox, where
        the redownload dialog itself already serves as confirmation.
        Returns True if a file was deleted, False otherwise (nothing
        on record, already gone, or a real deletion error)."""

        record = self.row_records.get(row_id)

        if record is None:
            return False

        path = record.get("output_path")

        if not path or not os.path.exists(path):
            return False

        try:

            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        except OSError as e:

            QMessageBox.warning(self, "Could not delete file", str(e))

            return False

        record["output_path"] = None

        row = self.find_row_index(row_id)

        delete_line = f"Deleted file:\n{path}"

        delete_line += f"\nURL: {record.get('url_original', '')}"

        if row is not None:

            item = QTableWidgetItem("(deleted)")

            item.setToolTip(path)

            self.table.setItem(row, self.COL_OUTPUT, item)

            user_item = self.table.item(row, self.COL_USER)

            if user_item is not None and user_item.text() not in ("", "?", "NA"):
                delete_line += f"\nAuthor: {user_item.text()}"

            size_item = self.table.item(row, self.COL_SIZE)

            if size_item is not None and size_item.text() not in ("", "?", "NA"):
                delete_line += f"\nSize: {size_item.text()}"

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(delete_line)

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        return True

    # =====================================================
    # FOLDER
    # =====================================================

    def select_folder(self):

        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Folder",
            self.output_dir
        )

        if folder:

            self.output_dir = folder

            self.folder_label.setText(
                folder
            )

            cfg = load_config()

            cfg["output_dir"] = folder

            save_config(cfg)

    # =====================================================
    # CLIPBOARD
    # =====================================================

    def toggle_clipboard(self):

        if self.auto_clipboard.isChecked():

            QApplication.clipboard().dataChanged.connect(
                self.check_clipboard
            )

            signals.log.emit(
                "Clipboard listener ENABLED"
            )

        else:

            try:

                QApplication.clipboard().dataChanged.disconnect(
                    self.check_clipboard
                )
            except RuntimeError:
                pass

            signals.log.emit(
                "Clipboard listener DISABLED"
            )

    def copy_url_to_clipboard(self, url):

        QApplication.clipboard().setText(url)

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(f"Copied URL:\n{url}")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    def check_clipboard(self):

        try:

            text = QApplication.clipboard().text().strip()

            if not text:
                return

            if text == self.last_clip:
                return

            if not text.startswith("http") or "\n" in text:
                return

            # Remember it before checking, so a copied link that isn't
            # downloadable isn't re-checked on every clipboard event.
            self.last_clip = text

            if not is_supported_url(text, strict=True):
                return

            self.add_download(
                text
            )

        except Exception as e:

            # Never let a clipboard hiccup silently kill future
            # detections -- surface it in the log instead.
            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(f"Clipboard listener error:\n{e}")

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    # =====================================================
    # DOWNLOAD
    # =====================================================

    def manual_download(self):

        text = self.url_input.toPlainText().strip()

        if not text:
            return

        self.queue_urls_from_text(text)

        self.url_input.clear()

    def import_from_file(self):

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import URLs from file",
            APP_DIR,
            "Text files (*.txt);;All files (*)"
        )

        if not path:
            return

        try:

            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

        except OSError as e:

            QMessageBox.warning(
                self,
                "Could not read file",
                str(e)
            )

            return

        self.queue_urls_from_text(text)

    def queue_urls_from_text(self, text):

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        valid_urls = [
            line for line in lines
            if is_supported_url(line)
        ]

        ignored = len(lines) - len(valid_urls)

        self.enqueue_bulk(valid_urls)

        if ignored:

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(
                f"Ignored {ignored} line(s) that weren't recognized URLs."
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    BULK_BATCH_SIZE = 25

    def enqueue_bulk(self, urls):
        """Adds many links without freezing the window: they're added
        in small batches, and the window gets to redraw and respond
        between batches."""

        if not urls:
            return

        starting = not self._pending_add_urls

        self._pending_add_urls.extend(urls)

        if len(urls) > self.BULK_BATCH_SIZE:
            self.add_log(f"Adding {len(urls)} links...")

        if starting:
            QTimer.singleShot(0, self._process_pending_adds)

    def _process_pending_adds(self):

        batch = self._pending_add_urls[:self.BULK_BATCH_SIZE]

        del self._pending_add_urls[:self.BULK_BATCH_SIZE]

        self._bulk_adding = True

        self.table.setSortingEnabled(False)

        self.table.setUpdatesEnabled(False)

        try:

            for url in batch:

                try:
                    self.add_download(url)
                except Exception as e:
                    self.add_log(f"Couldn't add link:\n{url}\n{e}")

        finally:

            self._bulk_adding = False

            self.table.setUpdatesEnabled(True)

        if self._pending_add_urls:

            # Give the window a moment to redraw and handle clicks.
            QTimer.singleShot(10, self._process_pending_adds)

            return

        self.finish_row_changes()

    def add_download(self, url):
        """Entry point for every new URL (paste, import, clipboard).

        Short/share links need a network request to find out where
        they really point, which used to run right here on the GUI
        thread and could freeze the window for up to 8 seconds per
        link. Those now resolve on a background thread and come back
        through signals.url_resolved; ordinary links need no network
        and are handled immediately."""

        if is_short_link(unwrap_redirect(url)):

            signals.log.emit(f"Resolving short link:\n{url}")

            self._resolver_pool.submit(self._resolve_in_background, url)

            return

        self.enqueue_resolved_url(url, resolve_and_clean_url(url))

    def _resolve_in_background(self, url):

        try:
            url_clean = resolve_and_clean_url(url)
        except Exception:
            # resolve_short_link already falls back to the original
            # URL on network errors; this only guards anything else.
            url_clean = strip_tracking_params(url)

        signals.url_resolved.emit(url, url_clean)

    def enqueue_resolved_url(self, url, url_clean):
        """Dedupe check + queueing for a URL whose final form is
        known. Always runs on the GUI thread (it builds table rows)."""

        norm = normalize_url_for_dedupe(url_clean)

        with _queue_dedupe_lock:

            already_completed = norm in COMPLETED_URLS

            already_queued = (not already_completed) and norm in QUEUED_URLS

            if not already_completed and not already_queued:
                QUEUED_URLS.add(norm)

        if already_completed:

            historical = COMPLETED_RECORDS.get(norm, {})

            skip_line = f"Skipped (already downloaded):\n{url}"

            if historical.get("uploader"):
                skip_line += f"\nAuthor: {historical['uploader']}"

            if historical.get("size_text"):
                skip_line += f"\nSize: {historical['size_text']}"

            if historical.get("output_filename"):
                skip_line += f"\nSaved as: {historical['output_filename']}"

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(skip_line)

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            # Outside the lock -- this does real UI/file-I/O work
            # (building a row, checking the historical file's size),
            # which shouldn't hold up a worker thread trying to
            # acquire the same lock to record its own result.
            self.add_completed_placeholder_row(url, url_clean, norm)

            return

        if already_queued:

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            signals.log.emit(
                f"Skipped (duplicate already in queue):\n{url}"
            )

            signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

            return

        domain = domain_from_url(url_clean)

        row_id = self.add_table_row(url)

        self.row_records[row_id]["domain"] = domain

        # Without this, the row kept the raw pasted link as its
        # "clean" URL, so Retry / Redownload used the uncleaned link
        # and recorded history under a different key than the
        # original download.
        self.row_records[row_id]["url_clean"] = url_clean

        site_queue = self.get_or_create_site_queue(domain)

        site_queue.put(
            (row_id, url, url_clean)
        )

        signals.queue_update.emit(
            self.total_queue_size()
        )

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(
            f"Added to queue ({domain}):\n{url}"
        )

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    def add_completed_placeholder_row(self, url_original, url_clean, norm):
        """When a pasted URL matches something already downloaded,
        show it as a Completed row (with Redownload/Delete File/etc.
        available via the right-click menu) instead of just a log
        line that's easy to miss in a busy log."""

        historical = COMPLETED_RECORDS.get(norm, {})

        row_id = self.add_table_row(url_original)

        domain = historical.get("domain") or domain_from_url(url_clean)

        self.row_records[row_id]["domain"] = domain

        self.row_records[row_id]["url_clean"] = (
            historical.get("cleaned_url") or url_clean
        )

        video_uid = historical.get("video_uid")

        if video_uid:
            self.row_records[row_id]["video_uid"] = video_uid

        self.update_title(row_id, historical.get("title") or "(already downloaded)")

        self.update_resolution(row_id, historical.get("resolution") or "NA")

        self.update_speed(row_id, historical.get("speed") or "NA")

        self.update_progress(row_id, 100)

        self.update_user(row_id, historical.get("uploader"))

        self.update_status(row_id, "Completed")

        output_path = historical.get("output_path")

        if output_path and os.path.exists(output_path):

            # The file's still there -- get a fresh, accurate size
            # rather than trusting whatever was cached when it was
            # first downloaded.
            self.finalize_output(row_id, output_path)

        else:

            # Either we never had a path on record, or the file's
            # since been moved/deleted -- fall back to whatever was
            # cached at download time so the row isn't just blank,
            # but be clear that it's not verified to still be there.
            row = self.find_row_index(row_id)

            if row is not None:

                cached_size = historical.get("size_text")

                size_item = NumericTableWidgetItem(cached_size or "NA")

                size_item.setData(NumericTableWidgetItem.SORT_ROLE, 0)

                self.table.setItem(row, self.COL_SIZE, size_item)

                cached_filename = historical.get("output_filename")

                output_label = (
                    f"(missing) {cached_filename}" if cached_filename
                    else "(missing)"
                )

                output_item = QTableWidgetItem(output_label)

                output_item.setToolTip(
                    output_path or "No file path was recorded for this download."
                )

                self.table.setItem(row, self.COL_OUTPUT, output_item)

    # =====================================================
    # WORKERS -- one queue + worker pool PER SITE, so
    # concurrency and pacing can be limited independently per site
    # =====================================================

    def get_or_create_site_queue(self, domain):
        """Lazily creates a queue and worker pool for a domain, sized
        to its currently configured max_concurrent. Raising the
        setting later tops up the pool with more workers; lowering it
        doesn't kill already-running threads -- they simply idle once
        the queue drains, which is a fine trade-off for a desktop app
        and avoids the complexity of tearing down live threads."""

        with self.queue_setup_lock:

            if domain not in self.site_queues:
                self.site_queues[domain] = Queue()

            settings = get_site_settings(domain)

            desired = max(1, int(settings.get("max_concurrent", 1) or 1))

            while self.site_worker_counts[domain] < desired:

                t = Thread(
                    target=self.site_worker,
                    args=(domain,),
                    daemon=True
                )

                t.start()

                self.site_worker_counts[domain] += 1

            return self.site_queues[domain]

    def total_queue_size(self):

        return sum(q.qsize() for q in self.site_queues.values())

    def site_worker(self, domain):

        site_queue = self.site_queues[domain]

        while True:

            row_id, url, url_clean = site_queue.get()

            signals.queue_update.emit(self.total_queue_size())

            # Per-site pacing delay -- space out when downloads for
            # this site START (not just when they finish), so even
            # with max_concurrent > 1 the requests don't all fire at
            # once. Serializing this section across the site's workers
            # is what actually staggers the starts.
            settings = get_site_settings(domain)

            delay = float(settings.get("delay_seconds", 0) or 0)

            random_delay = bool(settings.get("random_delay", False))

            delay_from = settings.get("delay_from", "start")

            if delay > 0:

                # "Random" mode treats the configured delay as a
                # ceiling rather than a fixed wait -- each start picks
                # a fresh random gap between 0 and that max, instead
                # of always waiting exactly the same amount, which
                # looks less like an automated, perfectly-timed pattern.
                target_gap = random.uniform(0, delay) if random_delay else delay

                # "Finish" counts the gap from when the previous download
                # for this site completed; "Start" from when it started.
                reference = (
                    self.site_last_finish if delay_from == "finish"
                    else self.site_last_dispatch
                )

                with self.site_dispatch_lock[domain]:

                    wait_left = reference[domain] + target_gap - time.time()

                    if wait_left > 0:

                        signals.status.emit(
                            row_id, f"Waiting ({wait_left:.0f}s delay)"
                        )

                        time.sleep(wait_left)

                    self.site_last_dispatch[domain] = time.time()

            try:

                self.download_video(
                    row_id,
                    url,
                    url_clean
                )

                self.site_last_finish[domain] = time.time()

            except Exception as e:

                self.site_last_finish[domain] = time.time()


                signals.status.emit(row_id, "Failed")

                signals.log.emit(
                    f"Worker Error:\n{e}"
                )

                with _queue_dedupe_lock:
                    QUEUED_URLS.discard(normalize_url_for_dedupe(url_clean))

                append_failed_url(url)

                append_log_entry({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "status": "Failed",
                    "url": url,
                    "reason": str(e)[:500]
                })

            site_queue.task_done()

    # =====================================================
    # LOG / DEDUPE RECORDING
    # =====================================================

    def record_success(
        self, row, url_original, url_clean, domain, title, res,
        video_uid=None, output_filename=None, size_text=None,
        output_path=None, uploader=None
    ):

        norm = normalize_url_for_dedupe(url_clean)

        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "Completed",
            "url": url_original,
            "cleaned_url": url_clean,
            "normalized_url": norm,
            "video_uid": video_uid,
            "domain": domain,
            "title": title,
            "resolution": res,
            "speed": self.row_records.get(row, {}).get("last_speed"),
            "uploader": uploader,
            "output_dir": self.output_dir,
            "output_path": output_path,
            "output_filename": output_filename,
            "size_text": size_text
        }

        with _queue_dedupe_lock:

            COMPLETED_URLS.add(norm)

            if video_uid:
                COMPLETED_IDS.add(video_uid)

            COMPLETED_RECORDS[norm] = record

            QUEUED_URLS.discard(norm)

        append_log_entry(record)

        remove_failed_url(url_original)

        signals.progress.emit(row, 100)

        signals.status.emit(row, "Completed")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        log_line = f"Completed:\n{title}"

        if uploader:
            log_line += f"\nAuthor: {uploader}"

        if size_text:
            log_line += f"\nSize: {size_text}"

        if output_filename:
            log_line += f"\nSaved as: {output_filename}"

        log_line += f"\nURL: {url_original}"

        signals.log.emit(log_line)

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        # record_success runs on a worker thread; Qt widgets (the
        # tray icon included) may only be touched from the GUI thread,
        # so this goes through a signal instead of calling it directly.
        signals.notify.emit("Download Completed", title)

    def record_failure(self, row, url_original, url_clean, domain, reason):

        norm = normalize_url_for_dedupe(url_clean)

        with _queue_dedupe_lock:
            QUEUED_URLS.discard(norm)

        signals.status.emit(row, "Failed")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(f"Download failed:\n{reason}")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        append_log_entry({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "Failed",
            "url": url_original,
            "cleaned_url": url_clean,
            "domain": domain,
            "reason": str(reason)[:500]
        })

        append_failed_url(url_original)

    def record_skipped_duplicate(self, row, url_original, url_clean, reason):

        norm = normalize_url_for_dedupe(url_clean)

        with _queue_dedupe_lock:
            QUEUED_URLS.discard(norm)

        signals.status.emit(row, "Skipped (duplicate)")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(
            f"Skipped (matches a previous download, {reason}):\n{url_original}"
        )

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

    def record_manual(self, row, url_original, url_clean, domain):
        """For Facebook image posts: we open it in the system browser
        instead of downloading it ourselves, so there's nothing to
        mark 'Completed' -- just release the dedupe guard and log it.
        Not added to the completed-URLs log, since we can't actually
        confirm whether the user saved it."""

        norm = normalize_url_for_dedupe(url_clean)

        with _queue_dedupe_lock:
            QUEUED_URLS.discard(norm)

        signals.status.emit(row, "Opened in Browser")

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        signals.log.emit(
            f"Opened in your browser for manual saving:\n{url_original}"
        )

        signals.log.emit(DOWNLOAD_LOG_SEPARATOR)

        append_log_entry({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "Opened in Browser",
            "url": url_original,
            "cleaned_url": url_clean,
            "domain": domain
        })

    def try_facebook_image(
        self, row, url_original, url_clean, domain, cookie_text, title, res
    ):
        """Facebook post with no playable video. Try gallery-dl first
        -- it does have a Facebook extractor, and recent versions have
        specifically worked on Facebook handling, though it's known to
        get rate-limited/blocked when fetching image URLs directly
        (per gallery-dl's own issue tracker) -- then fall back to just
        opening the URL in the system browser for the user to save it
        themselves."""

        if check_gallery_dl_available():

            try:

                files = run_gallery_dl(
                    url_clean, self.output_dir, cookie_text,
                    self.cookie_user_agent(domain, cookie_text)
                )

                shown_folder = os.path.dirname(files[0])

                signals.log.emit(
                    f"Downloaded via gallery-dl "
                    f"({len(files)} file(s)) to:\n{shown_folder}"
                )

                output_target = files[0] if len(files) == 1 else shown_folder

                _path, output_filename, size_text = self.finalize_output(
                    row, output_target
                )

                self.record_success(
                    row, url_original, url_clean, domain, title, res,
                    output_filename=output_filename, size_text=size_text,
                    output_path=output_target
                )

                return

            except RuntimeError as gdl_e:

                signals.log.emit(
                    f"gallery-dl failed for this Facebook post:\n{gdl_e}"
                )

        else:

            signals.log.emit(
                "gallery-dl not installed -- skipping that attempt "
                "(pip install gallery-dl to enable it)."
            )

        signals.log.emit(
            "Opening this Facebook URL in your browser so you can "
            "save the image yourself."
        )

        webbrowser.open(url_clean)

        self.record_manual(row, url_original, url_clean, domain)

    # =====================================================
    # LOGIN FLOW (called from worker thread)
    # =====================================================

    def request_login_and_wait(self, domain, url):

        request = LoginRequest(domain, url)

        signals.login_required.emit(request)

        request.event.wait()

        return request.success

    # =====================================================
    # VIDEO / IMAGE DOWNLOAD
    # =====================================================

    def download_video(
        self, row, url_original, url_clean, _retried=False,
        _without_cookies=False
    ):

        domain = domain_from_url(url_clean)

        signals.status.emit(
            row,
            "Processing"
        )

        # Cookies are read (and decrypted, if protected) into memory
        # and handed to yt-dlp as a text stream -- never written out as
        # a plain file for yt-dlp.
        try:

            # A retry after the site rejected the saved cookies runs
            # without them.
            cookie_text = None if _without_cookies else read_site_cookies(domain)

        except CookieVaultLocked:

            cookie_text = None

            signals.log.emit(
                f"Saved cookies for {domain} are locked (master "
                "password not entered) -- downloading without them. "
                "Unlock them in MANAGE COOKIES."
            )

        except (OSError, CookieVaultError) as e:

            cookie_text = None

            signals.log.emit(f"Couldn't read saved cookies for {domain}:\n{e}")

        cookie_stream = io.StringIO(cookie_text) if cookie_text else None

        # =============================================
        # TS
        # =============================================

        ts = datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S"
        )

        # =============================================
        # PROGRESS HOOK
        # =============================================

        # Captures the real final path once yt-dlp's own
        # move/merge postprocessor finishes -- more reliable than
        # guessing from outtmpl, since merge_output_format changes
        # the extension and not every post needs a merge.
        final_output_path = [None]

        def postprocessor_hook(d):

            if d.get('status') == 'finished':

                fp = d.get('info_dict', {}).get('filepath')

                if fp:
                    final_output_path[0] = fp

        # Only send the GUI what actually changed, at most a few times
        # a second: yt-dlp calls this hook many times per second per
        # download, and redrawing the status/progress cells on every
        # call is a big part of what made the window sluggish with many
        # downloads.
        hook_state = {"percent": None, "status": None, "speed_time": 0.0}

        def progress_hook(d):

            if d['status'] == 'downloading':

                total = d.get(
                    'total_bytes'
                ) or d.get(
                    'total_bytes_estimate'
                )

                downloaded = d.get(
                    'downloaded_bytes',
                    0
                )

                if hook_state["status"] != "Downloading":

                    hook_state["status"] = "Downloading"

                    signals.status.emit(row, "Downloading")

                if total:

                    percent = int(
                        downloaded / total * 100
                    )

                    if percent != hook_state["percent"]:

                        hook_state["percent"] = percent

                        signals.progress.emit(row, percent)

                speed = d.get('speed')

                now = time.time()

                if speed and now - hook_state["speed_time"] >= 0.5:

                    hook_state["speed_time"] = now

                    signals.speed.emit(row, format_speed(speed))

            elif d['status'] == 'finished':

                # A separate video and audio stream are downloaded one
                # after the other; the next one reports "downloading"
                # again, so let the status switch back.
                hook_state["status"] = "Merging"

                hook_state["percent"] = None

                signals.status.emit(
                    row,
                    "Merging"
                )

        # =============================================
        # OPTIONS
        # =============================================

        ydl_opts = {

            'format':
                'bestvideo+bestaudio/best',

            'merge_output_format':
                'mp4',

            # Falls back through channel/uploader_id/site name when a
            # site doesn't report an uploader (common outside the big
            # platforms, which used to give "NA_<timestamp>"), and
            # includes the video ID so two downloads from the same
            # uploader starting in the same second can't overwrite
            # each other.
            'outtmpl':
                os.path.join(
                    self.output_dir,
                    f"%(uploader,channel,uploader_id,extractor_key)s_{ts}_%(id)s.%(ext)s"
                ),

            'quiet': True,

            # Same safe names on every system (no characters Windows
            # rejects), and a length cap so long uploader names can't
            # push the full path past Windows' 260-character limit.
            'windowsfilenames': True,

            'trim_file_name': 150,

            'noplaylist': True,

            'progress_hooks': [
                progress_hook
            ],

            'postprocessor_hooks': [
                postprocessor_hook
            ],

            'concurrent_fragment_downloads': 4,

            'http_headers': self.download_headers(domain, cookie_text)
        }

        ffmpeg_path = find_ffmpeg()

        if ffmpeg_path:
            ydl_opts['ffmpeg_location'] = ffmpeg_path
        else:
            # Without ffmpeg, separate video+audio streams can't be
            # merged, so every such download used to fail at the very
            # end. Ask for the best single file that already contains
            # both instead (lower quality on some sites, but it works).
            ydl_opts['format'] = 'best'
            ydl_opts.pop('merge_output_format', None)

        if cookie_stream is not None:
            ydl_opts['cookiefile'] = cookie_stream

        # =============================================
        # INFO + DOWNLOAD (with image fallback)
        # =============================================

        title = "Unknown"

        res = "NA"

        video_uid = None

        output_filename = None

        size_text = None

        output_path = None

        try:

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:

                info = None

                processed = True

                try:

                    info = ydl.extract_info(
                        url_clean,
                        download=False
                    )

                except yt_dlp.utils.DownloadError as inner_e:

                    # Some posts (single images / carousels) have no
                    # playable video formats at all, and yt-dlp raises
                    # this *during* extraction rather than returning
                    # metadata. Re-extract without format processing so
                    # we can still get whatever image URLs it found.
                    err_lower = str(inner_e).lower()

                    # "Unsupported URL": yt-dlp has no extractor for
                    # this page at all (e.g. TikTok /photo/ posts).
                    # gallery-dl may still handle it, so treat it like
                    # a no-video post instead of failing outright.
                    unsupported = "unsupported url" in err_lower

                    if "no video formats" in err_lower or unsupported:

                        processed = False

                        if domain != "facebook.com" and not unsupported:

                            # Facebook just opens in the browser when
                            # this happens (see below) -- no need to
                            # spend a second request getting entries
                            # we won't use.
                            try:

                                info = ydl.extract_info(
                                    url_clean,
                                    download=False,
                                    process=False
                                )

                            except yt_dlp.utils.DownloadError:

                                info = None

                    else:

                        raise

                entries = []

                uploader = None

                if info:

                    title = info.get('title') or info.get('id') or 'Unknown'

                    res = info.get('resolution', '?')

                    uploader = (
                        info.get('uploader')
                        or info.get('channel')
                        or info.get('uploader_id')
                    )

                    entries = [
                        e for e in (info.get('entries') or [info]) if e
                    ]

                    vid_id = info.get('id')

                    extractor_name = info.get('extractor_key') or info.get('extractor')

                    if vid_id and extractor_name:

                        video_uid = f"{extractor_name}:{vid_id}"

                        if row in self.row_records:
                            self.row_records[row]["video_uid"] = video_uid

                signals.title.emit(row, title)

                signals.resolution.emit(row, res)

                signals.user.emit(row, uploader)

                # Second, more reliable dedupe check: yt-dlp's own
                # extractor+id is stable regardless of which URL
                # variant (short link, tracking params) pointed here,
                # unlike matching on the URL string itself.
                if video_uid and video_uid in COMPLETED_IDS:

                    self.record_skipped_duplicate(
                        row, url_original, url_clean, video_uid
                    )

                    return

                if not processed:

                    if domain == "facebook.com":

                        self.try_facebook_image(
                            row, url_original, url_clean, domain,
                            cookie_text, title, res
                        )

                        return

                    # yt-dlp couldn't extract this post at all (raised
                    # "no video formats" during extraction itself).
                    cascade_ok, output_path, output_filename, size_text = (
                        self.run_image_fallback_cascade(
                            row, url_clean, domain, cookie_text,
                            title, ts, entries
                        )
                    )

                    if not cascade_ok:

                        raise RuntimeError(
                            "No video or image content could be found "
                            "for this URL with any available method."
                        )

                elif domain == "facebook.com":

                    # Facebook specifically: it sometimes only reveals
                    # there's no playable video at actual download
                    # time, not during extraction, so this one site
                    # gets the extra download-time catch too.
                    try:

                        output_path = self.download_from_info(
                            ydl, info, url_clean, final_output_path
                        )

                        _path, output_filename, size_text = self.finalize_output(
                            row, output_path
                        )

                    except yt_dlp.utils.DownloadError as dl_e:

                        if "no video formats" not in str(dl_e).lower():
                            raise

                        self.try_facebook_image(
                            row, url_original, url_clean, domain,
                            cookie_text, title, res
                        )

                        return

                else:

                    output_path = self.download_from_info(
                        ydl, info, url_clean, final_output_path
                    )

                    _path, output_filename, size_text = self.finalize_output(
                        row, output_path
                    )

        except yt_dlp.utils.DownloadError as e:

            err_str = str(e).lower()

            # The site refused the download WITH the saved cookies
            # (stale/rejected session). Public posts don't need them,
            # so try once more without instead of failing everything
            # for that site until the cookies are cleared.
            if (
                cookie_text and not _without_cookies
                and ("http error 403" in err_str or "forbidden" in err_str)
            ):

                signals.log.emit(
                    f"{domain} rejected the download with your saved "
                    "cookies (HTTP 403) -- retrying without them."
                )

                return self.download_video(
                    row, url_original, url_clean,
                    _retried=_retried, _without_cookies=True
                )

            needs_login = looks_like_auth_error(err_str)

            if needs_login and not _retried:

                signals.log.emit(
                    f"{domain} needs sign-in/verification. "
                    "Opening browser window..."
                )

                signals.status.emit(row, "Waiting for login")

                got_session = self.request_login_and_wait(
                    domain, url_clean
                )

                if got_session:

                    return self.download_video(
                        row, url_original, url_clean, _retried=True
                    )

            self.record_failure(row, url_original, url_clean, domain, e)

            return

        except Exception as e:

            self.record_failure(row, url_original, url_clean, domain, e)

            return

        # Only after a SUCCESSFUL download: cookies the site sent along
        # with an error (e.g. a 403) must never replace a good session.
        self.persist_refreshed_cookies(domain, cookie_text, cookie_stream)

        if _without_cookies:

            signals.log.emit(
                f"Downloaded without your saved {domain} cookies. If "
                "this keeps happening, CLEAR that site in MANAGE COOKIES "
                "and sign in again with LOGIN & CAPTURE."
            )

        self.record_success(
            row, url_original, url_clean, domain, title, res, video_uid,
            output_filename=output_filename, size_text=size_text,
            output_path=output_path, uploader=uploader
        )

    @staticmethod
    def cookie_user_agent(domain, cookie_text):
        """The User-Agent to pair with this site's saved cookies, or
        None to use the default. Only applies while cookies are sent."""

        return get_site_user_agent(domain) if cookie_text else None

    def download_headers(self, domain, cookie_text):

        headers = browser_headers()

        user_agent = self.cookie_user_agent(domain, cookie_text)

        if user_agent:
            headers["User-Agent"] = user_agent

        return headers

    @staticmethod
    def persist_refreshed_cookies(domain, original_text, cookie_stream):
        """Sites often refresh session cookies while you use them, and
        yt-dlp writes the refreshed set back to its cookie stream when
        it finishes. Save that back (encrypted again if protected) so
        sessions don't go stale -- unless the stored cookies changed in
        the meantime (e.g. you just signed in again), in which case the
        newer sign-in wins."""

        if cookie_stream is None:
            return

        try:
            new_text = cookie_stream.getvalue().replace("\x00", "")
        except ValueError:
            return  # stream was closed

        if new_text == original_text or not any(parse_netscape_cookies(new_text)):
            return

        try:

            with _cookie_io_lock:

                if read_site_cookies(domain) != original_text:
                    return

                save_site_cookies(domain, new_text)

        except (OSError, CookieVaultLocked, CookieVaultError):
            pass

    @staticmethod
    def download_from_info(ydl, info, url_clean, final_output_path):
        """Downloads using the metadata extract_info() already fetched.
        ydl.download([url]) would make yt-dlp load and parse the page
        a second time -- twice the requests per video, which is what
        tends to trip rate limits. Returns the final file path."""

        if info:
            result = ydl.process_ie_result(info, download=True)
        else:
            result = ydl.extract_info(url_clean, download=True)

        if final_output_path[0]:
            return final_output_path[0]

        # Fallback if the post-processor hook didn't report a path.
        downloads = (result or {}).get("requested_downloads") or []

        for d in downloads:
            if d.get("filepath"):
                return d["filepath"]

        return None

    def run_image_fallback_cascade(
        self, row, url_clean, domain, cookie_text, title, ts, entries
    ):
        """Image fallback for posts yt-dlp couldn't extract a video
        from. Used for Instagram/TikTok/etc; Facebook bypasses this
        entirely and just opens in the system browser instead (see
        download_video) since gallery-dl's Facebook support is
        limited and browser-automation attempts here didn't hold up
        against Facebook's anti-bot/signed-URL checks. Returns
        (handled, output_path, output_filename, size_text) -- the
        latter three None if nothing was saved."""

        # Decide image-vs-video by file extension, NOT by whether
        # 'formats' is populated -- some extractors resolve to a
        # single format without ever filling 'formats'.
        all_images = bool(entries) and all(
            guess_ext(e) in IMAGE_EXTENSIONS for e in entries
        )

        has_image_urls = any(
            e.get('url') or e.get('thumbnails') for e in entries
        )

        if all_images and has_image_urls:

            # Layer 1: yt-dlp already gave us clean image URLs -- use
            # those directly, no extra tool needed.
            output_path, output_filename, size_text = self.download_image_entries(
                row, entries, title, ts, cookie_text,
                self.cookie_user_agent(domain, cookie_text)
            )

            return True, output_path, output_filename, size_text

        handled = False

        output_path = None

        output_filename = None

        size_text = None

        # Layer 2: gallery-dl -- a dedicated, actively maintained
        # image/gallery downloader with proper Instagram/TikTok
        # support (full resolution, full carousels), using the same
        # cookies.txt we already have from the login flow.
        if check_gallery_dl_available():

            try:

                files = run_gallery_dl(
                    url_clean, self.output_dir, cookie_text,
                    self.cookie_user_agent(domain, cookie_text)
                )

                shown_folder = os.path.dirname(files[0])

                signals.log.emit(
                    f"Downloaded via gallery-dl "
                    f"({len(files)} file(s)) to:\n{shown_folder}"
                )

                output_target = files[0] if len(files) == 1 else shown_folder

                output_path, output_filename, size_text = self.finalize_output(
                    row, output_target
                )

                signals.progress.emit(row, 100)

                handled = True

            except RuntimeError as gdl_e:

                signals.log.emit(
                    f"gallery-dl fallback failed:\n{gdl_e}"
                )

        else:

            signals.log.emit(
                "gallery-dl not installed -- skipping that "
                "fallback (pip install gallery-dl to enable "
                "it for higher-quality image/gallery posts)."
            )

        # Layer 3: last resort -- the page's own Open Graph / Twitter
        # Card preview tags via a plain HTTP request. Usually yields
        # only ONE image, so carousels may be incomplete this way.
        # Only for the social sites the preview-tag trick is meant for:
        # on an arbitrary page it would "succeed" by saving whatever
        # share-preview image the page has (a logo, an article photo).
        if not handled and domain not in SUPPORTED_DOMAINS:

            signals.log.emit(
                "No video or gallery found on this page (the page-"
                "preview fallback only applies to social media posts)."
            )

        elif not handled:

            session = build_requests_session(
                cookie_text, self.cookie_user_agent(domain, cookie_text)
            )

            try:

                scraped_urls = scrape_images_from_page(
                    url_clean, session
                )

            except requests.RequestException as scrape_e:

                signals.log.emit(
                    f"Page-scrape fallback failed:\n{scrape_e}"
                )

                scraped_urls = []

            if scraped_urls:

                if len(scraped_urls) == 1:

                    signals.log.emit(
                        "Note: only a single preview image "
                        "could be found for this post "
                        "(carousels may not be fully "
                        "retrievable this way)."
                    )

                output_path, output_filename, size_text = self.download_direct_image_urls(
                    row, scraped_urls, title, ts, session
                )

                handled = True

        return handled, output_path, output_filename, size_text

    def download_direct_image_urls(self, row, urls, title, ts, session):
        """Same idea as download_image_entries but for raw URLs found
        via the Open Graph/Twitter Card scraping fallback rather than
        yt-dlp entries."""

        safe_title = safe_filename(title)

        if len(urls) > 1:

            folder = os.path.join(
                self.output_dir, f"{safe_title}_{ts}"
            )

            os.makedirs(folder, exist_ok=True)

        else:

            folder = self.output_dir

        total = len(urls)

        saved_any = False

        last_dest_path = None

        for idx, img_url in enumerate(urls):

            path = urllib.parse.urlparse(img_url).path

            ext = os.path.splitext(path)[1].lstrip('.').lower() or 'jpg'

            if len(urls) > 1:
                filename = f"{idx + 1:02d}.{ext}"
            else:
                filename = f"{safe_title}_{ts}.{ext}"

            dest_path = os.path.join(folder, filename)

            try:

                # "with" closes the connection even if writing fails;
                # iter_content (unlike resp.raw) also undoes any gzip
                # transfer encoding the server applied.
                with session.get(img_url, stream=True, timeout=20) as resp:

                    resp.raise_for_status()

                    with open(dest_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            f.write(chunk)

                saved_any = True

                last_dest_path = dest_path

            except requests.RequestException as e:

                signals.log.emit(f"Image failed ({filename}):\n{e}")

            signals.progress.emit(
                row, int((idx + 1) / total * 100)
            )

        if not saved_any:
            raise RuntimeError("No images could be downloaded from this post.")

        return self.finalize_output(
            row, folder if len(urls) > 1 else last_dest_path
        )

    def download_image_entries(
        self, row, entries, title, ts, cookie_text, user_agent=None
    ):
        """Download a single image or a carousel/gallery post using the
        URLs yt-dlp already extracted, over an authenticated session."""

        session = build_requests_session(cookie_text, user_agent)

        safe_title = safe_filename(title)

        image_entries = [e for e in entries if e]

        if len(image_entries) > 1:

            folder = os.path.join(
                self.output_dir, f"{safe_title}_{ts}"
            )

            os.makedirs(folder, exist_ok=True)

        else:

            folder = self.output_dir

        total = len(image_entries) or 1

        saved_any = False

        last_dest_path = None

        for idx, entry in enumerate(image_entries):

            img_url = entry.get('url')

            if not img_url:

                thumbs = entry.get('thumbnails') or []

                if thumbs:
                    img_url = thumbs[-1].get('url')

            if not img_url:
                continue

            ext = guess_ext(entry) or 'jpg'

            if len(image_entries) > 1:
                filename = f"{idx + 1:02d}.{ext}"
            else:
                filename = f"{safe_title}_{ts}.{ext}"

            dest_path = os.path.join(folder, filename)

            try:

                # "with" closes the connection even if writing fails;
                # iter_content (unlike resp.raw) also undoes any gzip
                # transfer encoding the server applied.
                with session.get(img_url, stream=True, timeout=20) as resp:

                    resp.raise_for_status()

                    with open(dest_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=65536):
                            f.write(chunk)

                saved_any = True

                last_dest_path = dest_path

            except requests.RequestException as e:

                signals.log.emit(f"Image failed ({filename}):\n{e}")

            signals.progress.emit(
                row, int((idx + 1) / total * 100)
            )

        if not saved_any:
            raise RuntimeError("No images could be downloaded from this post.")

        return self.finalize_output(
            row, folder if len(image_entries) > 1 else last_dest_path
        )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    # A packaged .exe re-launches itself with this flag to run
    # gallery-dl (see gallery_dl_command) -- do that and exit, without
    # ever opening the GUI.
    if len(sys.argv) > 1 and sys.argv[1] == GALLERY_DL_FLAG:
        sys.exit(run_bundled_gallery_dl(sys.argv[2:]))

    app = QApplication(sys.argv)

    if qdarktheme is not None:
        try:
            app.setStyleSheet(qdarktheme.load_stylesheet())
        except Exception as e:
            print(f"Theme failed to load: {e}", file=sys.stderr)

    window = MainWindow()

    window.show()

    sys.exit(app.exec())
