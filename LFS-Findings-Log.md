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

### LFS-008 — The book puts command alternatives inside notes, and their headings aren't subsections

**Where:** the LFS 13.1 HTML book's admonition boxes (`div class="admon"`: Warning, Note, Important); `lfs/book-sections.py`; `api/lfs_build.py`.

**Symptom:** found 2026-10-07 (C1). Section 10.4, "Using GRUB to Set Up the Boot Process", came out with commands under subsections called "Warning" and "Note". Among them were alternatives the build must choose between: a rescue CD (`grub-mkrescue`, `xorriso` to `/dev/cdrw`), BIOS install (`grub-install --target=i386-pc`), UEFI install (`grub-install --target=x86_64-efi --removable`) and an optional `efibootmgr` entry. So nothing could be filtered by boot method.

**Root cause:** the extractor treated every `<h3>` as a new subsection, including an admonition box's own heading, so the real subsection ("10.4.4.1 Booting With BIOS", "10.4.4.2 Booting With UEFI") was lost.

**Fix:**
- **Notes keep their subsection:** headings inside admonitions now label the commands (`note`) without replacing the subsection.
- **Boot method:** the queue skips BIOS and 32-bit UEFI subsections by name.
- **Single commands:** a per-command skip list records the reason, for example the rescue CD: "an optional rescue CD; the lab machine has no CD writer".

**Verified by:** 10.4 now serves the UEFI `grub-install`, the efivars/efibootmgr note commands and the `grub.cfg` creation. The BIOS command and the rescue CD are left out, with reasons. 8.65 (GRUB's build) skips its BIOS and 32-bit UEFI subsections.

### LFS-009 — Asking the 14B to copy the book's commands back made planning slow and risky

**Where:** `examples/llm-chat/files/lfs_worker.py` (C2: the plan the model writes for each task).

**Symptom:** found 2026-10-07, on the first plan-only run (task 1, 2.2 Host System Requirements). The plan format asked the model to return every book command verbatim, with any changes. 2.2's one command is the book's ~60-line `version-check.sh`. On these CPUs the 14B reads about 4 tokens/s and writes about 2. Reading the prompt plus re-typing the script in JSON ran past 20 minutes, and the run's own time limit stopped it before any plan arrived.

**Root cause:**
- **Cost:** a planning call cost was proportional to the section's length, not to the work it needed.
- **Risk:** a 14B re-typing a long script can silently change it, so the copying was also a correctness risk.

**Fix:** the model now writes **only differences**: a command changed (with the filled-in text), left out, moved to another user, or added (`after: n`, or `-1` for first), each with a reason. Every other command runs exactly as the book prints it, inserted by the controller.
- **Guards kept:** a command still holding a placeholder (`/dev/<xxx>`) must be changed; nothing may touch the system disk; every change needs a reason.
- **Offline tests:** "as the book", fill-in plus omission, a commandless section's added steps (kept in the order given; an ordering bug was found and fixed by this test), a refused system-disk command, a bad reference.

**Verified by:** the re-run of task 1's plan (journalled), and C6.

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-10-07 | Paul Scott | Started with Phase A: LFS-001–LFS-005 (the books' published data, an upstream broken link, a first lesson about the 14B, a parsing quirk). |
| v0.2 | 2026-10-07 | Paul Scott | B4: LFS-006 (the build machine can't reach the repo through the lab fence: the controller delivers sources). |
| v0.3 | 2026-10-07 | Paul Scott | B2: LFS-007 (empty /etc/machine-id before imaging). |
| v0.4 | 2026-10-07 | Paul Scott | C1: LFS-008 (command alternatives inside notes; the extractor now keeps the real subsection). |
| v0.5 | 2026-10-07 | Paul Scott | C2: LFS-009 (the model writes only differences from the book; the controller inserts the book's exact commands). |
