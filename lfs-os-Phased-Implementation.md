# A bootable OS from Linux From Scratch, built by llm-chat — Phased Implementation

> **Roadmap (2026-10-09):** this plan stays the working plan while the build runs; its later phases are listed in `cloudcore-Roadmap.md` (CC-19, CC-90 to CC-95).

**Status:** started 2026-10-07 (Phase A). **Owner:** Paul Scott.

## Context

Direct request (2026-10-06): once llm-chat is as good as it can be (`llm-chat-Next-Steps.md`), use llm-chat and its lab to build a bootable operating system from the Linux From Scratch sources and the latest stable kernel, in two stages:
- **Stage 1:** a command-line OS.
- **Stage 2:** a graphical UI on top.

Who does what:
- **llm-chat (the 14B) does the bulk of the work.** Claude acts as its tutor, helping only when it struggles.
- **The build is documented between the two of them.**
- **Sentinel watches everything,** notes every issue either of them hits, and builds a knowledge base for the build.

## Decisions (Paul, 2026-10-06)

| Decision | Choice |
|---|---|
| Init system | **systemd** (the LFS systemd book) |
| Stage 2 display | **Wayland** |
| Boot | **UEFI** |
| Sources | **Downloaded once and stored** in the host package repo, like the Kiwix files and models |
| Update checks | **The dashboard scheduler** watches for new LFS/BLFS releases, book errata, stable kernels and updated packages |

## Principles

- **The book is the ground truth.** llm-chat works from the exact LFS/BLFS text for the pinned version, one section at a time, never from memory.
- **Pinned and reproducible.** Every source is stored with its published checksum, verified on download, and used from our repo. A newer version is a decision, never an automatic swap.
- **llm-chat first, Claude on escalation.** Claude's help is explained (why, not just what) and written into the journal and the knowledge base, so the next similar problem is llm-chat's to solve.
- **Checkpoints before risk.** A mistake late in the build must never cost the work before it.
- **Everything observable.** Every command, result, escalation and fix is logged, watched by Sentinel and kept.
- **The quality floor holds:** the 14B or better, never smaller, placed on the best measured host (`llm-chat-placement-Phased-Implementation.md`).
- **Never all of a host:** the build VM leaves the host's reserved cores free.

## Phase A — Sources and books: download, store, keep current

