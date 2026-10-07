# llm-chat — next steps once the lab is at its best

**Status:** recorded 2026-10-06, while the L29 sealed measurement runs. **Owner:** Paul Scott.

## When the lab is "as good as we can expect"

The lab-quality loop (seal a set, build, measure, read every result) stops when one sealed run shows all three:

- **No false verifications** on reading.
- **Lab faults fewer than answer faults** among the failures.
- **The remaining failures are honest limits** (no GPU, no public DNS, no physical hardware), each reported as "can't be tested here".

Where it stands:

| Run | Genuine / claimed | False | Lab faults vs answer faults |
|---|---|---|---|
| L24 | 4 / 13 | 8 | — |
| L27 | 6 / 8 | 1 | about 11 vs 5 |
| L29 | 7 / 11 | 2 (fixed after: F-228) | about 10 vs 4 |

**Decision (Paul, 2026-10-07):** pause the lab-quality loop after L29.
- **Fixed:** L29's two false passes (F-228).
- **Backlog:** the remaining lab faults, below, to fix when they come up.
- **Next:** the loose ends (step 1 below), then the LFS build (`lfs-os-Phased-Implementation.md`), which works from the book rather than the setup stage these faults are in.
- **Coming back:** to this loop after the LFS exercise.

Once the stop rule is met, the lab goes into maintenance. The main limit left is the model itself (Qwen2.5-Coder-14B Q4). Better hardware lifts it, and placement (`llm-chat-placement-Phased-Implementation.md`) uses new hardware without template changes.

## Next steps, in the suggested order

| # | Step | What it involves | Why this order |
|---|---|---|---|
| 1 | **Loose ends** | Three items:<br>• **Root-owned homes:** rebuild instances made before 2c960eb, which still have root-owned user homes (F-221).<br>• **Old SG:** decide whether to delete Stourport's old SG `llm-chat-coordinator-llwynygroes` (SSH and 8620 open to 0.0.0.0/0).<br>• **Host firewall:** roll it out (`setup-host-firewall.sh`; ufw inactive on both hosts, deferred 2026-09-28). | Small, and two are security |
| 2 | **Use what the lab proves** | A question that matches a lab-verified answer gets that answer at once, marked as tested, without the model. Unverified answers keep the current path. | The lab's work becomes speed and trust for students |
| 3 | **Finish placement** | Two pieces:<br>• **C6:** prove that a host with a better measured score takes the work automatically.<br>• **Routing students' answers:** per-request routing across answer-capable hosts, once there is a second one. | Matters most when new hardware arrives |
| 4 | **Auth follow-ups** | Audit and token views on the dashboard, audit events to Loki, a rotation routine for the shared guest tokens (`cloudcore-auth-Phased-Implementation.md`) | Noted, not urgent |
| 5 | **Back to haFullStack** | The build → test → document → destroy cycle on the wider platform (database tier, failover, backups) | Where llm-chat was one workload among several |

## Open with the lab itself (from L27 and L28 development)

These are known, small and not blocking:
- **A server the answer starts in the foreground** now runs, but isn't counted as a change, so nothing checks that it serves (L28 dev #3).
- **The setup stage can make a directory but not the program the question names** (L27 #3), and doesn't start a stub app on a port a proxy question names (L27 #21).
- **Known limit:** the lab can't judge what a setting means (L22 #19: `ClientAliveCountMax 0`).

## Lab backlog (from L29, 2026-10-07)

None of these is a false verification. They are failures the lab caused, or verifications too weak to trust:

| Run | Fault | Likely fix |
|---|---|---|
| #5 | A process placeholder filled with a real PID; `pkill -f tree` matched the step's own shell | The setup stage starts a stand-in process for a question about one (here: listening on port 9000), and fills its PID and name |
| #9 | A process of the lab's own still ran as the user being modified (`usermod: user uma is currently used`) | Find which lab step leaves it (probably a session from a user check) and end it before the answer runs |
| #12 | A stand-in for a script the service runs was a text file, so the service failed | A presumed program or script is a runnable stub (`#!/bin/sh`, then sleep) |
| #15 | A crontab line in a `bash` block run as a command; a stand-in file in `/srv/lab` the student couldn't chmod | Recognise five-field cron lines as crontab entries; make setup's files in `/srv/lab` the student's |
| #18 | "Only xavier may log in over SSH" judged a lockout because *student* was refused | When the question restricts logins to named users, log in as that user, and count the student's refusal as goal evidence |
| #24 | `du`'s permission noise taken as failure; its example output run as commands | Treat `du` like `find` (partly readable is fine); recognise size-and-path lines as output |
| #26 | The disk placeholder `/dev/sdX` not filled | A `disk` placeholder kind mapped to the spare disk |
| #29 | `eth0` where the lab's interface has another name | Map `eth0`/`ens3` and the like to the lab's main interface, as a person would |
| #32 | A placeholder certificate path mapped to `/srv/lab` rather than where the answer made the file | Map a placeholder path to a file of the same name the answer creates |
| #25, #30 (weak) | Read-only mount checked by reading; "no ping" by the sysctl the answer sets | Behaviour checks: a write is refused; another machine's ping gets no reply. A sysctl the answer sets directly is not evidence unless the question names it |
| L28 dev #3 | A foreground server the lab starts isn't counted as a change, so nothing checks that it serves | Count it, and probe its port |

