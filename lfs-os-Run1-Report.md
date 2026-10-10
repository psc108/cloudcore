# LFS OS — Run 1 Report (D7)

**Status:** run 1 complete, 2026-10-10. Stage 1 (LFS 13.1-systemd, kernel 7.2.9, UEFI) built by llm-chat's 14B and booted as its own VM. **Owner:** Paul Scott. **Sources:** the build journal (CloudCore's LFS tables, build 1), the tutor's session records, `LFS-Findings-Log.md` (LFS-001 to LFS-045), `haFullStack-Findings-Log.md` (F-237), and `lfs-os-Phased-Implementation.md` (phases A–D).

## 1. Summary

- **The outcome:** a bootable Linux From Scratch system. Under OVMF it loads GRUB from its own EFI partition, boots kernel 7.2.9, and reaches `lfs login:` with no failed units. It gets its address by DHCP, accepts SSH, and compiles and runs a test program. util-linux's root test suite passed 361 of 367 tests; the 6 failures are explained in section 9.
- **Who did the work:** the 14B planned and ran every one of the 145 tasks. It read the book's section, wrote a plan, ran it on the build machine, and repaired its own failures. Claude tutored and fixed the controller; Sentinel watched and nudged; Paul made the decisions that were his (root's password, chapter 9's settings, SSH policy).
- **The time:** 3 days 4.6 hours, from 2026-10-07 11:24 to 2026-10-10 15:59 UTC. GCC (8.32, 15.0 hours) and Glibc (8.5, 10.6 hours) took a third of it.
- **The main lesson:** most stops were **the controller's faults, not the 14B's**. Of the 32 findings logged during the run, 25 were in the controller (how steps ran, how plans were checked, how the book was read), 5 were the 14B's own mistakes, 1 was Claude's tutoring, and 1 was the build machine's memory. Each controller fault was fixed when found, so later chapters ran much more cleanly than earlier ones (chapter 9: 8 of 8 first time).
- **Four safety incidents** were caught and closed with guards (section 7). None reached Paul's machines; one damaged the build (three deleted unit files), which was repaired from the packages' own sources.

## 2. What run 1 was

| Role | Who | What it did |
|---|---|---|
| **Builder** | llm-chat's 14B (the quality floor: never a smaller model) | Planned each section from the book's text and commands, ran it, judged the output, proposed repairs |
| **Controller** | `lfs_worker.py` on the coordinator | Served tasks in book order, checked plans, ran steps on the build VM, took checkpoints, kept the journal |
| **Rung 1** | the 14B | Up to 2 repairs per step, 4 per task |
| **Rung 2** | Sentinel | A nudge with matching knowledge-base entries |
| **Rung 3** | the tutor (Claude, a separate session with no tools) | A diagnosis and a lesson, or "needs Paul" |
| **Rung 4** | Paul (in practice, Claude in this session) | The build paused for a person |
| **Decisions** | Paul | Root's password at first boot, chapter 9's settings, SSH policy, what to defer |

The build VM (Ubuntu, `standard.large`, 50 GB data disk as `/mnt/lfs`) sat on the lab network; the controller reached it over SSH and never gave it CloudCore's private key.

## 3. The run in numbers

| Chapter | Tasks | First attempt | Attempts | Repairs proposed | Rung 2 | Rung 3 | Rung 4 | Hours |
|---|---|---|---|---|---|---|---|---|
| 2 Preparing the host | 5 | 1 | 13 | 6 | – | – | – | 4.0 |
| 3 Packages and patches | 1 | 0 | 2 | 5 | – | – | – | 0.4 |
| 4 Final preparations | 3 | 1 | 5 | 2 | – | – | – | 0.5 |
| 5 Cross toolchain | 5 | 0 | 24 | 21 | 4 | 12 | 4 | 10.7 |
| 6 Temporary tools | 17 | 16 | 18 | 0 | 1 | – | – | 2.6 |
| 7 Chroot, more tools | 14 | 9 | 24 | 15 | 6 | 6 | 2 | 5.9 |
| 8 System software | 83 | 71 | 122 | 79 | 18 | 29 | 11 | 48.0 |
| 9 System configuration | 8 | 8 | 8 | 0 | – | – | – | 1.0 |
| 10 Making it bootable | 3 | 2 | 4 | 0 | – | – | – | 2.8 |
| BLFS (UEFI tools, OpenSSH) | 4 | 4 | 4 | 1 | – | – | – | 0.5 |
| 11 The end | 2 | 2 | 2 | 0 | – | – | – | 0.2 |
| **Total** | **145** | **114 (79%)** | **226** | **129** | **29** | **47** | **17** | **76.6** |

