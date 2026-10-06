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
| L29 | running (L28 code, set sealed 3ab4a98) | | |

After that, the lab goes into maintenance. The main limit left is the model itself (Qwen2.5-Coder-14B Q4). Better hardware lifts it, and placement (`llm-chat-placement-Phased-Implementation.md`) uses new hardware without template changes.

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
