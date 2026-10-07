# LFS OS Build — Findings Log

**Owner:** Paul Scott. **Plan:** `lfs-os-Phased-Implementation.md`.

Everything of value found while building the OS: problems with the books and their published data, upstream quirks, what llm-chat (the 14B) gets right and wrong, how the lab handles the build, and what tutoring it needed.

- **Where these go:** platform bugs found along the way (CloudCore, the lab machinery) go in `haFullStack-Findings-Log.md` as F-NNN.
- **Format:** each entry has the same fields as that log. Sentinel ingests this file into its knowledge base (headings `### LFS-NNN — title`), and its matches are fed back to llm-chat during the build (C4/C5).

### LFS-001 — LFS's stable-systemd source list also carries SysVinit-only files

**Where:** `https://www.linuxfromscratch.org/lfs/downloads/stable-systemd/wget-list` (LFS 13.1); `lfs/build-manifest.py`.

**Symptom:** found 2026-10-07 (A1). The systemd edition's `wget-list` has 99 entries, but its `md5sums` has 94. The 5 extra are `lfs-bootscripts`, `sysklogd`, `sysvinit` and its patch, and `udev-lfs`, which only the SysVinit edition uses.

**Root cause:** the download directory serves one shared `wget-list` for both editions; only `md5sums` is edition-specific.

**Fix:** the manifest takes the systemd set from `md5sums`, uses `wget-list` only for those files' URLs, and logs what it leaves out.

**Verified by:** 93 LFS files plus the book's kernel = the 94 `md5sums` entries, all mirrored and MD5-verified.

### LFS-002 — BLFS publishes no checksums for its patches, books or systemd units

**Where:** BLFS 13.1 (systemd): the efivar patch, the book tarballs, `blfs-systemd-units`; `api/lfs-mirror.py`.

**Symptom:** found 2026-10-07 (A1/A2). BLFS package pages give an MD5 for each source, but the "Required patch" links have none, and neither do the downloads directory's book and unit tarballs.

**Root cause:** BLFS publishes checksums only for package sources.

**Fix:** those files are pinned by SHA-256 at their first download over HTTPS, recorded in `artifacts/lfs/MIRROR.json`, and every later run must match.

**Verified by:** 4 such files recorded on the first run. A changed local copy is detected and replaced (tested).

### LFS-003 — LFS's advisories index links a page for 13.1 that doesn't exist

**Where:** `https://www.linuxfromscratch.org/lfs/advisories/` → `13.1.html`; the same for BLFS. `api/lfs_updates.py`.

**Symptom:** found 2026-10-07 (A5). The advisories index says "The advisories for LFS-13.1 … are at LFS-13.1" and links `13.1.html`, which returns 404, for LFS and for BLFS. The 13.1 errata page's "Known Security Vulnerabilities" section only points back to the advisories page.

**Root cause:** upstream: the per-version advisory page isn't published yet, or is misnamed.

**Fix:** the weekly update check watches the errata page, both advisories indexes and the per-version pages, reports a 404 as "not published yet", and reports when it appears.

**Verified by:** the first scheduled run (2026-10-07) logged both 404s and returned "nothing new".

### LFS-004 — Given the right book page, the 14B still answered generically instead of following it

**Where:** llm-chat (Qwen2.5-Coder-14B Q4), Linux Help, with the LFS ZIM in its Kiwix tier (A4b).

**Symptom:** found 2026-10-07. Asked "In Linux From Scratch, how do I build GRUB for 64-bit UEFI instead of BIOS?", llm-chat's top source was the right page, LFS 8.65 GRUB-2.14. Its answer was still a generic guide ("install dependencies", "download GRUB from the official website") without the book's `--target=x86_64 --with-platform=efi` configure line.

**Root cause:** search grounding gives the model a snippet to consult, not text to follow. A 14B leans on its general knowledge unless the exact steps are in front of it and the task is to carry them out.

**Fix (design, for C2):** the build's worker loop gives the model the exact section's commands for the subsection in hand (`sections-lfs-13.1.json`, A4a), and asks it to follow and check them, not to recall them.

**Verified by:** to be measured in C6, where the loop is proven on a small slice.

### LFS-005 — The book's headings carry no dot after the section number

**Where:** the LFS 13.1 HTML book's `<h1>` titles; `lfs/book-sections.py`.

**Symptom:** found 2026-10-07 (A4a). Parsing "8.65. GRUB-2.14" style headings found none: the rendered title text is "8.65 GRUB-2.14". The number ended up in the title and the package name ("8.65 GRUB").

**Root cause:** the HTML holds the number and the title in separate elements; the dot belongs to neither.

**Fix:** the dot after the number is optional. Titles like "Binutils-2.47 - Pass 1" are split into package, version and stage.

**Verified by:** GRUB → 8.65 / GRUB / 2.14; Binutils pass 1 → 5.2 / Binutils / 2.47 / "Pass 1"; the kernel → 10.3 / Linux / 7.1.8.

### LFS-006 — The build machine can't fetch its sources from the repo: the lab fence keeps it off the host

**Where:** the `lfs-build` lab VM (B4), on the isolated lab network `cclab0` (`api/setup-lab-network.sh`, table `inet cclab`).

**Symptom:** found 2026-10-07, testing B4. The plan's sources (A2, 783 MiB under `artifacts/lfs/`) are served by the host's package repo on 8090. From `cclab0` the host accepts only DHCP and DNS, and routed traffic to private ranges is dropped, so the build machine can't reach the repo.

**Root cause:** by design. Lab VMs run model-written commands, so the lab network fences them off from the host's services and the LAN. The build machine is a lab VM for the same reason: llm-chat proposes its commands.

**Fix (design, for C2):** the controller delivers what each step needs. It has access to the repo, and it copies the sources into the build machine over the control channel (SSH on 1022), verified against `MIRROR.json` on arrival. The fence is not loosened for the build.

**Verified by:** to be verified in C6.

Also noted while testing: the build machine's `/bin/sh` is `dash`, as Ubuntu ships it. LFS requires bash there (chapter 2.2's version check fails otherwise). The book makes that change part of host preparation, so it's llm-chat's first task in D1, not the broker's.

### LFS-007 — The LFS image must ship with an empty /etc/machine-id

**Where:** LFS 13.1 (systemd), the systemd section of chapter 8, which runs `systemd-machine-id-setup`; D6 (the image import).

**Symptom:** found 2026-10-07 while testing B2 (see F-232). An image imported from a booted system carries its `/etc/machine-id`. systemd-networkd's DHCP client ID derives from it, so every instance of the image asks for the same address.

**Root cause:** LFS creates the machine ID during the build. That's right for one machine, but every copy of an image would share it.

**Fix (for D6):** before the disk is imported as an image, truncate `/etc/machine-id` to empty, not delete it. systemd then generates a new one at each instance's first boot.

**Verified by:** to be verified in D6, by booting two instances of the LFS image together and checking they get different machine IDs and addresses.

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-10-07 | Paul Scott | Started with Phase A: LFS-001–LFS-005 (the books' published data, an upstream broken link, a first lesson about the 14B, a parsing quirk). |
| v0.2 | 2026-10-07 | Paul Scott | B4: LFS-006 (the build machine can't reach the repo through the lab fence: the controller delivers sources). |
| v0.3 | 2026-10-07 | Paul Scott | B2: LFS-007 (empty /etc/machine-id before imaging). |
