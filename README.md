# Modern Video Downloader Lite

A lighter variant of **Modern Video Downloader**: the same app, but it
signs in through the Microsoft Edge (or Google Chrome) already installed
on your PC instead of a browser built into the app, so the packaged
`.exe` is far smaller. It keeps its own settings and cookies, so both
versions can sit in the same folder without affecting each other.

A Windows desktop tool for downloading videos and photo posts from links
you paste, copy, or import from a file — from any site
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports, with
[gallery-dl](https://github.com/mikf/gallery-dl) as a fallback for image
and gallery posts. Adds a queue with per-site download limits, link
cleanup, duplicate detection, a searchable download history, and a
built-in update checker, all in a PySide6 GUI.

Built with the assistance of Claude (Anthropic).

## Purpose & legitimate use

This tool downloads content **you can already view** in a normal browser —
public posts, or posts visible to your own logged-in account. When a site
asks for a login or a verification check, the tool opens your own Edge
or Chrome in a separate window and **you** sign in or complete the check
yourself; it only saves the cookies that session produces so the download can continue. It
never fills in credentials, solves CAPTCHAs, or automates any verification
step on its own, and it doesn't get around DRM or paywalls.

The link cleanup described below only rewrites a link into the standard
form of the same post (following share/redirect links, removing tracking
parameters); it doesn't change what you have access to. The last-resort
image fallback reads a page's standard Open Graph / Twitter Card preview
tags — the same tags any messaging app reads to build a link preview.

What you download and what you do with it is still subject to each site's
terms of service and to the rights of whoever made the content — that's
between you and them, same as saving anything else you find online. This
is a personal archiving utility, not a tool for redistributing other
people's work.

## Requirements

- Windows 10 or 11 (the code has some macOS/Linux handling for opening
  files and folders, but it's built and tested for Windows).
- Python 3.9+.
- Python packages:

  ```
  pip install PySide6-Essentials yt-dlp gallery-dl requests pyqtdarktheme websockets
  ```

  - Install `PySide6-Essentials`, **not** plain `pyside6`: that one also
    installs `PySide6-Addons` (QtWebEngine and more), which this version
    doesn't use and which is most of the original app's size.
  - `websockets` lets the app talk to the sign-in browser for **Login &
    Capture**. Without it the app still runs; only signing in is
    unavailable.
  - `gallery-dl` is optional but strongly recommended for photo and
    gallery posts.
  - `pyqtdarktheme` provides the dark theme (`import qdarktheme`).
- **Microsoft Edge** (included with Windows) or **Google Chrome**, for
  **Login & Capture**. Edge is used when both are installed.
- **FFmpeg** — needed to merge separate video and audio streams, which most
  sites use for anything above low resolution. The "essentials" build from
  [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) is enough. Either add its
  `bin` folder to your PATH, or use **Set FFmpeg Path** in the app and point
  it at `ffmpeg.exe`. Without FFmpeg, downloads still work, but fall back
  to the best single file that already has both video and audio (lower
  quality on some sites).

## Running it

```
python video_downloader_lite.py
```

Settings, logs, and cookies are kept in an `app_data_lite` folder created
next to the script (or next to the `.exe` in a built version) (see **Files it keeps** below). Downloads go to
`Downloads\Videos` in your user folder by default; **Select Folder** changes
that, and the choice is remembered across restarts.

## Adding downloads

- **Paste** one or more URLs (one per line) and click **Add to Queue**.
  Any `http`/`https` link is accepted — yt-dlp's generic extractor can often
  find a video even on sites it has no dedicated support for.
- **Import from File** — a `.txt` file with one URL per line, accepted the
  same way.
- **Auto Add** (on by default) — watches the clipboard and queues a copied
  link if yt-dlp has dedicated support for its site, or if you've set that
  site up in **Manage Cookies** or **Site Settings**. It's deliberately
  stricter than the paste box, so copying ordinary links doesn't queue them.

Hundreds of links can be pasted or imported at once: they're added to the
list in small batches, so the window stays responsive while they load.

## Link cleanup

Before anything is queued, each link is turned into the standard form of
the post it points to. This makes more links download successfully and
lets the duplicate check recognize the same post shared in different ways:

- **Share/short links** are followed to the real post (`vm.tiktok.com`,
  `tiktok.com/t/`, `fb.watch`, Facebook `/share/`, Instagram `/share/`,
  Reddit `/s/` links, `t.co`, `bit.ly`, `pin.it`, and others). This runs in
  the background, so a slow link never freezes the window; the Log shows
  "Resolving short link…" meanwhile.
- **Redirect wrappers** are unwrapped without any network request —
  `l.facebook.com/l.php?u=…`, `l.instagram.com`, `google.com/url?q=…`,
  YouTube `/redirect`, `out.reddit.com`, and Google AMP links.
- **Mirror, embed-fixer and mobile domains** become the real site:
  `twitter.com`, `fxtwitter.com`, `vxtwitter.com`, `fixupx.com` → `x.com`;
  `ddinstagram.com` → `instagram.com`; `vxtiktok.com` → `tiktok.com`;
  `m.`/`mbasic.`/`web.` Facebook, `m.`/`music.` YouTube, and
  `old.`/`new.` Reddit.
- **Alternate URL forms** are normalized: `youtu.be/ID`, YouTube
  `/live/ID`, `/embed/ID` and `youtube-nocookie.com` → `watch?v=ID`;
  Instagram `/<user>/reel/ID` and `/reels/ID` → `/reel/ID`.
- **Tracking parameters** (`utm_*`, `fbclid`, `igsh`, `si`, and similar) are
  removed, while every other parameter is kept — for many sites the query
  *is* the video (`facebook.com/watch/?v=…`, Bilibili `?p=2`).

## How downloads are handled

Videos are downloaded at the best available quality and merged to `.mp4`,
saved as `<uploader>_<YYYY-MM-DD_HH-MM-SS>_<video id>.mp4` (sites that
don't report an uploader use the channel or site name instead). File and
folder names are always made safe for Windows: line breaks from captions
and characters Windows doesn't allow (`\ / : * ? " < > |`) are replaced,
trailing dots and spaces are removed, reserved names like `CON` or `NUL`
are avoided, and names are shortened so the full path stays within
Windows' length limit. Emoji and non-Latin letters are kept. When a post turns out to
be photos rather than a video — or yt-dlp doesn't support the page at all —
the tool falls back in order:

1. **Image URLs yt-dlp already found** — downloaded directly.
2. **gallery-dl** — full-resolution images and full carousels, using the
   same saved cookies.
3. **Page preview tags** — for YouTube, TikTok, Instagram, Facebook and X
   posts only. Usually just one image, so a carousel may come back
   incomplete this way (the Log says so when it happens).

Multi-image posts are saved into their own subfolder. **Facebook** photo
posts try gallery-dl and, if that fails, open in your normal browser for you
to save manually — those rows show **Opened in Browser** instead of
Completed.

## Sign-in, cookies & site settings

- **Manage Cookies** — one row per site, and **Add Site** for any other.
  There are two ways to give the app a site's sign-in:
  - **Login & Capture** opens Edge (or Chrome) in its own window, with a
    separate profile for that site, plus a small always-on-top window
    with **Start Fresh**, **Save Session & Close** and **Cancel**. Sign in
    in the browser, then **Save Session & Close** saves that site's
    cookies and closes the browser.
  - **Browse…** uses a `cookies.txt` you already have.

  The list shows each site's cookie file name and whether it's encrypted
  or plain text; hover a name to see its full location. Imported cookies
  are stored encrypted when protection is on. **Clear** forgets a site's sign-in. The
  sign-in browser opens with your saved cookies already loaded, so you stay
  signed in there between uses.
- The app picks up the browser's cookies every couple of seconds while it's
  open, so if you close the browser window first, **Save Session & Close**
  still saves what was captured.
- To sign in with a different Chromium-based browser, add
  `"login_browser_path": "C:\\path\\to\\browser.exe"` to
  `app_data_lite\config.json`.
- If the sign-in browser "closed right away", a sign-in browser for that
  site is probably still open from before; close it and try again.
- Downloads that use a site's saved cookies present the same browser
  identity (User-Agent) as the sign-in browser that created them. Some
  sites, TikTok in particular, reject a session that arrives from a
  different "browser".
- If a site refuses the sign-in browser itself (for example TikTok's
  "Access denied / HTTP ERROR 403", which comes and goes), the control
  window says so and won't save that session by default. **Start Fresh**
  clears the sign-in browser's cookies and cache for the site and reloads
  for a clean attempt — the cookies from a blocked page
  would make every download from the site fail too.
- If a download fails with something that looks like a login wall, rate
  limit, or verification check, the tool pauses that download, opens the
  sign-in browser for you, and retries once after you save the session.
- If a site refuses a download *with* the saved cookies (HTTP 403), it's
  retried once without them — public posts don't need them — and the Log
  suggests clearing and re-capturing that site's sign-in if it keeps
  happening. Refreshed cookies are only saved back after a successful
  download, so a rejected request can't overwrite a good session.
- **Site Settings** — per-site **Max Concurrent** downloads and a **Delay**
  between downloads, with an optional **Random** mode that treats the
  delay as a maximum rather than a fixed wait. **Delay From** sets what
  the delay counts from: **Start** (when the previous download for that
  site started) or **Finish** (when it completed — a real pause between
  downloads, best with Max Concurrent 1). A row waiting out its delay
  shows how long is left. By default each site downloads
  **one at a time** (different sites still run in parallel), since several
  simultaneous requests to one site is what tends to trigger rate limits.
  The `_default` row applies to any site without its own row, and **Apply
  to All Rows** sets every row at once.

## Protecting your saved sign-ins

Saved cookies are as good as your logged-in session: anyone who gets a
copy can use your account on that site. **Protection** in Manage Cookies
chooses how they're stored:

- **Plain text (not protected)** — ordinary `cookies.txt` files. Simple,
  but readable by anything that can open them.
- **Protected by your Windows account** — encrypted with Windows' own
  data protection (DPAPI, the same system behind saved Wi-Fi and
  Credential Manager passwords). Only your Windows account on this PC can
  decrypt them, so a copied, synced, or backed-up file is useless anywhere
  else. Nothing to type.
- **Windows account + master password** — the same, plus a password
  you enter once each time the app starts. Even your own Windows account
  can't read the cookies until it's entered. **Lock Now** forgets the
  password until you enter it again; **Change Password** re-encrypts
  everything with a new one. Skipping the password prompt is fine —
  downloads just run without saved cookies until you unlock them.

Switching modes converts every saved cookie file right away. When turning
protection on, the app offers to delete any plain-text `cookies.txt` you
had picked from elsewhere, and the sign-in browser's saved profiles
(its own unencrypted copy of your sessions). While protection is on, the
sign-in browser uses a temporary profile that's deleted as soon as it
closes, and cookies are handed to yt-dlp directly in memory; gallery-dl
needs a file, so it gets a temporary copy that's deleted as soon as it
finishes.

The master password can't be recovered. **Forgot Password…** on the
unlock prompt deletes the protected cookies and switches to Windows-account
protection, so you sign in to those sites again.

No local encryption stops malware that's already running as you while the
cookies are unlocked — protection is about copies of the files, other
accounts, backups, and syncing.

## The download list

Each download is a row showing its status, title, progress, resolution,
size, speed, uploader, output file, and URL. **Speed** shows the current
download speed and keeps the last reading once the download finishes (it's
also remembered in the history). A **?** means a value isn't known yet
(the download hasn't started); once a download is running or done, any
value the site didn't provide shows **NA** instead. Right-click a row for **Open File**,
**Show in Folder**, **Copy URL**, **Open in Browser**, **Retry**,
**Redownload**, **Delete File**, and **Remove Record**.

- **Retry** re-queues a failed download in the same row.
- **Redownload** asks whether to delete the previous file first (default:
  yes) and whether to keep the old row as a record and start a new one
  (default: no, reuse the row).
- **Delete File** removes the file from disk but keeps the row as a record
  that it was downloaded, unless you tick the option to remove the row too.
- **Remove Record** / **Clear List** only tidy up the table — they never
  delete files or touch the log.
- Tick the **Sel** checkboxes (or the one in the column header to select
  everything) and use **Bulk** to Remove Files, Remove Record, Redownload,
  or Retry several rows at once.

Columns can be sorted by clicking their header; **Restore Order** puts them
back in the order they were added. The search box filters rows (across all
columns or just one) and highlights matches, and clicking any cell shows its
full text in the box underneath. The Log below has its own search, with
color-coded lines for completed, failed, and skipped downloads.

## Duplicates & history

Every completed download is recorded in `download_log.jsonl`, and the tool
won't download the same thing twice. It checks both the cleaned link and
yt-dlp's own site+video ID, so the same video arriving through a different
share link, mirror domain, or tracking URL is still caught. Pasting
something you already have adds it to the list as a **Completed** row (with
its original title, size, and file — or `(missing)` if the file has since
been moved or deleted), so **Redownload** is right there if you want it
again.

Failed URLs are collected in `failed_downloads.txt`, and removed from it
automatically once they succeed — handy for re-importing everything that
didn't work in one go via **Import from File**.

## Checking for updates

Sites change often, and most "this link stopped working" problems are fixed
by a yt-dlp or gallery-dl update. **Check for Updates** compares what's
installed with the latest releases of yt-dlp, gallery-dl, PySide6
(updated through `PySide6-Essentials`), requests, pyqtdarktheme,
websockets, and FFmpeg, and ticks everything that's out
of date. "Latest" means the newest version that installs on the Python
you're running — some packages' newest releases only support certain
Python versions, and those are shown as a note instead of an update that
could never install.

- **gallery-dl** runs as a separate program for each download, so updating
  it takes effect immediately.
- **yt-dlp, PySide6, requests, websockets and the dark theme** are loaded while the app
  runs, so they need a restart — which the app does for you: it closes,
  a small helper waits for it to exit, runs pip, and reopens the app, which
  then reports in the Log whether the update worked (or shows pip's error if
  it didn't). This usually takes under a minute.
- **If downloads are still running**, you choose: **When Downloads Finish**
  (the default) updates and restarts on its own once the list is idle,
  while **Restart Now** adds unfinished links back automatically after the
  restart — ones that were mid-download start over from the beginning.
- **FFmpeg** isn't installed through pip, so it's only reported; **FFmpeg
  Download Page** opens gyan.dev to get the new build.

In a built (PyInstaller) version of the app, the Python packages are
frozen inside it, so the dialog only reports what's out of date: update
them with pip and rebuild the app. FFmpeg is updated the same way in both.

A quiet check also runs at startup at most once a day and only writes a
note in the Log when something is out of date — you can turn it off in the
same dialog.

## Files it keeps

All inside `app_data_lite` next to the script or `.exe` (**Open Logs Folder** opens it):

- `config.json` — download folder, FFmpeg path, site settings, cookie
  protection mode, and update check preferences. In master-password mode it
  holds a random salt and a password check value — never the password.
- `download_log.jsonl` — one line per completed/failed download.
- `failed_downloads.txt` — URLs that haven't succeeded yet.
- `cookies\` — one file per site (`<site>.txt` when plain,
  `<site>.cookies.bin` when protected), plus `sites.json` mapping sites to
  their cookie files.
- `browser_profiles\` — the sign-in browser's own profile per site, used
  only while protection is off.
- `update_helper.py`, `update_result.json`, `resume_after_update.txt` —
  used during **Update & Restart**; the last two are removed once the app
  has reopened and read them.

**Clear Log** only clears the on-screen Log; the files on disk are never
touched by it.

## Building a standalone .exe

See `BUILD_INSTRUCTIONS_lite.txt`. Build in its own virtual environment
with only `PySide6-Essentials` installed; otherwise the build picks up the
large Qt parts of the original app and loses the size advantage.

## Known limitations

A few are inherent to the sites themselves rather than bugs in this tool:

- **Sites change often.** When a site changes its layout or API, downloads
  from it can break until yt-dlp or gallery-dl catches up — **Check for
  Updates** is the first thing to try.
- **Sites without dedicated support** depend on yt-dlp's generic extractor,
  which finds ordinary embedded videos but not everything.
- **Photo carousels** may come back as just the cover image when only the
  page-preview fallback works — installing gallery-dl avoids this for most
  posts.
- **Facebook photo posts** frequently can't be downloaded automatically and
  open in your browser instead.
- **No cancel button.** Removing an in-progress row from the list hides it
  but doesn't stop the download.
- **Lowering Max Concurrent** takes full effect after a restart; workers
  that are already running for that site stay until then (raising it
  applies immediately).
- **Restart Now** during an update restarts in-progress downloads from the
  beginning, and their partial `.part` files are left in the download
  folder.
- **Plain-text mode is the default** for compatibility. Until you turn on
  protection, anyone with a site's `cookies.txt` can act as your logged-in
  account on that site.
- **Cookie protection is Windows-only** (it relies on Windows' own
  encryption); on other systems only plain text is available.
- **Signing in needs Edge or Chrome.** The sign-in happens in a separate
  browser window rather than inside the app.
- **Cookies aren't shared with the original app.** Each version keeps its
  own, so sign in once in each.

## License

GPL-3.0. See the `LICENSE` file (or add one from
[gnu.org](https://www.gnu.org/licenses/gpl-3.0.txt) if you haven't yet) —
note that GPL-3.0 is copyleft: anything you build on top of this and
distribute must also be released under GPL-3.0.
