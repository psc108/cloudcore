# llm-chat — A Real Lab: Run the Advice, Keep What Works

> **Roadmap (2026-10-09):** open items are now tracked in `cloudcore-Roadmap.md` (CC-61, CC-62) and `sentinel-Roadmap.md` (SN-31, SN-32). This document is kept as the record of how the work was done.

Paul Scott | Direction agreed 2026-09-30

---

## Context

Linux Help answers are advice that nobody runs. F-174 and F-175 showed what
that costs:
- the model recommends packages that the lab can't install, or that may not exist;
- it has the student edit files that aren't there;
- it gives config that weakens security.

Today the only protection is a warning under each answer. The student's
Terminal can't test most advice either: it only sees `jammy main`, its
kernel lacks the netfilter/TUN/FUSE features that common tools need, and it
is a 1 vCPU / 256MB / 2GB VM.

Direct request: "we need to be able to install anything the llm might offer
as advice to install and then configure it. that way the student can safely
test the llm response and as a bonus we'll start collecting a decent corpus
of our own that is verified working response or not and what did/didn't
work in advice".

**Decisions (2026-09-30):**
- **Sizing is never tied to a machine.** Resources come from what the
  approved peers report at build time and from what the coordinator actually
  has at run time. No host names in templates or defaults; a single small host
  still works, with a smaller sandbox. ("someone coming behind us using this
  code will wonder why they can't run")
- **Every Linux Help answer is run automatically, step by step**, in a
  disposable microVM. It never runs in the student's own Terminal, which still
  runs only what the student clicks.
- **Answers verified in the lab are reused automatically** for grounding,
  labelled as lab-verified, not human-approved.

## What limits the lab today (measured 2026-09-30)

| Limit | Effect | Fix (stage) |
|---|---|---|
| Runtime apt sources: `jammy main` only | universe/multiverse packages (fail2ban, libpam-google-authenticator, docker.io, nodejs…) can't be installed; no `-updates`/`-security` | L2 |
| Guest kernel = Firecracker CI 6.1 config: no `NF_TABLES`, no xt `limit`/`recent`/`multiport`/`state`/`LOG`, no `TUN`, `WIREGUARD`, `FUSE`, `NFSD`, AppArmor; `MODULES` off | jammy's iptables (nft backend), ufw, fail2ban's ban actions, VPNs and FUSE tools fail even once installed | L1 |
| Terminal VM 1 vCPU / 256MB / 2GB scratch; per-run VM 256–512MB | databases, docker, JVMs and larger installs don't fit | L3 |
| Guest sshd is also the control channel (Terminal bridge, per-run executor) | advice that edits `sshd_config`/PAM and restarts sshd cuts off the session running it | L2 |

## Stages

| # | Stage | Status |
|---|---|---|
| L1 | **Guest kernel.** Build our own 6.1 guest kernel from Firecracker's CI config plus a fragment (nf_tables + expressions, the common xt matches/targets, NAT/masquerade, TUN, WireGuard, FUSE, NFSD, loop/dm), with a new `api/build-firecracker-kernel.sh` in a throwaway builder instance like the rootfs. Pinned artifact + sha256, TF + Ansible. Prove ufw, fail2ban (iptables and nft actions), nft, WireGuard and a FUSE mount in a guest | Done — 176-option fragment; ufw, fail2ban, nft, WireGuard, FUSE/TUN and AppArmor verified in a guest (F-176) |
| L2 | **Rootfs.** Full runtime apt sources (main, restricted, universe, multiverse; `-updates`, `-security`). A separate **control sshd**: its own port, config and host key, key-only, `UsePAM no`, used only by the Terminal bridge and the executor, so advice that reconfigures the normal sshd/PAM can't cut off the session. Rebuild the rootfs (F-171 procedure), re-pin the SHA | Done — whole-archive sources and baked lists, Ubuntu server userland, vsock control channel with a separate self-repairing sshd (F-177, F-179) |
| L3 | **Sizing from real resources.** Build time: the coordinator's flavor candidates run up to the largest flavor, and `/v1/peers/recommend` picks the best any approved peer can afford now (the mechanism already exists). Run time: Terminal and advice-run VMs size themselves from the coordinator's actual memory, cores and disk (reserve for its own services, divide by the allowed concurrent sessions, never claim every core), with floors below which a session is refused with a clear message rather than started half-sized | Done — sized from the coordinator's real free memory, cores and disk via the cgroup ledger; peer-placed coordinator from the recommender; verified 2 vCPU / 2GB / 16GB per sandbox (F-179) |
| L4 | **Advice runner.** Turn an answer into steps: `bash` blocks are *run*; `text`/config blocks tied to a named path are *write/append file*; editor invocations (`nano`, `vim`) are replaced by those writes; interactive commands run with stdin closed and a timeout, and are recorded as *interactive*. Run the steps in a fresh microVM (internet-only egress, as today). After each step, check what the answer relied on: packages exist and installed, referenced files/commands/units exist, services `is-active`, and config validators where one exists (`sshd -t`, `nginx -t`, `visudo -c`, `apachectl configtest`, `named-checkconf`, `fail2ban-client -t`, `systemd-analyze verify`…). Classify every step | Done — verified on real answers: fail2ban+ufw, MFA (interactive), nginx reverse proxy (lab_verified) (F-179) |
| L5 | **Page.** Show the run under the answer as it progresses ("Lab run: step 3/7…"), then per-step results in plain words ("fail2ban: installs ✓", "/etc/fail2ban/jail.local: didn't exist — the answer edits it but never creates it"). F-175's notices become facts from the run (e.g. "package X does not exist in Ubuntu 22.04") instead of guesses | Done — live Lab-run box under each answer, polled; notices updated (F-179) |
| L6 | **Corpus.** Sentinel stores each run: question, answer, the extracted steps, per-step command/exit/output tail/classification, verdict. Student-initiated Terminal runs from Linux Help are recorded the same way (source `student`). A Sentinel view of runs, filterable by classification | Done — Sentinel advice_runs table, API and Lab runs tab; live run recorded (F-179) |
| L7 | **Reuse.** Answers verified in the lab (definition below) become `lab_verified` in `grounding_log`. `find_match()` returns them after human-approved answers, labelled "Verified in the lab (date)". Failures become *known-bad facts* ("libfoo does not exist in Ubuntu 22.04"; "X ships no /etc/x/y.conf") added to the prompt when a question matches, so the model stops repeating them. Verify live with repeat questions | Done — lab_verified promotion and reuse after human-approved answers; lab facts in the prompt; verified live (F-179) |
| L8 | **Goal probes from a second machine.** A small prober VM on a private per-run bridge with the target (no uplink, no host address): real SSH logins recording every prompt (key first, then any second factor), `pamtester` for each PAM service the answer edited, a baseline before the steps so a lockout is told apart from "never worked", right and wrong TOTP codes for MFA answers, port reachability and HTTP from outside. A `reboot` in an answer is a real reboot on the same disk, and prompts are answered the way a person would (yes, the code from a newly shown secret, Enter). Only probes of what the answer changed decide the verdict; the rest are shown as information | Done — today's MFA answer lab_verified on its goal (F-180) |
| L9 | **Watch it and look around.** A live read-only log of everything done on the lab machine; afterwards the machine is kept (20 min, at most 2, token-gated) with login instructions (Terminal panel as student; the answer's own login with the lab's test password and an `oathtool` code), plus a Destroy button | Done — verified live, including an MFA login by following the instructions (F-185) |
| L10 | **Measure first.** ~40 real Linux Help questions across the kinds students ask (everyday commands; services, users, cron, systemd units, permissions; logins/SSH/MFA/firewalls/web; disks and LVM; networking; multi-machine; kernel/boot/hardware; explanations with no commands), each through the model and the lab. Report per kind: goal-verified, ran clean without a goal check, failed (and why: the advice or the lab), nothing to run. The numbers set the order of L11–L14 | Done — results below; reordered what follows |
| L10a | **Harness correctness first** (from L10). (1) a multi-line block fails if any line fails, not just the last; (2) search/diagnostic commands that find nothing or print permission noise aren't failures; (3) steps get a real terminal: full-screen tools (top, htop, less, watch) run briefly and quit, `fsck`/`fdisk` answer as a person would, password prompts get a lab password (reported); (4) more editors: `crontab -e`, `visudo`, `systemctl edit`; (5) prose edits ("change `#Port 22` to `Port 2222`", "uncomment `X`") applied to the file being edited; (6) INI sections merged whenever the section exists; (7) placeholders substituted the way a person would, and said so (`username`/`yourgroup` → created lab user/group, `/path/to/…` → a created path, `server_ip` → the prober or target, example IPs → the target), instead of skipped or run literally; (8) state changes (`hostnamectl`, `timedatectl set-…`, `useradd`, `chmod` …) count as changes; (9) exact-name package first for a missing command (htop, not bashtop); `<html>` isn't a placeholder; a netplan block goes to a file, not the directory | Done — same 40 answers re-run: 14 verified (was 8, 5 of them false), 21 failed (was 27); see results (F-189) |
| L10b | **Step-level repair.** When one step fails, the lab tries to fix *that step* before moving on, bounded per step and per run, every attempt in the live log and the corpus. First its own strategies (fast, no model): a missing package or command → command-not-found / `apt-cache search` / `apt-file` candidates tried in turn; index or 404 errors → `apt-get update` and retry; a missing directory for a file the step writes → create it; a service that won't start → read its journal. Then, if none works, the model is asked for a one-line fix to that step alone. The original answer keeps its honest verdict (failed at step N); the **repaired procedure** is recorded separately with what the lab changed, and is what can become a verified answer. Repairs that add a PPA/third-party repository or pipe `curl … \| sh` are marked and need human approval before reuse | Done — 6 of 7 targeted L10 failures repaired to verified; the answer keeps its own verdict (F-190) |
| L11 | **Try another way.** When a run fails, a fresh lab VM tries again: the model gets the question, its previous answer and what actually failed (the step, its real output, the failed check) and is asked for a *different method*. Up to N attempts (configurable); every attempt goes into the corpus; the first goal-verified one becomes the verified answer, and the student sees each attempt and its result. Failure facts state their cause as observed in the lab (e.g. "the lab kernel can't load modules"), so correct advice for real hardware is not taught as wrong | Done — not_testable verdict for lab limits; up to 2 different-method attempts with the machine's own diagnosis; repeats refused; bind9 and second-IP now goal-verified via another way (F-194) |
| L12 | **Grade the verification.** *Goal-verified* (a prober check of what the answer set out to do passed) is reused automatically, as now; *ran clean* (steps and checks passed, nothing tested the goal) is recorded but reused only after human approval; failed as today. Replaces today's single lab_verified, which can mean either of the first two | Done — goal_verified / ran_clean / failed; 17 goal checks by kind; only goal_verified reused automatically (F-191) |
| L13 | **A spare disk.** Each lab VM gets a blank extra disk for partitioning/LVM/RAID/mkfs advice, with the device names answers use (`/dev/sdb` …) pointed at it, so disk advice can be tried without destroying the VM's own writable layer (`/dev/vdb`) | Done — snapshot root (real ext4), two spare disks as /dev/sdb and /dev/sdc, interactive sessions and SQL clients; all 6 disk/NFS/Docker/swap/PostgreSQL questions goal-verified (F-192) |
| L14 | **Goal checks by kind, in the order L10 shows.** Candidates: users/groups (`id`, `getent`, sudo rights), systemd units (active and still active after a real reboot), cron (force-run and check its effect), permissions/ACLs (`stat`, access as another user), disks (`lsblk`, `findmnt`, survives reboot), networking (prober reaches the new address/route), and the prober as a real client for NFS/DNS/SSH-between-hosts | Not started |
| L15 | **Seal a fresh question set** before the fixes below, so they can't be shaped by it: 40 new questions, same mix as L10 (`reports/llm-chat-lab/sealed-questions-2026-10-03.json`, committed 983f43d). Not run, read for tuning or used in tests until L20 | Done 2026-10-03 |
| L16 | **Setup stage** (F-200 class A, 8 of the F5 failures). Before running, find what the question and answer presume exists: users and groups, installed or running services, files and directories, a second machine. The lab creates it from a **fixed menu** of setup actions (create user/group, add to group, install package from the repo, start service, make file/dir with stand-in content, make a partition), never shell from the model. The existing rules stay as the fast path; one model call returns the rest as JSON, validated against the menu. Every action is reported ("The lab set up user bob because the question assumes him") | Built 2026-10-04 (results below) |
| L17 | **Placeholders by kind** (class C, 8 F5 failures). General detection (`your-…`, `…_here`, `<…>`, `ALL_CAPS_WORD` where a value goes, `server_ip_address`-style names, undefined `$vars`), plus the model call above classifying each by kind: user, IP of the other machine, UUID, PID, service, package, path, domain. Filled from real lab facts (the prober's address, `blkid` of the spare disk, a real PID, the setup stage's user). Unfillable placeholders are reported, not run literally | Built 2026-10-04 (results below) |
| L18 | **Lab limits before running** (class D, 4 F5 failures). Categories, not commands: physical hardware (SMART/NVMe health, sensors and fans, GPUs, Wi-Fi radios, USB devices, firmware) and public-internet identity (certificates for real domains, public DNS, a public IP). Marked "can't be tested here" with the reason, before the run where the question makes it clear | Built 2026-10-04 (results below) |
| L19 | **Goal-check precision** (class B and L14's first candidates): checks fire on what the question asks, not keywords ("swappiness" isn't swap); client questions are checked as clients; source-restricted firewall rules are checked from inside and outside the allowed range | Built 2026-10-04 (results below) |
| L20 | **Measure once with the sealed set** (also F8 in the full-VM plan), through `/sandbox/linux-ask` on full VMs, graded by reading every run. Compare with F-200's held-out result (3/40 proven) | Done 2026-10-04 (results below) |
| L21 | **Goal checks from the model's reading, run by the lab** (from L20: 1/40 verified). The setup stage's model call also proposes what success means for the question, as checks from a fixed menu: user exists/in group/shell, path exists/owner/mode, file contains, service active/enabled/disabled, port listening or open/closed from the prober, default target, sysctl, read-only command output (allowlisted programs, no shell syntax). Each is run before the answer and after; only a **false→true** change proves the answer, and a failing model check is "not confirmed", never a failure. Fresh set sealed first: `sealed-questions-2026-10-04.json` (8e1c52d) | Built 2026-10-04 (659adf5); development run on the 2026-10-03 answers |
| L22 | **Measure L21 once with the 2026-10-04 sealed set**, as L20 | Done 2026-10-05 (results below) |
| L23 | **Behaviour checks** (from L22: checks of configuration, not behaviour, let a wrong answer pass). New menu kinds:
- **What a person would see:** a fresh login's environment (`login_env`); reading, writing, executing or listing *as a user* (`user_can` / `user_cannot`); sudo's own verdict on an exact command (`sudo_allowed` / `sudo_denied`); a real HTTP request from the other machine, with status, body text and redirect target (`http_from_other`).
- **What the system really does:** name resolution (`resolves`); running a unit once and checking it succeeds (`unit_runs_ok`); sshd's effective settings (`sshd_effective`).
- **Reboot:** `after_reboot`, which reboots the full VM once and re-checks.

A mode or owner the answer sets directly, or a file line it writes, is never proof unless the question names that value. Placeholder users in checks map to the lab's.

**Known limit:** the lab can't judge whether a setting means what the question wants (L22 #19: `ClientAliveCountMax 0` is accepted by sshd but disables the disconnect). Fresh set sealed first: `sealed-questions-2026-10-05.json` (2fc22fd) | Built 2026-10-05 (20f389f, 4ecdfcf). Development run on the L22 answers: 8 "verified", 7 genuine on reading (L22 grading of the same answers: 5), including #12 (a webteam member can write) and #13 (a fresh login sees the variable). The 8th, #19, is the known limit. The run's one error (#17): an answer firewalling all but SSH and HTTPS cut off the full VM's control sshd on 1022, so the lab now allows 1022 in ufw first |
| L24 | **Measure L23 once with the 2026-10-05 sealed set** | Done 2026-10-05 (results below): 13 lab-verified, **4 genuine** on reading |
| L25 | **A lab reader on another host** (a 7B for the lab's readings) | Switched off 2026-10-05: weaker checks than the 14B, and students' answers still slow. Replaced by `llm-chat-placement-Phased-Implementation.md` (the lab's own 14B, placed by measured speed) |
| L26 | **One check standard** (from L24: 9 of 13 verifications false or weak, most from the older hand-written checks):
- **Before and after for every check.** The hand-written goal checks now run before the answer as the model's do, and one already true shows nothing (L24 #32: "SSH refuses passwords" is the image's default).
- **"Allowed through the firewall" only while a firewall filters** (another port is blocked), else not evidence (#27, #30).
- **Configuration is not evidence:** the answer's own crontab entry (#11), `sshd -T` showing the answer's own line (#19), and the basic web-server probe (#16, nginx's default page) can fail a run but never verify one, except the probe for a pure install question.
- **Model checks:** owner or mode counts only if the question is about ownership or permissions (#24, #25: the mode of a directory the answer made). Web text counts only if the answer or the question supplied it; a bare status doesn't show a custom page (#16: "404 Not Found" is nginx's own).
- **New behaviour checks:** a new file in the directory gets the group (#13, not the setgid bit); another machine is shown the SSH banner (#19); a burst of connections is cut off (#30, a rate limit).

Fresh set sealed first: `sealed-questions-2026-10-06.json` (1f5b252, sha256 `3fc00be5…`) | Built 2026-10-06 |
| L27 | **Measure L26 once with the 2026-10-06 sealed set**, as L24 | Done 2026-10-06 (results below): 8 lab-verified, **6 genuine**, 1 weak, 1 false |
| L28 | **The lab's own handling** (from L27: about 11 of 16 failures and both unfinished runs were the lab's fault, not the answer's):
- **A working directory the student can write to.** Steps run there, and the setup stage puts presumed files there (F-221: #4, #5, #22, #39).
- **Checks that follow the question's direction** (F-222: #26, swap "must be active" for "turn swap off").
- **Example blocks:** a config block the answer shows as "it might look like this" is not an edit, and `<Directory /var/www/>` is not a file to edit (F-223: #19).
- **Servers started in the foreground** run in the background and are checked as running, not left to hit the step time limit (F-224: #32, #34).
- **The answer's own reboot** is followed through: reconnect, then go on (F-225: #23). #28's channel timeout after `ufw default deny` needs finding first.
- **Explanation questions** are never goal-verified by their demo commands (F-226: #40). A no-password sudo question is checked with `sudo -n` as the user (#9).
- **Smaller gaps:** a repair that runs a step as root makes the answer's own sudo retry fail (#12); a gateway outside the lab's network is a lab limit, not a failure (#27); the setup stage made `/opt/app/bin` but not the program the question names (#3), and no stub app on the port a proxy question names (#21).

Then measure once with a fresh sealed set, as before | Built 2026-10-06 (2c960eb, 66c66c2); measured as L29 |
| L29 | **Measure L28 once with the 2026-10-06b sealed set** | Done 2026-10-07 (results below): 11 lab-verified, **7 genuine**, 2 weak, 2 false |

## Results: the same 40 answers through each stage

The L10 answers are stored (`reports/llm-chat-lab/l10-results.json`) and re-run unchanged, so only the lab changes between columns. Per-run records: `l10a-rerun-same-answers.jsonl`, `l13-rerun-same-answers.jsonl`.

| | L10 (2026-09-30) | L10a | After L10b–L13 (2026-10-01) |
|---|---|---|---|
| Verified | 8, 5 of them false or weak | 14 | **19 goal-verified as written**: a check of the goal passed, a stricter test than before |
| … plus after the lab's repairs | — | — | **23 goal-verified** |
| Ran clean (nothing to check the goal) | — | — | 4 (5 after repairs) |
| Read-only, nothing changed (partial) | 6 | 5 | 5 |
| Failed | 27 | 21 | **12 as written, 7 after repairs** |

The 7 that still fail, by cause:

| # | Question | Cause |
|---|---|---|
| 28 | second IP address | **advice**: `nmcli` on "Wired connection 1", but Ubuntu Server uses netplan/networkd, not NetworkManager |
| 34 | bind9 local domain | **advice**: the zone doesn't load; another machine can't resolve it |
| 35 | kernel module at boot | **advice**: invents a `load-i2c-dev.service` |
| 25 | check a filesystem for errors | **lab**: the answer assumes an existing `/dev/sdb1` with a filesystem; the spare disks are blank |
| 31 | WireGuard server | **lab**: `modprobe` in a microVM (the module is built in); the repairs fixed `sudo` and the directory, but not this |
| 36 | GRUB kernel parameters | **lab**: a microVM has no bootloader and no `/etc/default/grub` |
| 37 | NVIDIA drivers | **lab**: no GPU |

**After "can't be tested here" and L11 (try another way), 2026-10-01:**

| # | Now | How |
|---|---|---|
| 28 | **goal-verified** | another way, attempt 1: `ip` plus netplan instead of `nmcli` |
| 34 | **goal-verified** | another way, attempt 2 (the first L11 run's two attempts failed) |
| 35 | ran clean | another way: a udev rule; it can't be proven here, so it waits for review |
| 36, 37 | can't be tested here | GRUB, NVIDIA: lab limits, not failures, and never "wrong advice" facts |
| 25 | failed | the lab now prepares the `/dev/sdb1` the answer assumes; what's left is genuine: `fsck.ntfs` doesn't exist on Ubuntu |
| 31 | failed | WireGuard: 3 rounds of 2 attempts, all failing on genuine mistakes (wg-quick config given to `wg setconf`, netplan YAML, iptables persistence) |

So the 40 now stand at 25 goal-verified, 6 ran clean, 5 read-only, 2 can't be tested here, and 2 failed.

**Corpus quarantine lifted (2026-10-01).** The 25 Sentinel runs marked `needs_rerun` in F-188 were re-run unchanged through the current lab and replaced under their own ids. Results: 13 goal-verified (promoted for reuse), 3 ran clean, 2 read-only, 7 failed; 4 of the 7 work after the lab's repairs. The refresh didn't run "another way", so second-IP and bind9 stay failed there; their goal-verified alternatives are stored separately. The 11 runs graded `lab_verified` before L12 were regraded the same way: all 11 are goal-verified (F-195). A goal-verified repaired procedure is now stored as an answer of its own; it's reused automatically only when its repairs are real corrections (F-195).

Before that, three failures were the model's advice. Four are things this lab can't be: real hardware, a bootloader, kernel modules, or a disk with existing data. Those four should be reported as *can't be tested here*, not as failures, so they don't become "wrong advice" facts (L11's note on causes, and the next step below).

## L29: L28 on a fresh sealed set (2026-10-07)

The 40 questions sealed before any L28 code (`sealed-questions-2026-10-06b.json`, 3ab4a98, sha256 `82c9587f…`, unchanged), through `/sandbox/linux-ask` on full VMs, with the lab's model calls on Stourport's 14B: `sealed5-asks.jsonl`, `sealed5-labs.jsonl`, `sealed5-run.log`.
- **Run time:** 7 h, against L27's 10 h. No step waited out its time limit on a foreground server.
- **Students' answers:** median 186 s (max 514).

Every verification and every failure was read.

| | L24 | L27 | L29 |
|---|---|---|---|
| Lab-verified | 13 | 8 | 11 |
| **… genuine on reading** | **4** | **6** | **7** |
| Weak | 1 | 1 | 2 |
| False | 8 | 1 | 2 |

**Genuine:**
- **#8:** tara can use sudo (after the lab's repair).
- **#11:** victor's shell became nologin.
- **#16:** another machine gets the answer's own page on 8081.
- **#22:** the swap file is active.
- **#34:** another machine mounts the NFS export.
- **#35:** IP forwarding is on, and still after a reboot.
- **#38:** the timezone is Europe/London.

**Weak:**
- **#25:** a read-only bind mount, checked by the student *reading* it, not by a write being refused.
- **#30:** "don't answer ping", checked by the sysctl value the answer set (via another way), not by pinging from the other machine.

**False, both new kinds:**
- **#20, gzip in nginx:** "how do I enable gzip" was taken as a pure install question, so "nginx answers" counted.
- **#23, a filesystem by UUID:** the lab filled the answer's `your-uuid-here` with the UUID of the *wrong disk* (`/dev/sdb1`, not `/dev/sdc`). "A filesystem is mounted at /data" then passed.

**Failures (15) and their cause:**

| | Runs |
|---|---|
| **The answer** (the lab was right) | #20 (nginx config that fails `nginx -t`), #30 and #31 (firewall answers that really lock SSH out), #33 (an sshd change that stops sshd) |
| **The lab or the platform** | #7: no `~/.bashrc` on any CloudCore user (F-227)<br>#5: the lab filled a process placeholder with a real PID, and `pkill -f tree` matched the step's own shell<br>#9: a process of the lab's own held the user<br>#12: a stand-in script that isn't runnable<br>#15: a crontab line run as a command; a stand-in file the student can't chmod<br>#18: "only xavier may log in" judged a lockout because *student* was refused<br>#24: du's permission noise taken as failure, example output run as commands<br>#26: the disk placeholder `/dev/sdX` not filled<br>#29: `eth0` where the lab's interface is named otherwise<br>#32: a placeholder certificate path mapped away from where the answer made the file |

**What it shows:**
- **Verification is steady:** 7 genuine of 11, against 6 of 8 and 4 of 13 before. Both false passes are new, narrow kinds.
- **Lab faults still outnumber answer faults:** about 10 to 4. They have moved on, though: no longer the working directory or reboots, now the long tail of the setup stage (stand-ins, placeholder kinds, environment names) and judging intent ("only xavier").

## L27: L26 on a fresh sealed set (2026-10-06)

The 40 questions sealed before the L26 code was committed (`sealed-questions-2026-10-06.json`, 1f5b252, sha256 `3fc00be5…`, unchanged). Asked through `/sandbox/linux-ask` on full VMs, with the lab's model calls on the lab's own 14B on Stourport (placement C2–C5): `sealed4-asks.jsonl`, `sealed4-labs.jsonl`, `sealed4-run.log`. Students' answers had a median of 192 s (max 431). Every verification and every failure was read.

| | L20 | L22 | L24 | L27 |
|---|---|---|---|---|
| Goal-verified, as graded by the lab | 1 | 8 | 13 | 8 |
| **… true on reading** | **1** | **6** | **4** | **6** |
| Weak | 0 | 0 | 1 | 1 |
| False | 0 | 2 | 8 | 1 |

**The 6 genuine:**
- **#3:** the link exists and the student can run the program through it (via another way).
- **#8:** svcapp's shell became nologin.
- **#16:** another machine gets the answer's own "Hello from the lab".
- **#18:** SSH answers on 2222 from another machine.
- **#36:** swappiness is 10, and still after a reboot.
- **#38:** the hostname is labhost.

**Weak:** #9. The answer is right, but `sudo -l` shows oscar is *allowed* the command, not that no password is asked.

**False:** #40, an explanation question. The lab "verified" the answer's own demo `chmod u+s`, and the answer is partly wrong ('S' in the others column isn't the sticky bit).

L26 removed every L24 false-pass pattern: none recurred.

**Failures and unfinished runs: whose fault (16 failed, 2 could not finish).**

| | Runs |
|---|---|
| **The answer** (the lab was right) | #14 (ACLs, not the sticky bit), #17 and #21 (nginx configs that fail `nginx -t`), #20 (`PasswordAuthentication yes` overridden by the image's `50-cloud-init.conf`, caught by the effective-config check), #25 (one partition took the whole disk) |
| **The lab** | #4 and #39 (retries), #5, #22: steps run where the student can't write; #26: the swap check expects swap on; #19: an example block taken as an edit; #32, #34: foreground servers hit the step limit; #12: a repair run as root broke the answer's own sudo retry; #27: a gateway outside the lab's network; #3: the setup stage made the directory, not the program |
| **The lab, unfinished** | #23 (lost the machine at the answer's own reboot), #28 (channel timeout after `ufw default deny`) |

**What it shows:**
- **Verification is now mostly trustworthy:** 6 of 8, against 4 of 13 in L24, and the one false pass is a new kind (explanation demos).
- **Lab faults are now the bigger error:** about 11 of the 16 failures and both unfinished runs, against about 5 answer faults. Those are L28.

## L24: L23 on a fresh sealed set (2026-10-05)

The 40 questions sealed before L23 (`sealed-questions-2026-10-05.json`, sha256 `76b418be…`, unchanged), through `/sandbox/linux-ask` on full VMs (`sealed3-asks.jsonl`, `sealed3-labs.jsonl`). Every "verified" was read: 8 first time, 5 after a repair or another way.

| | L20 | L22 | L24 |
|---|---|---|---|
| Goal-verified, as graded by the lab | 1 | 8 | 13 |
| **… true on reading** | **1** | **6** | **4** (#8 devs group members, #9 liam's one sudo command, #21 the www redirect, #22 tmpfs mounted and in fstab) |
| False or weak | 0 | 2 | 9 |

**The 9, by cause** (each fixed in L26):
- **No before-state for hand-written checks:** #27 (ports "allowed" with no firewall at all), #30 (port 22 "allowed", and nothing tested the limit), #32 ("SSH refuses passwords", already so, and the question was about the client).
- **Configuration taken as the goal:** #11 (the crontab has the answer's own line), #19 (sshd uses the answer's own Banner line).
- **Owner or mode of a new directory:** #24 (`/mnt/DATA` is root:root 755, nothing about the label), #25 (the same for a bind mount).
- **A default page:** #16 ("404 Not Found" is nginx's own text, not the custom page).
- **Weak:** #13 (the setgid bit, not that new files get the group).

L23's behaviour checks were right where the model chose them. The false passes came from the older hand-written checks and two loopholes in the rules.

## L22: L21 on a fresh sealed set (2026-10-05)

The 40 questions sealed before L21 (`sealed-questions-2026-10-04.json`, sha256 `846d6179…`, unchanged), asked through `/sandbox/linux-ask` on a coordinator rebuilt from cf1b850, with proofs on full VMs (`sealed2-asks.jsonl`, `sealed2-labs.jsonl`). 39 runs completed; #17 errored. Every "verified" was read.

| | L20 sealed (before L21) | L22 sealed (with L21) |
|---|---|---|
| Goal-verified, as graded by the lab | 1 | 8 |
| **… still true on reading** | **1** | **6** (#9 frank in adm, #14 gina's shell, #16 Apache answering on 8080 from another machine, #22 swap active and in fstab, #23 XFS mounted and in fstab via another way, #29 IPv4 forwarding on) |
| False passes found by reading | 0 | 2 |

**The two false passes:**
- **#19, SSH idle disconnect,** was "verified" by `sshd_config` containing the line the answer wrote. But `ClientAliveCountMax 0` *disables* the disconnect on OpenSSH ≥ 8.2, so the answer doesn't work.
- **#13, an environment variable for all users,** was "verified" by "SSH and PAM logins still work", which are regression guards, not evidence for the question.

Both are fixed for the next measurement (no re-run of this set):
- a `file_contains` check of text the answer itself wrote is never decisive;
- "… still works" login checks can fail a run but never verify one.

## L20: the sealed set (2026-10-04)

The 40 questions sealed before L16–L19 (`sealed-questions-2026-10-03.json`, sha256 `c2ad4982…`, unchanged), asked through `/sandbox/linux-ask` on a coordinator rebuilt from the L16–L19 code, with proofs on full VMs.
- **Results:** `sealed-asks.jsonl`, `sealed-labs.jsonl`.
- **Re-runs:** 13 lab runs never started because the lab network had run out of DHCP addresses (F-217). After the fix they were re-run on the **same recorded answers**, with no new model answers: `sealed-rerun.jsonl`, through the setup stage and repairs but without "try another way".
- **Grading:** every run was read.

| Grade, by reading the answer and its run | Count | Questions |
|---|---|---|
| Correct, and it ran in the lab (read-only commands, or changes nothing checked) | 21 | 2 3 4 5 6 7 9 10 12 14 20 23 27 28 31 35 36 37 38 39 40 |
| Wrong or weak as written, which the lab showed | 8 | 1 (a placeholder path, not `~`), 8 (`backup` is an Ubuntu system user), 11 (`network.target`, not `network-online.target`), 13 (cron for a one-off, not `at`), 15 (shadow format, not `chage -d 0`), 17 (default site left in place), 18 (locks SSH out), 30 (`ifdown eth0` on 22.04) |
| Ran, but correctness unclear or unchecked | 3 | 19 (interactive `mysql_secure_installation`), 25 (quota needs kernel support), 34 (Postgres from another machine, unchecked) |
| Can't be tested here, said so | 2 | 21 (Let's Encrypt), 26 (NVMe health) |
| Lab gap: a correct answer couldn't be tried properly | 4 | 16 (server block appended, not replacing the default), 24 (presumed LVM volume group), 29 (hosts line run as a command), 33 (a remote machine to mount) |
| Lab error, no verdict | 2 | 22, 32 |
| **Goal-verified** | **1** | 18, its "another way" attempt, after the original locked SSH out |

**Against F-200** (the held-out set before L16–L19): lab-caused failures fell from about 13 of 17 to 6 of 40. Most answers now run as the student would run them, and the lab catches real mistakes (#8, #15, #18, #30). But **goal verification barely generalises:** 1 of 40, against F-200's 3. On the sealed set:
- **Read-only questions:** most changes-nothing questions (7 everyday, 2 explanations, most kernel/boot) have nothing to verify.
- **No check for the topic:** for most that change state, no goal check exists. Checks are keyword-triggered per topic, as F-200 found, so carol in sudo (#9), Postgres reachable (#34), the ufw block (#28) and boot-to-text (#37) went unchecked.

**Next, from this:**
- **L21, goal checks the same way as the setup stage.** The model proposes what to check from the question, on a fixed menu run by the lab: user in group, file contains a line, service active and enabled, a port reachable or blocked from the prober, a default target, a command's output contains something.
- **Smaller gaps:**
  - a config block after an editor command should replace a whole default file (#16);
  - lines in a bash block that are file contents, like a hosts entry (#29) or a shadow line (#15), shouldn't run;
  - `systemctl set-default` counts as a change;
  - presumed LVM and remote machines need setup actions;
  - the harness should record a run's `error`.

## L16–L19 on the 40 held-out answers (2026-10-04)

The 40 held-out answers, as recorded (no new model answers), on full VMs with the setup stage, placeholders by kind, lab limits and goal-check precision (`reports/llm-chat-lab/l16-all-heldout.jsonl`). These answers were used while building L16–L19, so this is a development result, not a measurement; L20's sealed set is the measurement.

| Final verdict | F5, full VMs | L16–L19 |
|---|---|---|
| goal_verified | 4 | 5 |
| ran_clean | 5 | 10 |
| partial (nothing to verify) | 6 | 6 |
| not_testable (lab limit, said before or during the run) | 0 | 4 |
| not_runnable | 2 | 2 |
| failed | 23 | **13** |

**Changes from F5:**
- **Fixed by the setup stage:** #6 (alice and nginx set up, sudoers written 0440), #8 (bob, /srv/reports), #20 (a real UUID: now goal-verified), #33 (a real PID owned by the student), #36, #38 (placeholder user mapped to the lab's).
- **Fixed by goal-check precision:** #28, where "swappiness" no longer checks swap.
- **Now recognised as lab limits:** #14 (Let's Encrypt, recognised before booting), #21 (SMART), #30 (graphics), #35 (fans).
- **One regression:** #25, rsync to another machine. It exposed F-216, since fixed.

The remaining failures are mostly genuine answer mistakes (#9, #13, #27, #40), or setup that needs another machine prepared (an SSH server or rsync target on the prober: #25, #39) or a reachable gateway (#23). Median run time: 94 s (F5: 83 s), as the model's reading mostly overlaps the VM boot.

## Held-out evaluation (2026-10-01/02)

40 new questions, none used for tuning (`reports/llm-chat-lab/heldout-questions.json`), asked through the page's own endpoint: grounding and reuse, the answer, the lab, repairs, another way. Results: `heldout-asks.jsonl`, `heldout-results.jsonl`. Grading was by reading every run.

| Outcome (best of as-written / repaired / another way) | Held-out | Tuning set (F-193/F-194) |
|---|---|---|
| Goal-verified by the lab | 5 | 25 |
| … still true on reading the run | **3** (#15 root SSH login, #19 RAM disk, #24 iptables 8080) | — |
| Ran clean | 10 | 6 |
| Read-only / nothing to run | 7 | 5 |
| Can't be tested here | 1 | 2 |
| Failed | 17 | 2 |

**The two weak verifications:**
- **#7 is a false pass:** the cron entry deletes only `*.txt`, while the check only confirmed an entry exists.
- **#11 is unproven:** the Docker check runs `docker` as root, so it never tested "without sudo".

**The 17 failures are mostly the lab, not the model.** By class, most-cases first:

| Class | Questions | What happened |
|---|---|---|
| A. The question presumes existing state | #6, #8, #11, #13, #20, #27, #38, #39 | users (bob, alice in sudo), installed services (nginx, MariaDB, Docker), a partition, an SSH server to connect to: none exist in a fresh machine |
| B. Goal check fires on the wrong thing | #12, #28, #39, #40 (and #7, #11) | "swappiness" matched the swap check; a client SSH question checked for a server; the Docker-container timezone checked the host; the ufw rule allowing only 10/8 was checked from a prober outside 10/8 |
| C. Placeholder styles not recognised | #9, #20, #26, #31, #33, #39 | `$service_name`, `package_name`, `ZOMBIE_PID`, `server_ip_address`, `yourlinuxip`, `your-uuid-here` ran literally |
| D. Lab limits not recognised | #14, #18, #21, #30, #35 | public domain for Let's Encrypt, a grown disk, SMART, PCI graphics, fan sensors |
| E. Harness bugs | #6, #10, #16, #17, #26, #2–#5 | sudoers file written 0644 (`visudo` gives 0440); a pre-created placeholder path clashing with the answer's own `mkdir`; apt locked by the image's boot-time index refresh; `tail -f` never stops; the `username` substitution rewrote the mount option `username=`; kept labs used all memory, so 4 runs never started; F-199 (worker crash) |

**Genuine answer mistakes were seen too:** `ufw … port 22/tcp`, a `default.bak` left in nginx's `sites-enabled`, `systemd-resolve` (gone in 22.04), `mysql -p` with SQL on stdin, a Python syntax error, `docker run` without the group.

**Reuse:** the matcher handed the model a stored answer 3 times.
- **Right question once:** #37 (hostname, a paraphrase).
- **Different question twice:** #12 got the "SSH, HTTP and HTTPS" ufw answer; #15 got "change the SSH port to 2222".
- **Missed:** one paraphrase, #36 "London time".
- **Near-misses:** all three were (correctly) left alone.

**Goal checks miss paraphrases:** they're keyword-triggered, so "rename my server" and "London time" got no hostname or timezone check.

**Time:** answers took a median of 233 s (max 354 s); lab runs with two other-way attempts took up to 43 min.

**What it means:** the 25/40 on the tuning set was overfitted. On unseen questions, the lab proved 3 of 40. The model is not the main limit: most failures are the lab not setting up what a question presumes, and checks or parsing that don't generalise beyond the answers they were written against. **These 40 are no longer held out once fixes are made from them**, so the next measurement needs a fresh set.

## Definitions

**Step classifications:** `ok`, `package_not_found`, `command_not_found`,
`file_missing` (the answer uses a path that isn't there and never creates
it), `service_failed`, `config_invalid` (a validator rejected it),
`step_failed` (other non-zero exit), `interactive` (needed input),
`timeout`, `not_runnable` (no step could be extracted).

**Lab-verified** (and eligible for automatic reuse): every extracted step
is `ok`, at least one step changed the system, nothing was skipped as
`interactive`, and every service the answer starts is active at the end.

**The limit, stated plainly:** "it ran" is not "it's good advice". F-174's
`pam_unix.so … md5` line would run cleanly. So a reused answer keeps any
F-175 risky-change notice it carries, and a later human rejection in Sentinel
always overrides `lab_verified`.

## Risks and constraints

- **Time.** The model generates at ~1 token/s, so the run starts only after
  the answer is complete and runs alongside nothing heavier than the next
  question. Most runs are apt plus config, seconds to a few minutes.
- **Egress.** Advice-run VMs reach only the internet, the same policy as
  today's per-run and Terminal VMs. No lab or host network.
- **Security of the runner.** It executes model output as root, but only
  inside a disposable, network-fenced microVM (the same boundary the coding
  panel's verification already relies on). It never touches the student's
  own Terminal.
- **Kernel build time.** It's a one-off per kernel version, done in a builder
  instance, never on a live path.

Methodology unchanged: build and verify live, log findings as F-NNN, tear
down after.