| # | Stage | Who |
|---|---|---|
| A1 | **Inventory.** Pin the current stable LFS systemd book. The source list comes from its `wget-list-systemd`, the checksums from its `md5sums`. Pin the latest stable kernel, checksummed by kernel.org's `sha256sums.asc`. From the matching BLFS book, list the packages for UEFI boot (efivar, efibootmgr, popt, GRUB for EFI, and their dependencies) and for the Wayland stack (after decision E1). The result is one manifest: file, version, upstream URL, checksum, book section. | **Done 2026-10-07:** `lfs/build-manifest.py` writes `lfs/manifest.json` (102 files) from the books' own data:<br>• **LFS 13.1 (systemd):** 93 sources and patches, from its `md5sums`, the authoritative systemd list. Its `wget-list` also carries 5 SysVinit-only files, left out.<br>• **Kernel:** 7.2.9, latest stable, verified at download by kernel.org's signed `sha256sums.asc`; plus the book's 7.1.8 as a fallback.<br>• **UEFI:** LFS 13.1 builds UEFI GRUB itself; from BLFS 13.1 only efibootmgr 18, efivar 39 (plus a required patch) and popt 1.19.<br>• **Also:** BLFS's systemd unit files and both books.<br>• **One gap:** BLFS publishes no checksums for its patches, books or units; those are pinned by SHA-256 at first download over HTTPS.<br>Not yet: book sections per package (C1 builds them from the book's chapters); the Wayland set (E1). |
| A2 | **A mirror script, `api/lfs-mirror.py`.** Downloads everything in the manifest into `artifacts/lfs/<lfs-version>/`, `artifacts/kernel/` and `artifacts/blfs/<blfs-version>/`.<br>• **Sources:** the LFS mirrors, which hold a release's whole set in one place, with upstream as fallback.<br>• **Verification:** each file against the book's checksum; the kernel's checksum file by its PGP signature too.<br>• **Behaviour:** resumable, `--dry-run`, logs to stderr, exits non-zero on any mismatch. | **Done 2026-10-07** (322c554): `api/lfs-mirror.py`, all 102 files verified (783 MiB) under `artifacts/lfs/`, each recorded in `artifacts/lfs/MIRROR.json`:<br>• **94** by the book's MD5;<br>• **kernel 7.2.9** by its PGP signature, from Greg Kroah-Hartman's pinned key;<br>• **4** with nothing published, pinned by SHA-256 at first download.<br>Tested: a wrong checksum is rejected and discarded (exit 1); a re-run skips verified files; a changed local copy is reported and replaced. LFS files came from the OSUOSL mirror, the rest from upstream. Layout: one `artifacts/lfs/` tree (`13.1/`, `kernel/`, `blfs-13.1/`, `books/`) rather than three top-level directories. |
| A3 | **Into the repo and onto the peer.** Mirrored files go into `sync-index.json` and reach Llwyn-y-Groes through the existing repo sync. Space: about 5–10 GB in all; Stourport has 692 GB free, Llwyn-y-Groes 593 GB. | **Done 2026-10-07:** indexed in `sync-index.json`, and copied to Llwyn-y-Groes through the peer tunnel and both new host firewalls: 104 files, 783 MB, in 10 s. Re-hashed there against `MIRROR.json`: 0 mismatches. On the way: F-231, the repo copy couldn't create a new subdirectory (fixed). |
| A4 | **The books into llm-chat's corpus.** The LFS and BLFS books for the pinned versions, so the 14B reads the exact text for the release it's building. Section by section, alongside the build knowledge base. | **Done 2026-10-07:** two forms, for two uses:<br>• **A4a, by section, for the build** (358810a): `lfs/book-sections.py` splits the books into `sections-lfs-13.1.json` (202 sections, 675 command blocks) and `sections-blfs-13.1.json` (878, 3135). Each section has its number, title, package, version, stage ("Pass 1"), prose, and commands in book order. Commands are tagged by subsection, so GRUB's 64-bit UEFI steps are told apart from BIOS, and by as-root.<br>• **A4b, by search, for llm-chat** (0f1ed88, aa8c8cc): `lfs/make-zim.py` writes full-text ZIMs (`lfs_en_systemd_13.1.zim`, `blfs_en_systemd_13.1.zim`), added to `kiwix-zims.json` as tier `6-lfs` for the Linux panel and synced to both hosts. Live: the Kiwix VM searches them. Asked "how do I build GRUB for 64-bit UEFI", llm-chat's top source was LFS 8.65.<br>**Lesson for C2:** with the right page retrieved, the 14B's answer still stayed generic, without the book's exact `--target=x86_64 --with-platform=efi`. Search grounding isn't enough for the build; the worker loop gives the model the exact section's commands (A4a) and has it follow them. |
| A5 | **A scheduler job, `lfs_update`.** Weekly, modelled on `kiwix_update`. It checks for:<br>• a new stable LFS or BLFS release;<br>• new book errata and security advisories;<br>• a new stable kernel;<br>• newer versions of the packages in the manifest.<br>It **reports and never replaces**: a dashboard entry and a Sentinel finding, with what changed and why it matters (for example, an advisory for a package we've built). Moving up is a decision, mirrored side by side with the pinned set. | **Done 2026-10-07:** `api/lfs_updates.py`, scheduler kind `lfs_update`, offered on the dashboard's Scheduler page as "LFS update check (report only)". Schedule "Weekly: LFS update check, report only (Mon 06:00 UTC)" on Stourport; first run through the scheduler: "nothing new". It reports, never replaces:<br>• a new stable LFS or BLFS release;<br>• any change to the pinned version's errata page and to the LFS/BLFS advisory pages (what changed is quoted);<br>• a newer stable kernel, a newer point release in the pinned series, or that series reaching end of life.<br>Anything new marks the run **attention** (amber); tested with simulated changes. LFS's own index links `advisories/13.1.html`, which returns 404; the job reports when it appears.<br>Not yet: package-version news (open decision), and a Sentinel finding for news (comes with C4). |

## Phase B — Platform capabilities (CloudCore)

Checked 2026-10-06: CloudCore has none of these yet.

| # | Stage | Who |
|---|---|---|
| B1 | **Disk snapshots.** Create, list and restore snapshots of an instance's disks, through the API and the dashboard. The build's checkpoints (C3) depend on it. | **Done 2026-10-07:** `GET/POST /v1/instances/<id>/snapshots`, `POST …/snapshots/<name>/restore`, `DELETE …/snapshots/<name>`, and a Snapshots panel on the dashboard's Instances page.<br>• **How:** a qcow2 internal snapshot of every disk of the instance, taken with the VM shut down cleanly (and started again), so the disks are consistent. Restore powers off and reverts every disk.<br>• **Live test** (a throwaway Ubuntu instance): snapshot of a running VM in **7 s** including shutdown and restart. After a change, restore brought back the old marker file and removed a file made after the snapshot. Guards: bad name 400, duplicate 400, delete while running 409, unknown 404. Delete while stopped removes it from the disk.<br>• **Limits:** local instances only; data disks exist only on lab VMs, so multi-disk snapshots are tested with the build machine (B4). |
| B2 | **Boot a custom image, with UEFI.** Import a disk image built by us as an image, and boot it as an instance under OVMF firmware. Its serial console is captured, as instances' consoles already are. | **Done 2026-10-07:** `POST /v1/images` imports a disk of a stopped instance (as it is, or as one of its snapshots) as a standalone custom image, with metadata saying how to boot it: firmware `uefi`/`bios`, disk bus `virtio`/`scsi`. `DELETE /v1/images/<id>` removes it once no instance uses it. A `uefi` image boots under OVMF, with each instance's own copy of the variable store. **OVMF was already on both hosts**, so no sudo step was needed.<br>**Live test:** a stock Ubuntu instance still booted by BIOS. Stopped and imported as a UEFI image in 2.4 s, its disk then booted a new instance through UEFI (29 EFI variables visible). Guards: an image in use 409, a duplicate id 400, a running source 409.<br>**Found:** F-232 / LFS-007, an imported image carries the source's machine ID and so its DHCP identity. The LFS image must ship with an empty `/etc/machine-id`. |
| B3 | **A graphical console.** **VNC** (Paul, 2026-10-07: VNC rather than SPICE) display for an instance, reachable only through the dashboard, plus a screenshot API so the GUI stage can be checked by Claude as well as seen by Paul. | **Done 2026-10-07** (VNC, Paul's choice): an instance tagged `display=vnc` (or an image asking for one) gets virtio-gpu (a DRM device a Wayland compositor can use) and VNC on the host's **loopback only**.<br>• **Screenshots:** `GET /v1/instances/<id>/screenshot` returns a PNG via libvirt (converted from PPM with the standard library).<br>• **Live viewer:** `POST …/vnc-ticket` gives a one-time, 60-second ticket, and `…/vnc?ticket=` is a WebSocket the API bridges to the VNC port (`api/vnc_bridge.py`, RFC 6455, standard library only). The dashboard's Console button opens noVNC 1.7.0 (vendored, SHA-512 checked against npm).<br>**Tested:** the screenshot is a valid 1280×800 PNG. Through the bridge, the upgrade answers 101 and the VNC greeting `RFB 003.008` arrives framed; a reused or forged ticket gets 401. Along the way: Werkzeug only routes upgrade requests to rules declared `websocket=True`, and F-233 (a UEFI instance's VM leaked on delete). **Verified by Paul 2026-10-07:** the dashboard's Console button opened the instance's screen in the browser, and a direct VNC client on Stourport's loopback worked too. The test instance was deleted afterwards, and its VM removed cleanly (F-233's fix). |
| B4 | **A build machine in the lab.** A new lab VM purpose, `lfs-build`. It differs from the lab's disposable VMs:<br>• **Lifetime:** days, not an hour, and never idle-reaped while a build is running.<br>• **Disks:** a data disk for the LFS partitions.<br>• **Size and place:** as many cores as placement allows, on the host that compiles fastest. That needs a compile benchmark beside `llm-bench`. | **Done 2026-10-07** (cc74903): broker purpose `lfs-build`.<br>• **Size:** the largest of standard.2xlarge, xlarge or large the host affords.<br>• **Disk:** a 50 GB data disk for the LFS system (`/dev/sdb`).<br>• **Packages:** the book's host requirements installed at boot.<br>• **Limits:** one at a time; 72 h idle / 30 days (reaping deletes its snapshots too).<br>**Live test:** requested from the llm-chat coordinator, it came up as standard.xlarge on Llwyn-y-Groes, since the coordinator held the memory for 2xlarge: 6 cores, 7 GB, 60 GB + 50 GB, every required tool present. A **two-disk** snapshot (system + LFS disk) took 6.3 s, and restore brought both back. Deleted through the broker afterwards.<br>**Not yet:** placement by measured compile speed. Today it's the largest flavor on the controller's host; compiling is CPU-bound, so cores are a fair proxy until a compile benchmark exists.<br>**Constraint for C2:** lab VMs can't reach the host's package repo (the lab fence), so the controller delivers the sources (LFS-006). |
| B5 | **Seeing the build machine yourself** (Paul, 2026-10-09: check build logs directly rather than ask Claude; moved here from the Sentinel plan, since it's CloudCore work).<br>**Why it doesn't work today:** the dashboard's terminal connects to port 22 as the instance's non-sudo user, with CloudCore's key, but lab VMs (the `lfs-build` machine included) run sshd on **port 1022** on the fenced lab network, as `labctl`. Lab VMs are also created without a VNC display.<br>**B5.1** **A read-only build log viewer** (the main ask), **in CloudCore's dashboard** (Paul, 2026-10-09: the build happens in CloudCore, and Sentinel is a substantial project in its own right). A new LFS build page beside the instance pages:<br>• **The build at a glance:** every task with its state, attempts and times; the current step from the heartbeat.<br>• **Each step's full log,** fetched on demand. A new broker route serves files from `/var/log/lfs-build/` on an `lfs-build` machine only: read-only, a fixed directory, names checked, size-capped.<br>• **The current step's log, followed live** (a `tail -f` view that refreshes).<br>• **The journal** (`journal.md`), rendered, with links from each journal entry to its step's log.<br>No shell, so nothing can be disturbed mid-build.<br>**B5.2** **A terminal on lab VMs.** `terminal.py` learns lab VMs: port 1022 on the lab network, CloudCore's key, from the host (the key never goes to the VM).<br>**A dedicated non-sudo user, `viewer`,** created on `lfs-build` machines. It can read `/var/log/lfs-build` and look around `/mnt/lfs`, but can't change the build. `labctl` and root stay the controller's.<br>**B5.3** **A console** (optional): `lfs-build` machines created with a VNC display (B3), so the dashboard's Console button works for them, as for ordinary instances. Mostly useful when a build machine won't boot or SSH has failed.<br>**B5.4** **Tests:**<br>• **the log route:** refuses paths outside `/var/log/lfs-build`, other lab-VM purposes, and other tokens; serves a growing log correctly;<br>• **the terminal:** reaches a lab VM as `viewer`, and `viewer` can't write under `/mnt/lfs`. | Claude |

## Phase C — The build protocol: llm-chat, the lab, Claude and Sentinel

| # | Stage | Who |
|---|---|---|
| C1 | **The build journal and task queue.** One task per book section, in book order, each with its state (waiting, running, done, stuck, escalated). It records who did what, every command and result, and every lesson. This is the documentation "between the two of us": llm-chat writes its working, and Claude writes its tutoring. | **Built 2026-10-07:** `api/lfs_build.py`, the journal and queue in CloudCore's database on the coordinator's host (backed up nightly, outlives coordinator rebuilds). Routes for admin or the lab token, reachable from the coordinator on the guest listener.<br>**The queue** for stage 1: 144 tasks in book order, from the section files plus the BLFS UEFI tools (before 10.4) and OpenSSH (added to the manifest for D6, verified, mirrored). Each task has:<br>• **its context:** host as root (10), the `lfs` user (23), or chroot (111);<br>• **what to skip:** subsections (BIOS, 32-bit UEFI) and single commands with a reason (10.4's rescue CD);<br>• **version overrides:** kernel 7.2.9 for 5.4 and 10.3;<br>• **risk and checkpoints:** 12 high-risk sections get a checkpoint before them, each chapter end one after.<br>**The journal** records who did what: llm-chat, Claude, the lab, Sentinel, Paul or the controller; as proposal, command, result, lesson, escalation, note, state or checkpoint. It's readable as `journal.md`.<br>**Tested** on a copy of the database: create, queue order, state, attempts, journal, the served commands, bad input, no token. The authorization walk now fills typed route parameters, which it couldn't before. Found on the way: LFS-008 (alternatives inside notes). |
| C2 | **llm-chat's worker loop.** For each task:<br>1. Read the book section, plus the knowledge base for that package.<br>2. Propose the commands.<br>3. The lab runs them on the build VM, in the chroot where the book says so.<br>4. Judge the result against the book's expected results, including the test-suite results the book says to expect.<br>5. Repair, then journal.<br>One section at a time keeps the 14B's context small. | **Built and running 2026-10-07:** `examples/llm-chat/files/lfs_worker.py` on the coordinator, shipped by both templates.<br>• **The machine:** created or reused, and re-keyed through the broker after a coordinator rebuild (proven live).<br>• **Running steps:** contexts (root / lfs / chroot), mounts restored after reboots, sources delivered and checked in place of wget, steps run detached.<br>• **The model's work:** it plans only differences from the book (LFS-009), with every call schema-constrained (LFS-011) and repair commands in a required list (LFS-012); added steps keep their order, and the model is kept to its own section (LFS-013).<br>• **Judging:** an exit-0 step that prints errors is judged (2.2); test failures are judged against the book; repairs are limited, then the task goes stuck.<br>• **Tutoring:** the tutor's journal notes go into the model's plan (LFS-014).<br>• **Model choice:** the endpoint is picked by live speed (F-236).<br>**Live:** tasks 2.2–2.7 done (host check, a UEFI partition layout written by the 14B, filesystems, $LFS, mounts). One tutoring round was needed (2.5/2.7). |
| C3 | **Checkpoints.** A snapshot at the end of each chapter, and before every high-risk section (the toolchain passes, glibc, GCC, the kernel, the bootloader). A section that breaks the system beyond repair is rolled back to the last checkpoint, not patched over. | **Built 2026-10-07:** broker routes `GET/POST /v1/lab-vms/<id>/snapshots` and `POST …/snapshots/<name>/restore`, for `lfs-build` machines only, with the lab token. The worker snapshots both disks before each high-risk section (a retry keeps the first) and after each chapter, then reconnects and restores the mounts. Live: checkpoints after chapters 2, 3 and 4 and before 5.2, about 4 s each. |
| C4 | **Sentinel watching.** Every build log goes to Loki (promtail on the build VM). Sentinel:<br>• **Logs problems:** every problem becomes a finding in a separate `LFS-Findings-Log.md` (same F-NNN format), with its cause and fix.<br>• **Keeps the knowledge base:** findings, lessons and the journal are ingested into a build knowledge base.<br>• **Detects stalls by build progress markers** (section started or finished, test results), never by log silence: GCC's build legitimately runs for an hour. | **Built 2026-10-07, changed from the plan:** Sentinel reads the build's own API, not Loki.<br>• **Why the change:** the build VM is fenced, and the journal already carries every step's output (a 16 KB tail for each failure). Full logs stay on the VM, inside every checkpoint.<br>• **The heartbeat:** the worker posts its phase every minute (`POST /v1/lfs/builds/<id>/heartbeat`).<br>• **Sentinel** (`sentinel/lfs_watch.py`, in the watch loop, every 60 s) raises a stall when the heartbeat is lost for over 10 min, or when a live worker's journal is frozen for longer than any step may take (7 h 15 min). It also ingests the tutor's lessons into the KB as `LFS-T<id>`. Tested with a fake API. |
| C5 | **The escalation ladder.**<br>1. **llm-chat retries,** with the lab's repairs and other approaches.<br>2. **Sentinel nudges llm-chat** with matching knowledge-base entries, for a fresh attempt.<br>3. **Sentinel starts a tutor session:** headless Claude Code (`claude -p`) on Stourport, given the stuck task, its logs and the matches. Guardrails:<br>&nbsp;&nbsp;• permissions limited to the build VM and the journal;<br>&nbsp;&nbsp;• one session at a time, with a daily cap;<br>&nbsp;&nbsp;• every session logged and ingested.<br>&nbsp;&nbsp;Claude explains the lesson in the journal, and llm-chat carries on.<br>4. **Paul is told** by phone notification, and that branch of the build pauses. | **Built 2026-10-07:** one rung per stuck episode, each recorded in the journal as `{rung: N}`.<br>1. **The worker** retries with repairs, then goes stuck. With `--follow`, it now waits to be reset instead of exiting.<br>2. **Sentinel** posts its closest KB findings (confidence ≥ 0.2) as a lesson and resets the task. With no close match, it goes straight to rung 3.<br>3. **Sentinel requests a tutor;** `lfs/lfs-tutor.py` (a user timer on the host with Claude Code, every 2 min) answers it.<br>&nbsp;&nbsp;• **Guardrails:** headless `claude -p` with **no tools**, given the journal, section and facts; it answers only in a JSON lesson schema. One session at a time, a daily cap of 6 (at the cap: Paul), 2 per task, every session kept.<br>&nbsp;&nbsp;• **The answer:** a lesson plus a KB finding, and the task reset; or "needs Paul".<br>4. **Paul:** the build is paused, with an event in Sentinel's UI and a notification via `SENTINEL_NOTIFY_URL` (ntfy-style). **No channel for now (Paul, 2026-10-07):** rung 4 pauses the build and shows in Sentinel's UI and the journal.<br>The worker puts both tutor and Sentinel lessons into plans *and* repairs.<br>Installed: `lfs/install-lfs-tutor.sh --api-ssh <API host>`. Auth checked from a systemd unit. |
| C6 | **Prove the loop on a small slice.** For example, the first pass of binutils (LFS chapter 5). End to end: worker loop, judging, a checkpoint and restore, Sentinel findings, and one deliberate escalation through every rung. | All. **Done 2026-10-07, on real failures rather than a staged test:**<br>• **The worker loop and judging:** chapters 2–5.3 built by the 14B (LFS-009 to -020).<br>• **Checkpoints:** taken after each chapter and before each high-risk section. A **restore** (`before-012`, 6.3 s) undid 5.5's GRUB damage on both disks (LFS-020).<br>• **The ladder, unattended:**<br>&nbsp;&nbsp;– **5.3 GCC pass 1:** rung 2 (nudge), then rung 3 twice (the tutor diagnosed the out-of-memory, then the half-linked `cc1`). Built on attempt 5, with swap added (LFS-019).<br>&nbsp;&nbsp;– **5.5 glibc:** rung 2, then rung 3, where the tutor chose "needs Paul" (a cause one section back, plus damage to the build machine), then rung 4 with the build paused (LFS-020).<br>• **Sentinel:** findings logged and ingested; tutor lessons become KB entries (LFS-T…).<br>• **Not exercised:** a phone notification. No channel, by Paul's choice for now. |

## Phase D — Stage 1: the command-line OS

| # | Stage | Who |
|---|---|---|
| D1 | **Prepare the host and partitions** (LFS chapters 2–4) on the build VM's data disk: a GPT table with an EFI system partition, for UEFI. | llm-chat |
| D2 | **The cross-toolchain and temporary tools** (chapters 5–7). The subtlest part of LFS: expect tutoring. A checkpoint after each chapter. | llm-chat; Claude tutors |
| D3 | **The final system** (chapter 8), with each package's test suite. Test results are compared with the failures the book says to expect. | llm-chat |
| D4 | **System configuration** (chapter 9): systemd, networking, clock, locale, fstab. | llm-chat |
| D5 | **The kernel and boot** (chapter 10, plus BLFS for UEFI).<br>• **The kernel:** the latest stable, so the book's configuration notes are adapted to it. Expect tutoring.<br>• **The bootloader:** GRUB for EFI, efivar and efibootmgr, installed to the ESP. | llm-chat; Claude tutors |
| D6 | **Proof: it boots.** **Root's password (Paul, 2026-10-09): set by Paul at first boot.** Root stays locked through the build (LFS-035). As the last step before imaging, the controller runs `passwd -d root; chage -d 0 root`, so the first console login asks Paul for a new password at once. SSH refuses empty passwords and root password logins by default, so the VNC console is the only way in until then. The disk becomes an image (B2), booted as an instance under OVMF. On the serial console it reaches a login; the network comes up; another machine can SSH in. Basic checks pass (filesystems, `systemctl --failed` empty, the compiler builds a test program), and util-linux's root test suite (`bash tests/run.sh`, with `CONFIG_SCSI_DEBUG`), which the book's 8.81 says to run only on the booted system (LFS-041). | **Booted 2026-10-10** (image `lfs-13-1-run1`, instance `fb38f2ed`): OVMF loaded GRUB from the image's EFI partition; kernel 7.2.9 reached `lfs login:` with every unit OK (network, `/boot/efi`, D-Bus, logind, sshd); DHCP gave 10.250.101.193; from the coordinator, SSH answers and offers public keys only (passwords refused). Paul then turned password logins on (root login still off) until hardening (H3a). Before imaging: root's password cleared and expired, `/etc/machine-id` empty, the disk unmounted. Found on the way: F-237 (an instance disk smaller than its image), fixed. **Still to do, after Paul's first login** (root's password, his user and key): `systemctl --failed`, a test compile, util-linux's root test suite (LFS-041). |
| D7 | **Write-up.** What llm-chat did, where it needed tutoring and why, and the findings and the knowledge base so far. A checkpoint image of stage 1 is kept as the base for stage 2. | Claude; llm-chat drafts |

## Phase E — Stage 2: the Wayland UI

| # | Stage | Who |
|---|---|---|
| E1 | **Choose the compositor**, which sets the Wayland package list for A1. Two candidates: one BLFS documents, or one outside it (for example Sway), at the cost of more tutoring. Claude checks what the current BLFS covers first. | **Paul** decides |
| E2 | **The graphics and input stack:** libdrm, Mesa (virtio-gpu, or llvmpipe for software rendering: the hosts have no GPU), libinput, seatd, Wayland and wayland-protocols, fonts. | llm-chat; Claude tutors |
| E3 | **The compositor, a terminal and a session** that starts at login. | llm-chat |
| E4 | **Proof: it shows.** The image boots; the compositor starts; a terminal opens and runs a command. Shown on the graphical console (B3), with a screenshot checked by Claude. | All |
| E5 | **Write-up,** as D7. | Claude; llm-chat drafts |

## Phase F — Run 2: the 14B and Sentinel alone

Agreed 2026-10-08 (Paul): once run 1 has produced a bootable OS, build it again with **only llm-chat and Sentinel**. No tutor sessions, and no Claude in the loop. The question is whether the system *learned*: run 1's failures, findings and tutor lessons are now Sentinel's knowledge base.

| # | Stage | Who |
|---|---|---|
| F1 | **A fair baseline.** Freeze the controller at its end-of-run-1 version, and record what run 1 cost: stops per chapter, which rung cleared each, attempts, model time.<br>• **Classify every run-1 stop** as a **controller fault** (fixed for good, so it can't recur: LFS-020 to -028) or a **14B mistake** (inventing reasons to omit, deleting to retry, ignoring notes, copying book commands).<br>• **Only the second kind measures learning.** | Claude |
| F2 | **The ladder without rung 3:** the tutor timer is off for the run. Stuck: retries, then Sentinel's nudge, then Paul.<br>• **Run 1's lessons as plain notes:** in a fresh build, the controller can give the 14B the knowledge base's lessons for a section *before* it plans (not only after a stop). This is a design choice to test both ways. | Claude builds; Sentinel runs |
| F3 | **The run,** on a fresh build VM, same book and versions, through D6 (boot). | llm-chat; Sentinel |
| F4 | **The comparison.** Per chapter:<br>• stops and attempts;<br>• how many stops the nudge cleared;<br>• which run-1 mistakes recurred;<br>• model time;<br>• whether it still boots.<br>Plus a verdict on the knowledge base: which entries helped, and which matched on noise. | Claude; llm-chat drafts |

## Phase G — Supply chain: what goes into the OS

Agreed 2026-10-08 (Paul): give the OS a fighting chance from the start. Scanning about 100 packages' source for backdoors mostly finds noise; the real xz-utils backdoor (2024) was in the release **tarball** but not the project's git, and was found by a slow SSH login, not a scanner. So the emphasis is provenance, known flaws and build behaviour, with review aimed where an attack would land.

| # | Stage | Who |
|---|---|---|
| G1 | **Signatures.** Check upstream's GPG signature for every source that has one, against pinned keys, as the kernel's already are (A2). Sources with only a checksum are listed as such. | Claude |
| G2 | **Tarball against git.** For each source with a public repository, compare the release tarball with the tagged commit. Differences beyond the generated files a release normally adds (configure, Makefile.in, docs) are flagged for review. **This is the check that would have caught xz.** | Claude builds; llm-chat reviews the flags |
| G3 | **Known vulnerabilities.** Match every pinned package version against OSV (and NVD) for CVEs, plus the LFS/BLFS security advisories. It runs with the weekly `lfs_update` job (A5), and a finding becomes a report and a Sentinel finding. | Claude builds; the scheduler runs |
| G4 | **Build-time behaviour.** Builds are already offline. Also record, per package, the files its build reads and writes outside its own tree and `$LFS/usr` (an strace/audit pass on a clean build). Anything unexpected is flagged: xz's trigger hid in a build file. | Claude builds; Sentinel watches |
| G5 | **Targeted review** of the attack surface, not of everything:<br>• sshd;<br>• PAM and shadow;<br>• systemd's network-facing parts;<br>• glibc's resolver;<br>• the kernel config.<br>Static analysis (cppcheck/semgrep) on these, with the 14B triaging and Claude checking what it marks real. | llm-chat; Claude checks |
| G6 | **Review updates as diffs.** When A5 finds a new version of a package, the diff from the pinned version is reviewed before it's accepted. A diff is small and reviewable, and it's where a supply-chain attack arrives. | llm-chat; Claude on escalation |

## Phase H — Hardening the build

Hardening changes how everything is compiled. It goes into a **third run** (after F), so run 2 stays comparable with run 1.

| # | Stage | Who |
|---|---|---|
| H1 | **Compiler hardening, system-wide:** PIE, `-fstack-protector-strong`, `_FORTIFY_SOURCE=3`, full RELRO and BIND_NOW, `-fstack-clash-protection`, and CET (`-fcf-protection`) where the toolchain supports it. Set as defaults in GCC's build (chapter 8) and checked on every binary afterwards (`checksec`-style). | llm-chat; Claude tutors |
| H2 | **Kernel hardening:** the Kernel Self-Protection Project's recommended options, checked by `kernel-hardening-checker`, with each one not taken recorded and justified. | llm-chat; Claude tutors |
| H3 | **A minimal system:** no package beyond stage 1's need, nothing listening by default except sshd, an nftables firewall that denies by default, and systemd service sandboxing for every service shipped. | llm-chat |
| H3a | **SSH keys only** (Paul, 2026-10-10). Run 1 was built keys-only, but adding a key at first boot through the VNC console proved awkward, so on run 1's booted image Paul turned `PasswordAuthentication` back on (root login stays off). Hardening restores keys only: `PasswordAuthentication no` and `KbdInteractiveAuthentication no`, with each user's key in place first. It needs a way to provision a key at first boot that doesn't mean typing it on the console. | llm-chat; Paul |
| H4 | **Proof:** a scan of the booted image. Hardening flags on every binary, kernel checks, open ports, failed or unsandboxed units. Plus the D6 boot checks. | All |

## Phase I — Login: multi-factor authentication

Agreed 2026-10-08 (Paul): the OS requires MFA. Paul would rather not use an off-the-shelf authenticator, because it's well known, but accepts one as the proof.

Claude's view, for the decision (I1):
- **Being well known isn't where a scheme's strength comes from.** TOTP (RFC 6238) is strong because of its secret key; a home-made *scheme* would be weaker, because it hasn't had years of people trying to break it (Kerckhoffs's principle).
- **The part to make our own is the implementation and integration,** on a standard algorithm.

| # | Stage | Who |
|---|---|---|
| I1 | **Choose the factors.** Options, strongest first:<br>• **FIDO2 hardware keys** (e.g. a YubiKey) via `pam_u2f` at the console and `ed25519-sk` keys for SSH: phishing-resistant, and no authenticator company involved. Needs a key (or two: a spare).<br>• **TOTP from our own PAM module,** written from RFC 6238 and tested against the RFC's published vectors. Any authenticator app works with it.<br>• **Both:** FIDO2 as the main factor, TOTP as the fallback. | **Paul** decides |
| I2 | **Build it.** For TOTP: our own PAM module (C, with the RFC test vectors as its tests). For FIDO2: libfido2 and pam_u2f from source, through the G checks like every other package. | llm-chat; Claude tutors and reviews |
| I3 | **Every way in:** console login, SSH (key plus second factor), `sudo` re-authentication, and what root's console does. | llm-chat; Claude tutors |
| I4 | **Recovery, designed before it's needed:** a lost key or phone must not lock Paul out of his own machine, nor open a back door. One-time recovery codes, kept offline, and a documented console path. | Claude; **Paul** approves |
| I5 | **Proof:** each way in refuses one factor alone and accepts both. A wrong code is rate-limited and logged, and Sentinel sees the attempts. | All |

## Order and dependencies

- **Before anything:** llm-chat is at its best.
- **Phase A** can start first. The A1 Wayland list waits for E1.
- **Phase B** runs alongside A. **B5** (build-machine access) waits until run 1 is complete, with the other new work, unless Paul pulls it forward; it doesn't touch the running build's controller.
- **Phase C** needs B1 and B4.
- **Phase D** needs C6.
- **Phase E** needs D6. Whether stage 2 waits for H and I is an open decision.
- **Phase F** needs D6 (run 1 boots).
- **Phases F–I wait until everything before them is complete** (Paul, 2026-10-08: focus on run 1 first). Within them: G4 needs a clean build (F's run is one), and G5 and G6 follow.
- **Phase H** is run 3: after F, so F stays comparable with run 1.
- **Phase I** needs a booted stage 1. I1 can be decided any time.

## Scale (estimates, to be replaced by measurement)

| | Estimate |
|---|---|
| Sources | ~0.5 GB LFS, ~0.15 GB kernel, 1–3 GB BLFS (UEFI + Wayland) |
| Compile time, stage 1 | 10–20 h on 6 threads |
| Compile time, stage 2 | a multiple of stage 1; Mesa and its dependencies dominate |
| Model time | small beside compiling: one section's proposal is minutes at ~2.4 tok/s |
| Calendar time | weeks, set by struggles and tutoring, not compute |

## Risks

- **The 14B's limits:** the toolchain chapters, kernel configuration, the bootloader and BLFS dependency order will need Claude. Mitigation: one section at a time, the book as context, a knowledge base that grows, and escalation.
- **Tutor usage:** each headless session uses Paul's Claude usage. Mitigation: the daily cap, conservative stall detection, and the knowledge base reducing repeats.
- **No GPU:** stage 2 renders in software or through virtio-gpu. That's enough to prove a working UI, not to make it fast.
- **A "latest stable" kernel moves:** pinned at A1, updated only by decision (A5 reports).
- **Long builds on shared hosts:** the build VM keeps the reserved cores free, and students' llm-chat use comes first where they compete.
- **Licences:** the books are CC BY-NC-SA; private, non-commercial use is fine. The sources keep their own licences.

## Model evaluation (agreed 2026-10-07, after C6)

Paul asked whether to move up from the 14B. Agreed:
- **Keep the 14B for now.** C6 gathers evidence on the hard sections (toolchain, kernel, GRUB). So far the 14B's failures were structural (LFS-011, LFS-012), not misunderstanding.
- **Then evaluate** Qwen3-14B and a 30B mixture-of-experts coder (e.g. Qwen3-Coder-30B-A3B: ~3B active per token, so possibly faster than the dense 14B on these CPUs):
  - speed with `api/llm-bench.py`;
  - quality on a fixed set of LFS tasks: plan quality, repairs, judgements, how often tutoring is needed.
- **Constraints:**
  - **RAM:** these machines take **32 GB at most** (no upgrade path). A dense 32B (~20 GB) can't run beside the 8 GB build machine on Llwyn-y-Groes while the students' 14B is there.
  - **Freeing Llwyn-y-Groes:** for the trial, students can move to Stourport (Paul, 2026-10-07). That frees Llwyn-y-Groes for the build machine plus one ~20 GB model.
- **Decide on evidence**, with the quality floor "14B or better" unchanged.

## Open decisions

- **E1:** the compositor.
- **A5:** how often to check (weekly suggested), and whether package-version news is wanted or only releases and advisories.
- **C5:** the daily cap on tutor sessions.
- **F2:** whether the 14B gets the knowledge base's lessons for a section before planning, or only after a stop. Run both, or choose.
- **I1:** the MFA factors: FIDO2 keys, our own TOTP module, or both. FIDO2 needs keys bought.
- **Order:** does stage 2 (E) wait for hardening (H) and MFA (I)?
- **B5.2:** whether `viewer` may also read the chroot's `/sources` build trees, or only the logs.
