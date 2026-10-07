"""Weekly LFS update check -- the scheduler's `lfs_update` job kind
(lfs-os-Phased-Implementation.md, A5).

Compares what the LFS OS build is pinned to (lfs/manifest.json) with what the
LFS project and kernel.org publish now, and reports what changed and why it
matters. It never downloads or replaces anything: moving to a new version is
a decision, made by re-running lfs/build-manifest.py and api/lfs-mirror.py.

Checked:
  - a new stable LFS or BLFS release (systemd editions);
  - the LFS errata page for the pinned version, and the LFS and BLFS security
    advisory indexes: any change since the last run is reported, with what
    changed (an advisory for a package we build matters most);
  - the kernel: a new latest stable; a newer point release in the pinned
    series (usually fixes, often security ones); the pinned series reaching
    end of life.
Not checked yet: newer upstream versions of individual packages -- an open
decision in the plan.

What was seen last time is kept in ~/.local/share/cloudcore/lfs-update-state.json,
so "changed" means changed since the previous run; the first run records a
baseline. Returns (status, summary, log) like the other job kinds:
"success" when nothing is new, "attention" when something is.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
MANIFEST = API_DIR.parent / "lfs" / "manifest.json"
STATE = Path.home() / ".local" / "share" / "cloudcore" / "lfs-update-state.json"
LFS = "https://www.linuxfromscratch.org/lfs"
BLFS = "https://www.linuxfromscratch.org/blfs"
KERNEL_RELEASES = "https://www.kernel.org/releases.json"


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "cloudcore-lfs-update/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def _text(page: str) -> str:
    """A page's readable text, without the site's template and footer, for change detection."""
    body = re.sub(r"(?s)<!--.*?-->|<script.*?</script>|<style.*?</style>", " ", page)
    h1 = re.search(r"(?i)<h1\b", body)  # the content starts at its heading, after the site's menus
    body = body[h1.start():] if h1 else body
    txt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))).strip()
    return re.sub(r"©.*$", "", txt).strip()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _watch(state: dict, key: str, text: str, label: str, log: list[str], news: list[str]) -> None:
    """Report a page whose readable text changed since the last run."""
    prev = state.get(key)
    now = {"digest": _digest(text), "text": text[:4000]}
    if prev is None:
        log.append(f"{label}: baseline recorded")
    elif prev["digest"] != now["digest"]:
        old, new = set(re.split(r"(?<=[.!?])\s+", prev.get("text", ""))), re.split(r"(?<=[.!?])\s+", text[:4000])
        added = [s for s in new if s and s not in old][:5]
        news.append(f"{label} changed" + (": " + " | ".join(a[:160] for a in added) if added else ""))
    else:
        log.append(f"{label}: unchanged")
    state[key] = now


def run(var_overrides: dict) -> tuple[str, str, list[str]]:
    log: list[str] = []
    news: list[str] = []
    try:
        pinned = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as e:
        return "failed", f"can't read {MANIFEST}: {e}", [str(e)]
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    lfs_v, blfs_v, kern = pinned["lfs_version"], pinned["blfs_version"], pinned["kernel"]
    log.append(f"pinned: LFS {lfs_v}, BLFS {blfs_v} (systemd), kernel {kern}")
    try:
        # New releases.
        listing = _fetch(f"{LFS}/downloads/stable-systemd/")
        cur_lfs = re.search(r"LFS-BOOK-([\d.]+)\.tar\.xz", listing).group(1)
        cur_blfs = re.search(r"Version ([\d.]+)", _text(_fetch(f"{BLFS}/view/stable-systemd/index.html"))).group(1)
        for name, have, now in (("LFS", lfs_v, cur_lfs), ("BLFS", blfs_v, cur_blfs)):
            if now != have:
                news.append(f"{name} {now} is out (pinned: {have}). To move: re-run lfs/build-manifest.py, "
                            "then api/lfs-mirror.py, and review the build against the new book")
            else:
                log.append(f"{name}: {have} is still the current stable release")
        # Errata and advisories.
        _watch(state, f"lfs_errata_{lfs_v}", _text(_fetch(f"{LFS}/errata/{lfs_v}-systemd/")),
               f"LFS {lfs_v} errata", log, news)
        for name, base in (("LFS", LFS), ("BLFS", BLFS)):
            idx = _fetch(f"{base}/advisories/")
            _watch(state, f"{name.lower()}_advisories_index", _text(idx), f"{name} advisories index", log, news)
            page = f"{base}/advisories/{lfs_v if name == 'LFS' else blfs_v}.html"
            try:
                _watch(state, f"{name.lower()}_advisories_{lfs_v}", _text(_fetch(page)),
                       f"{name} advisories for {lfs_v if name == 'LFS' else blfs_v}", log, news)
            except urllib.error.HTTPError as e:
                # Seen 2026-10-07: LFS's own index links 13.1.html, which returns 404.
                log.append(f"{name} advisories page {page}: HTTP {e.code} (not published yet)")
        # The kernel.
        rel = json.loads(_fetch(KERNEL_RELEASES))
        latest = rel["latest_stable"]["version"]
        series = ".".join(kern.split(".")[:2])
        same = [r for r in rel["releases"] if r["version"].startswith(series + ".") or r["version"] == series]
        if latest != kern:
            news.append(f"kernel {latest} is the latest stable (pinned: {kern})")
        else:
            log.append(f"kernel: {kern} is still the latest stable")
        if same and same[0]["version"] != kern:
            news.append(f"kernel {same[0]['version']} is newer in the pinned {series} series: point releases "
                        "are mostly fixes, often security ones")
        if same and same[0].get("iseol"):
            news.append(f"kernel {series} has reached end of life: move to a maintained series")
        elif not same:
            news.append(f"kernel {series} is no longer listed by kernel.org: it is likely end of life")
    except (urllib.error.URLError, TimeoutError, AttributeError, KeyError, ValueError) as e:
        return "failed", f"check failed: {type(e).__name__}: {e}", log + [str(e)]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)
    if news:
        return "attention", "; ".join(news), log + ["NEW: " + n for n in news]
    return "success", f"nothing new for LFS {lfs_v} / BLFS {blfs_v} / kernel {kern}", log
