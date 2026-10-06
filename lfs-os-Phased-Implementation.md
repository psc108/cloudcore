# A bootable OS from Linux From Scratch, built by llm-chat — Phased Implementation

**Status:** draft for review, 2026-10-06. Nothing here is started. **Owner:** Paul Scott.

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
| A1 | **Inventory.** Pin the current stable LFS systemd book. The source list comes from its `wget-list-systemd`, the checksums from its `md5sums`. Pin the latest stable kernel, checksummed by kernel.org's `sha256sums.asc`. From the matching BLFS book, list the packages for UEFI boot (efivar, efibootmgr, popt, GRUB for EFI, and their dependencies) and for the Wayland stack (after decision E1). The result is one manifest: file, version, upstream URL, checksum, book section. | Claude |
| A2 | **A mirror script, `api/lfs-mirror.py`.** Downloads everything in the manifest into `artifacts/lfs/<lfs-version>/`, `artifacts/kernel/` and `artifacts/blfs/<blfs-version>/`.<br>• **Sources:** the LFS mirrors, which hold a release's whole set in one place, with upstream as fallback.<br>• **Verification:** each file against the book's checksum; the kernel's checksum file by its PGP signature too.<br>• **Behaviour:** resumable, `--dry-run`, logs to stderr, exits non-zero on any mismatch. | Claude |
| A3 | **Into the repo and onto the peer.** Mirrored files go into `sync-index.json` and reach Llwyn-y-Groes through the existing repo sync. Space: about 5–10 GB in all; Stourport has 692 GB free, Llwyn-y-Groes 593 GB. | Claude |
| A4 | **The books into llm-chat's corpus.** The LFS and BLFS books for the pinned versions, so the 14B reads the exact text for the release it's building. Section by section, alongside the build knowledge base. | Claude |
| A5 | **A scheduler job, `lfs_update`.** Weekly, modelled on `kiwix_update`. It checks for:<br>• a new stable LFS or BLFS release;<br>• new book errata and security advisories;<br>• a new stable kernel;<br>• newer versions of the packages in the manifest.<br>It **reports and never replaces**: a dashboard entry and a Sentinel finding, with what changed and why it matters (for example, an advisory for a package we've built). Moving up is a decision, mirrored side by side with the pinned set. | Claude |

## Phase B — Platform capabilities (CloudCore)

Checked 2026-10-06: CloudCore has none of these yet.

| # | Stage | Who |
|---|---|---|
| B1 | **Disk snapshots.** Create, list and restore snapshots of an instance's disks, through the API and the dashboard. The build's checkpoints (C3) depend on it. | Claude |
| B2 | **Boot a custom image, with UEFI.** Import a disk image built by us as an image, and boot it as an instance under OVMF firmware. Its serial console is captured, as instances' consoles already are. | Claude; **Paul** installs OVMF on both hosts (sudo) |
| B3 | **A graphical console.** A VNC or SPICE display for an instance, reachable only through the dashboard, plus a screenshot API so the GUI stage can be checked by Claude as well as seen by Paul. | Claude |
| B4 | **A build machine in the lab.** A new lab VM purpose, `lfs-build`. It differs from the lab's disposable VMs:<br>• **Lifetime:** days, not an hour, and never idle-reaped while a build is running.<br>• **Disks:** a data disk for the LFS partitions.<br>• **Size and place:** as many cores as placement allows, on the host that compiles fastest. That needs a compile benchmark beside `llm-bench`. | Claude |

## Phase C — The build protocol: llm-chat, the lab, Claude and Sentinel

| # | Stage | Who |
|---|---|---|
| C1 | **The build journal and task queue.** One task per book section, in book order, each with its state (waiting, running, done, stuck, escalated). It records who did what, every command and result, and every lesson. This is the documentation "between the two of us": llm-chat writes its working, and Claude writes its tutoring. | Claude |
| C2 | **llm-chat's worker loop.** For each task:<br>1. Read the book section, plus the knowledge base for that package.<br>2. Propose the commands.<br>3. The lab runs them on the build VM, in the chroot where the book says so.<br>4. Judge the result against the book's expected results, including the test-suite results the book says to expect.<br>5. Repair, then journal.<br>One section at a time keeps the 14B's context small. | llm-chat; Claude builds it |
| C3 | **Checkpoints.** A snapshot at the end of each chapter, and before every high-risk section (the toolchain passes, glibc, GCC, the kernel, the bootloader). A section that breaks the system beyond repair is rolled back to the last checkpoint, not patched over. | Claude |
| C4 | **Sentinel watching.** Every build log goes to Loki (promtail on the build VM). Sentinel:<br>• **Logs problems:** every problem becomes a finding in a separate `LFS-Findings-Log.md` (same F-NNN format), with its cause and fix.<br>• **Keeps the knowledge base:** findings, lessons and the journal are ingested into a build knowledge base.<br>• **Detects stalls by build progress markers** (section started or finished, test results), never by log silence: GCC's build legitimately runs for an hour. | Claude builds; Sentinel runs |
| C5 | **The escalation ladder.**<br>1. **llm-chat retries,** with the lab's repairs and other approaches.<br>2. **Sentinel nudges llm-chat** with matching knowledge-base entries, for a fresh attempt.<br>3. **Sentinel starts a tutor session:** headless Claude Code (`claude -p`) on Stourport, given the stuck task, its logs and the matches. Guardrails:<br>&nbsp;&nbsp;• permissions limited to the build VM and the journal;<br>&nbsp;&nbsp;• one session at a time, with a daily cap;<br>&nbsp;&nbsp;• every session logged and ingested.<br>&nbsp;&nbsp;Claude explains the lesson in the journal, and llm-chat carries on.<br>4. **Paul is told** by phone notification, and that branch of the build pauses. | All; **Paul** sets up Claude Code's credentials on Stourport for headless runs |
| C6 | **Prove the loop on a small slice.** For example, the first pass of binutils (LFS chapter 5). End to end: worker loop, judging, a checkpoint and restore, Sentinel findings, and one deliberate escalation through every rung. | All |

## Phase D — Stage 1: the command-line OS

| # | Stage | Who |
|---|---|---|
| D1 | **Prepare the host and partitions** (LFS chapters 2–4) on the build VM's data disk: a GPT table with an EFI system partition, for UEFI. | llm-chat |
| D2 | **The cross-toolchain and temporary tools** (chapters 5–7). The subtlest part of LFS: expect tutoring. A checkpoint after each chapter. | llm-chat; Claude tutors |
| D3 | **The final system** (chapter 8), with each package's test suite. Test results are compared with the failures the book says to expect. | llm-chat |
| D4 | **System configuration** (chapter 9): systemd, networking, clock, locale, fstab. | llm-chat |
| D5 | **The kernel and boot** (chapter 10, plus BLFS for UEFI).<br>• **The kernel:** the latest stable, so the book's configuration notes are adapted to it. Expect tutoring.<br>• **The bootloader:** GRUB for EFI, efivar and efibootmgr, installed to the ESP. | llm-chat; Claude tutors |
| D6 | **Proof: it boots.** The disk becomes an image (B2), booted as an instance under OVMF. On the serial console it reaches a login; the network comes up; another machine can SSH in. Basic checks pass (filesystems, `systemctl --failed` empty, the compiler builds a test program). | All |
| D7 | **Write-up.** What llm-chat did, where it needed tutoring and why, and the findings and the knowledge base so far. A checkpoint image of stage 1 is kept as the base for stage 2. | Claude; llm-chat drafts |

## Phase E — Stage 2: the Wayland UI

| # | Stage | Who |
|---|---|---|
| E1 | **Choose the compositor**, which sets the Wayland package list for A1. Two candidates: one BLFS documents, or one outside it (for example Sway), at the cost of more tutoring. Claude checks what the current BLFS covers first. | **Paul** decides |
| E2 | **The graphics and input stack:** libdrm, Mesa (virtio-gpu, or llvmpipe for software rendering: the hosts have no GPU), libinput, seatd, Wayland and wayland-protocols, fonts. | llm-chat; Claude tutors |
| E3 | **The compositor, a terminal and a session** that starts at login. | llm-chat |
| E4 | **Proof: it shows.** The image boots; the compositor starts; a terminal opens and runs a command. Shown on the graphical console (B3), with a screenshot checked by Claude. | All |
| E5 | **Write-up,** as D7. | Claude; llm-chat drafts |

## Order and dependencies

- **Before anything:** llm-chat is at its best.
- **Phase A** can start first. The A1 Wayland list waits for E1.
- **Phase B** runs alongside A.
- **Phase C** needs B1 and B4.
- **Phase D** needs C6.
- **Phase E** needs D6.

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

## Open decisions

- **E1:** the compositor.
- **A5:** how often to check (weekly suggested), and whether package-version news is wanted or only releases and advisories.
- **C5:** the daily cap on tutor sessions.
- **B3:** VNC or SPICE.