Rung columns count escalation records in the journal; a rung-3 record is written when a session is requested and again with its outcome. **The tutor ran 22 sessions** (Claude Opus, 11 minutes in all, US$6.50): 16 ended "retry with the lesson", 6 "needs Paul". The daily cap (6 sessions) was reached on 2026-10-08 and 2026-10-10, sending later stops straight to rung 4.

**Sections that took more than two attempts:** 2.2 (6), 5.2 (4), 5.3 (5), 5.4 (6), 5.5 (3), 5.6 (6), 7.3 (3), 7.4 (3), 7.5 (5), 8.2 (3), **8.5 Glibc (10)**, 8.23 (4), 8.30 (3), 8.32 (5), **8.36 Gettext (7)**, 8.64 (3), 8.65 (3), 8.72 (3), 8.84 (6).

**The longest:** 8.32 GCC 15.0 h (4.6 hours of tests lost once to LFS-036), 8.5 Glibc 10.6 h, 5.3 GCC pass 1 3.0 h, 2.2 2.7 h, 5.5 2.7 h, 8.36 2.7 h.

## 4. Where help was needed, and why

The 32 findings logged during the run (LFS-014 to LFS-045), by whose fault:

| Fault | Count | Findings | The pattern |
|---|---|---|---|
| **The controller** | 25 | 015, 017, 018, 021–031, 033, 034, 036–043, 045 | How steps ran (lost `cd`, wrong tree, `set -e` against the book's ignorable errors), how plans were checked (too strict on templates, too loose on omissions and replacements), how the book was read (reading sections, examples in notes, post-boot tests), and the worker's own robustness (context overflow, a crash nobody noticed) |
| **The 14B** | 5 | 014, 016, 020, 035, 044 | Misreading the book's alternatives, inventing reasons to skip commands, running a dangerous command on the wrong machine, setting a credential, leaving out a whole command when told to drop one line |
| **Claude's tutoring** | 1 | 032 | A "change nothing" lesson that forgot a placeholder (044's note was also ambiguous) |
| **The platform** | 1 | 019 | GCC pass 1 ran the build VM out of memory; 8 GB of swap fixed it |

