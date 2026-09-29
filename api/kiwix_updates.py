"""Weekly kiwix ZIM update check -- the scheduler's `kiwix_update` job kind.

For every ZIM in examples/llm-chat/kiwix-zims.json that came from Kiwix, look
up the same book and flavour in Kiwix's live OPDS catalog. When a newer release
exists AND it has grown meaningfully (min_growth_pct or min_growth_mb, from the
schedule's var_overrides), download it into the host artifact cache, verify it
against Kiwix's published sha256, point the manifest at it, and commit + push
the manifest change. A kiwix VM picks the new version up on its next build.

Safety rules, in order of importance:
- Nothing is replaced until the new file is fully downloaded and verified.
- The superseded file is kept: a running kiwix VM reads the library over NFS
  and would break if a file it has open vanished. Superseded files are only
  deleted once no kiwix VM is running and they have been superseded for
  retention_days.
- An update is skipped (and reported) when the disk can't hold the new file
  plus reserve_gb -- a new Stack Overflow is ~110GB.
- git: only the manifest file is committed, never other work in the tree;
  nothing is committed during a merge/rebase; a failed push leaves the commit
  local and says so. Nothing is ever forced.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_DIR = Path(__file__).resolve().parent
REPO_DIR = API_DIR.parent
MANIFEST = REPO_DIR / "examples" / "llm-chat" / "kiwix-zims.json"
ARTIFACTS = API_DIR / "package-repo" / "jammy" / "artifacts"
CATALOG_URL = "https://opds.library.kiwix.org/catalog/v2/entries?count=-1&lang=eng"

DEFAULTS = {
    "min_growth_pct": 5.0,     # grown by at least this percentage ...
    "min_growth_mb": 200,      # ... or by at least this many MB
    "reserve_gb": 20,          # free disk to keep after a download
    "retention_days": 14,      # before a superseded file may be deleted
    "dry_run": False,          # report only; download/commit nothing
    "git": "push",             # push | commit | none
}

_A = "{http://www.w3.org/2005/Atom}"
_DATE_SUFFIX = re.compile(r"_(\d{4}-\d{2})[a-z]?\.zim$")


def _fetcher():
    """The download/verify helpers from api/fetch-kiwix-zims.py (a hyphenated
    CLI script, so loaded by path rather than imported by name)."""
    spec = importlib.util.spec_from_file_location("fetch_kiwix_zims", API_DIR / "fetch-kiwix-zims.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stem(filename: str) -> str:
    """Book + flavour without the release date: wikipedia_en_all_nopic_2026-06.zim
    -> wikipedia_en_all_nopic. How the same book/flavour is matched across
    releases (the catalog's `name` alone doesn't separate maxi/nopic)."""
    return _DATE_SUFFIX.sub("", filename)


def _release(filename: str) -> str:
    m = _DATE_SUFFIX.search(filename)
    return m.group(0)[1:-4] if m else ""


def fetch_catalog(url: str = CATALOG_URL) -> dict[str, dict]:
    """Latest catalog entry per book/flavour stem: {stem: {filename, url, size, release}}."""
    with urllib.request.urlopen(url, timeout=120) as resp:
        root = ET.fromstring(resp.read())
    latest: dict[str, dict] = {}
    for e in root.findall(_A + "entry"):
        for link in e.findall(_A + "link"):
            if link.get("type") != "application/x-zim":
                continue
            href = link.get("href", "")
            zurl = href.removesuffix(".meta4")
            fn = zurl.rsplit("/", 1)[-1]
            rel = _release(fn)
            if not rel:
                continue
            cur = latest.get(_stem(fn))
            if cur is None or rel > cur["release"]:
                latest[_stem(fn)] = {"filename": fn, "url": zurl, "size": int(link.get("length") or 0),
                                     "release": rel, "meta4": href if href.endswith(".meta4") else ""}
    return latest


def find_updates(manifest: dict, catalog: dict, min_growth_pct: float, min_growth_mb: float) -> list[dict]:
    """Candidates: a newer release of the same book/flavour that grew meaningfully.
    Newer releases that didn't grow enough are returned too, marked, so the
    run log says why they were left alone."""
    out = []
    for z in manifest["zims"]:
        if not z.get("source_url"):
            continue                                   # not from Kiwix (pre-manifest local files)
        new = catalog.get(_stem(z["filename"]))
        if not new or new["release"] <= _release(z["filename"]):
            continue
        old_size = int(z.get("size_bytes") or 0)
        grew = new["size"] - old_size
        pct = (grew / old_size * 100) if old_size else 100.0
        meaningful = grew > 0 and (pct >= min_growth_pct or grew >= min_growth_mb * 2**20)
        out.append({"entry": z, "new": new, "grew_bytes": grew, "grew_pct": round(pct, 1),
                    "meaningful": meaningful})
    return out


def _download(fetch, new: dict, dest: Path, log: list[str]) -> None:
    """aria2c across Kiwix's mirrors for big files (one connection is throttled
    to ~1MB/s; aria2c reached ~108MB/s for Stack Overflow), else the fetcher's
    own resumable single-stream download."""
    if new["meta4"] and new["size"] > 2**30 and shutil.which("aria2c"):
        meta = dest.with_suffix(".meta4")
        urllib.request.urlretrieve(new["meta4"], meta)
        r = subprocess.run(["aria2c", f"--dir={dest.parent}", f"--out={dest.name}", "--continue=true",
                            "--max-connection-per-server=4", "--split=16", "--min-split-size=64M",
                            "--file-allocation=none", "--console-log-level=warn", "--summary-interval=0",
                            "--check-integrity=true", str(meta)], capture_output=True, text=True, check=False)
        meta.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"aria2c failed ({r.returncode}): {r.stderr[-300:]}")
        log.append("  downloaded with aria2c across mirrors")
    else:
        import httpx
        with httpx.Client(headers={"User-Agent": "cloudcore-kiwix-updates"}) as client:
            fetch.download(client, new["url"], dest, new["size"])


def _kiwix_vm_running() -> bool:
    import store
    return any("kiwix" in (i.name or "") and i.status.value == "running" for i in store.list_instances())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, check=False)


def _commit_manifest(repo: Path, manifest_path: Path, message: str, mode: str, log: list[str]) -> str:
    if mode == "none":
        return "manifest updated (git: none)"
    git_dir = repo / ".git"
    if any((git_dir / p).exists() for p in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
        log.append("  git: merge/rebase in progress -- manifest left uncommitted")
        return "manifest updated but NOT committed (merge/rebase in progress)"
    rel = str(manifest_path.relative_to(repo))
    # A pathspec commit records only this file, whatever else is staged.
    r = _git(repo, "commit", "-m", message, "--", rel)
    if r.returncode != 0:
        log.append(f"  git commit failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return "manifest updated but commit FAILED"
    log.append(f"  git: committed {_git(repo, 'rev-parse', '--short', 'HEAD').stdout.strip()}")
    if mode != "push":
        return "manifest committed (not pushed)"
    r = _git(repo, "push", "--quiet")
    if r.returncode != 0:
        log.append(f"  git push failed (commit kept locally): {(r.stderr or r.stdout).strip()[-300:]}")
        return "manifest committed, push FAILED (kept locally)"
    log.append("  git: pushed")
    return "manifest committed and pushed"


def _prune(manifest: dict, artifacts: Path, retention_days: int, dry_run: bool, log: list[str]) -> bool:
    """Delete superseded files past retention, only while no kiwix VM runs."""
    sup = manifest.get("superseded") or []
    if not sup:
        return False
    if _kiwix_vm_running():
        log.append(f"prune: {len(sup)} superseded file(s) kept -- a kiwix VM is running and may have them open")
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    keep, changed = [], False
    in_use = {z["filename"] for z in manifest["zims"]}
    for s in sup:
        old_enough = datetime.fromisoformat(s["superseded_at"]) <= cutoff
        if old_enough and s["filename"] not in in_use:
            if dry_run:
                log.append(f"prune: would delete {s['filename']}")
                keep.append(s)
                continue
            (artifacts / s["filename"]).unlink(missing_ok=True)
            log.append(f"prune: deleted {s['filename']} (superseded {s['superseded_at'][:10]})")
            changed = True
        else:
            keep.append(s)
    manifest["superseded"] = keep
    return changed


def run(options: dict | None = None, manifest_path: Path = MANIFEST, artifacts: Path = ARTIFACTS,
        repo: Path = REPO_DIR, catalog: dict | None = None) -> tuple[str, str, list[str]]:
    """One update pass. Returns (status, summary, log) for the scheduler."""
    opts = {**DEFAULTS, **(options or {})}
    log: list[str] = [f"options: {json.dumps(opts)}"]
    manifest = json.loads(manifest_path.read_text())
    catalog = catalog if catalog is not None else fetch_catalog()
    log.append(f"catalog: {len(catalog)} book/flavour releases; manifest: {len(manifest['zims'])} ZIMs")

    candidates = find_updates(manifest, catalog, float(opts["min_growth_pct"]), float(opts["min_growth_mb"]))
    fetch = _fetcher()
    updated, skipped, failed = [], [], []
    for c in candidates:
        z, new = c["entry"], c["new"]
        what = (f"{z['filename']} -> {new['filename']} "
                f"({c['grew_bytes'] / 2**20:+.0f}MB, {c['grew_pct']:+.1f}%)")
        if not c["meaningful"]:
            skipped.append(what)
            log.append(f"skip (growth below threshold): {what}")
            continue
        free = shutil.disk_usage(artifacts).free
        need = new["size"] + int(opts["reserve_gb"]) * 2**30
        if free < need:
            skipped.append(what)
            log.append(f"skip (disk): {what} -- needs {need / 2**30:.0f}GB free incl. reserve, have {free / 2**30:.0f}GB")
            continue
        if opts["dry_run"]:
            log.append(f"would update: {what}")
            updated.append(what)
            continue
        dest = artifacts / new["filename"]
        try:
            log.append(f"update: {what}")
            expected = None
            import httpx
            with httpx.Client() as client:
                expected = fetch.published_sha256(client, new["url"])
            if not dest.exists():
                _download(fetch, new, dest, log)
            actual = fetch.sha256_of(dest)
            if actual != expected:
                dest.rename(dest.with_suffix(dest.suffix + ".bad"))
                raise ValueError(f"checksum mismatch: got {actual}, Kiwix publishes {expected}")
            old_fn = z["filename"]
            # size_bytes is what the kiwix VM checks at boot, so set it here:
            # record_metadata only fills it (and the ZIM's own ids) when libzim
            # is importable, which the API's system python is not. The old
            # release's ids are dropped rather than left describing another file.
            z.update({"filename": new["filename"], "source_url": new["url"], "sha256": actual,
                      "size_bytes": dest.stat().st_size})
            for stale in ("book_name", "uuid", "fulltext"):
                z.pop(stale, None)
            fetch.record_metadata(z, dest)
            manifest.setdefault("superseded", []).append(
                {"filename": old_fn, "superseded_at": datetime.now(timezone.utc).isoformat()})
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            log.append(f"  verified {actual[:16]}...; manifest now points at {new['filename']}")
            updated.append(what)
        except Exception as e:  # noqa: BLE001 -- one book failing must not stop the rest
            failed.append(what)
            log.append(f"  FAILED: {e}")

    pruned = _prune(manifest, artifacts, int(opts["retention_days"]), bool(opts["dry_run"]), log)
    if pruned:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    git_result = ""
    if not opts["dry_run"] and (any(u for u in updated) or pruned):
        msg = ("chore: kiwix ZIM updates (scheduled)\n\n" + "\n".join(f"- {u}" for u in updated)
               + ("\n- pruned superseded files" if pruned else "")
               + "\n\nVerified against Kiwix's published sha256 by api/kiwix_updates.py.")
        git_result = _commit_manifest(repo, manifest_path, msg, str(opts["git"]), log)

    verb = "would update" if opts["dry_run"] else "updated"
    summary = (f"{len(updated)} {verb}, {len(skipped)} skipped, {len(failed)} failed"
               + (f" -- {git_result}" if git_result else "") + (" (dry run)" if opts["dry_run"] else ""))
    status = "failed" if failed else "success"
    return status, summary, log