Plus **F-237** at boot (an instance's disk smaller than its image), a CloudCore bug.

**What the controller faults had in common:** the book is written for a person at a keyboard who knows which lines are examples, which errors are harmless, and which steps belong to another machine or another time. Every one of those judgements had to be made explicit: `_SKIP_COMMANDS`, reading sections, `_ERRORS_EXPECTED`, the placeholder and template rules, the post-boot test move. Run 2 starts with all of them.

**What the 14B did well:**
- Planned and ran 114 sections correctly first time, including all of chapters 6 and 9 and the kernel, GRUB and fstab once the notes were clear.
- Often diagnosed correctly ("the target does not exist", "the book says these warnings can be ignored") even when its fix was wrong.
- Followed a specific lesson reliably once it was concrete: exact commands, exact numbers.

**What it did badly:**
- **Under pressure, it reached for the nearest command that would make the error go away:** deleting the files strip complained about (LFS-042), a different `make` target (LFS-045), a password (LFS-035).
- **Instructions about part of a command** ("leave out this line") were applied to the whole command (LFS-044), even after a clearer second note.
- **It could not change one line inside a long command** without rewriting or fragmenting it (LFS-038, LFS-042).
- **Rung 2's nudges were often irrelevant, and it acted on them anyway** (SN-20).

## 5. The escalation ladder in practice

- **Rung 1 (repairs)** fixed most transient failures, and caused most of the damage (section 7). The guards now limit what a repair may do, not just how many it gets.
- **Rung 2 (Sentinel)** fired 29 times. Early nudges matched noise; later ones improved as the run's own findings and lessons entered the knowledge base (LFS-T lessons). Measuring which nudges helped is SN-20.
- **Rung 3 (the tutor)** was right in nearly every session, including the 6 times it said "needs Paul": those were all controller faults it could see but not fix. The daily cap was spent mostly on controller faults that are now fixed (SN-22).
- **Rung 4** came to Claude in this session 17 times, usually for a controller fix and a ladder reset.

## 6. What changed in the controller during the run

The worker gained, in order of discovery: the cwd trap between steps (LFS-018), swap (019), the dropped-line check and its template rules (020, 038, 040, 043), the version note (021), the `as_the_book` plan (031), lesson supersession and ladder resets (024), reading sections and example skips (029, 034, 040), the shared evidence judge (030, 036), the context fit for prompts (037), the heartbeat and stall watch (037), resume from the failed step (CC-95), the credential guard (035), errors-expected sections (042), the deletion guard (015, 042), the omission guard (044), the BLFS units tree and the replacement check for repairs (045). Offline tests grew from none to 74 plan checks and 8 resume cases.

## 7. Safety incidents

| Finding | What happened | Harm | Guard now |
|---|---|---|---|
| **LFS-015** | A repair deleted the freshly delivered sources | Re-delivered | Repairs may delete only inside the package's tree or `/tmp` |
| **LFS-020** | The 14B ran `grub-install` as root on the build VM's own disk | The build VM's boot record; restored from a checkpoint | Checkpoints before high-risk sections; firmware-writing steps skipped |
| **LFS-035** | A repair set root's password to `newpassword` | Root locked again; the password never used | The credential guard; root's password is Paul's, set at first boot |
| **LFS-042** | Repairs deleted three systemd unit files strip complained about | Restored verbatim from systemd and D-Bus sources | The deletion guard covers single files; the book's ignorable errors honoured |

Two of Claude's own mistakes are recorded too: a `pkill -f` that killed its own SSH session, and a kill by uid that stopped the build host's systemd-resolved (restarted automatically). Neither reached the LFS system.

## 8. Decisions Paul made

- **Root's password:** never in the build; locked throughout; cleared and expired before imaging; set by Paul at first login.
- **Chapter 9:** hostname `lfs`, `en_GB.UTF-8`, keymap `uk`, `Lat2-Terminus16`, DHCP via systemd-networkd on `en*`, systemd-resolved, `Europe/London`.
- **SSH:** built keys-only with no root login. At first boot, adding a key through the VNC console proved awkward, so Paul turned password logins on (root login still off) until hardening restores keys only (H3a).
- **Scope:** phases F–I and the Sentinel KB work wait until run 1 is complete; the phone channel for rung 4 is deferred.

## 9. The proof (D6)

| Check | Result |
|---|---|
| Boot | OVMF → GRUB (`EFI/BOOT/BOOTX64.EFI`) → kernel 7.2.9 → `lfs login:`; every unit OK |
| System state | `running`, no failed units |
| Filesystems | `/` ext4 on `/dev/sda2` read-write (fstab by UUID); `/boot/efi` vfat with the book's code pages; the whole 49 GB disk |
| Identity | hostname `lfs`, its own machine-id, `en_GB.UTF-8`, `uk`, Europe/London |
| Network | `ens3` by DHCP (10.250.101.193/24), default route, DNS via resolved |
| SSH | reachable from another machine; root login refused |
| Compiler | GCC built and ran a test program |
| util-linux root suite | 361 of 367 passed |

The util-linux failures: `su/shell` (the book configures `--disable-su`, so the test met shadow's `su`), `lsfd/mkfds-vsock` (no VSOCK in the kernel), `chrt/chrt-ext` (no sched_ext), and `lsfd/column-source`, `mount/set_ugid_mode`, `hwclock/show`, which are being confirmed from their diffs. The book warns the root suite needs `CONFIG_SCSI_DEBUG` and further BLFS packages for complete coverage.

## 10. What the knowledge base holds now

- **45 LFS findings** (LFS-001 to LFS-045), each with symptom, root cause, fix and verification, ingested into Sentinel.
- **The tutor's lessons,** ingested as `LFS-T<id>` entries; at least one (LFS-T2247) was a relevant nudge later in the run.
- **F-237** in the platform findings.
- **The code index** of CloudCore, re-run after each controller change.

## 11. What is kept

- **The image `lfs-13-1-run1`:** the LFS disk as it was before first boot (root's password expired, machine-id empty). The base for stage 2 and for comparing run 2.
- **The build VM** (stopped, not deleted) with its checkpoints, including `after-145-openssh-10-5p1`.
- **The booted instance `lfs-run1-boot`**, now carrying Paul's first-boot changes.
- **The journal** of build 1, in CloudCore's database, backed up nightly.

## 12. Into run 2 and beyond

- **Run 2 (phase F)** starts with every controller fix above, the 14B and Sentinel only. Its comparison with run 1 needs the LFS subjects (SN-03) and measured nudge relevance (SN-20).
- **The tutor's cap (SN-22):** revisit with these counts; most of the cap went on controller faults now fixed.
- **First-boot key provisioning:** needed before H3a can restore keys-only SSH without typing a key on the console.
- **Open:** the three unconfirmed util-linux failures.

## Document History

| Version | Date | Author | Change |
|---|---|---|---|
| v1.0 | 2026-10-10 | Paul Scott | Run 1 report (D7). |
