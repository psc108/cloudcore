# Roadmap: items in question

**Status:** 2026-10-09. These are the items from the plan review (`cloudcore-Roadmap.md`, `sentinel-Roadmap.md`) whose state the documents left unclear or contradictory. Each has a question, how it was or will be checked, what was found, and the outcome. **Owner:** Paul Scott.

## Checked and settled (2026-10-09)

Checked by reading the code, config or documents directly. Each outcome has been applied to the roadmaps and the source plans.

| # | Question | What was checked | Found | Outcome |
|---|---|---|---|---|
| V-01 | Is the capture listener (port 8083) open to every network? EXT 13.4 said yes, but that was written while ufw was off. | `/etc/cloudcore/host-firewall.inventory` on Llwyn-y-Groes; ufw has been on both hosts since 2026-10-07. | Port 8083 is allowed only from the CloudCore bridge (guests) and the LAN. | **Closed:** CC-21 removed. The firewall rollout settled it. |
| V-02 | Was haFullStack.md §6.3's quorum-queue policy corrected (HA 4A-04, F-029)? | haFullStack.md §6.3, lines 562–590. | Corrected: the `set_policy queue-type` form is marked "version-dependent, RabbitMQ 3.11+", with the declaration-time `x-queue-type` form for older versions (the Lab runs 3.9.27). | **Closed:** removed from CC-84. |
| V-03 | Is F8 (measure on a fresh held-out set) done? FVM says "not started"; LAB says L20 "also F8", done. | LAB L20 (done 2026-10-04) and FVM's F8 row. | L20 *is* F8, done. FVM's row is stale. | **Closed:** FVM's F8 row marked done (see L20). |
| V-04 | Did EXT 11.4 update the sandbox plan's input-wait entry? | SBX's out-of-scope list. | Updated: it now cites F-158 (Stage 11), with exact detection for blocking reads. | **Closed.** |
| V-05 | Are the Ansible copies of the llm-chat files the same as the Terraform ones (EXT 10.6, 12.8)? | `cmp` of each file in `examples/llm-chat/files/` against `ansible/examples/templates/llm-chat/`. | All 7 identical: `advice_runner`, `fullvm`, `lfs_worker`, `microvm`, `model_router`, `sandbox_terminal`, `verify_proxy`. | **Closed.** |
| V-06 | Should the `webui_temperature` and `webui_system_message` variables be removed? | `examples/llm-chat/variables.tf`. | Still there, by a recorded decision ("not worth removing"). | **Closed:** kept as decided. |
| V-07 | Is the placement plan still "planned"? | Its header against its rows. | C1–C5 done; only C6 is open (CC-50). | **Closed:** header corrected to "C1–C5 done; C6 open". |

## Still to check: when the item is next worked on

Each needs a run or a test, not just reading. None blocks anything, and each is checked when its area is next worked on.

| # | Question | How to check | Outcome if true / if false | Roadmap |
|---|---|---|---|---|
| V-10 | **Older lab gaps:** are they still real after L21–L29? These are L14's goal checks by kind; L20's smaller gaps (a config block replacing a default file; file-content lines in bash blocks; `systemctl set-default`; setup for LVM or remote machines; the harness recording a run's error); an SSH/rsync target on the prober (#25, #39); L28 #28's channel timeout. | When the lab loop resumes (CC-60): re-run the original questions and see which still fail. | Still failing: add to the lab backlog (CC-61). Passing: close. | CC-62 |
| V-11 | **A coordinator with no workers:** `rpc_servers` comes out empty, so `--rpc` has nothing (`examples/llm-chat/locals.tf:121`). Is a coordinator-only config wanted? | Decide; if yes, render no `--rpc` when there are no workers, and test. | Wanted: an entry. Not wanted: record it as a design limit. | CC-98 |
| V-12 | **`coordinator_flavor`,** once called "a stopgap": still needed now that placement exists? | Read placement C2–C4's sizing against the variable's use. | Superseded: remove the variable. Still used: keep it, documented. | CC-98 |
| V-13 | **The full VM's disk rescan** (FVM F3, "the runner must rescan a grown data disk"): done? | A full-VM run with a grown data disk. | Works: close. Doesn't: an llm-chat entry. | CC-98 |
| V-14 | **The peer on current code** for full-VM proof runs (FVM F2). Later runs used Stourport; does Llwyn-y-Groes work too? | One proof run placed on Llwyn-y-Groes. | Works: close. Doesn't: a placement entry. | CC-98 |
| V-15 | **A Run during active generation** (EXT Stage 12's failure mode): never clearly tested mid-generation. | Start a Run while llama-server is generating. | Fine: close. Not fine: a sandbox entry. | CC-98 |
| V-16 | **The student review page:** does it have the local-client filter and token label (EXT 13.6)? | Open the review page in a browser. | Present: close. Missing: a dashboard entry. | CC-98 |
| V-17 | **CodeMirror in a real browser** (SBX Stage 2, never seen in a browser then). Likely covered by later headless-Chrome screenshots. | Open an editor page in a browser. | Renders: close. | CC-98 |
| V-18 | **haFullStack:** 6A-12's teardown after the v0.19 retrofit, and Phase 7's missing .B–.F rows. | Read HA 6.A against the v0.19 retrofit notes; ask whether Phase 7 needs other paths. | Settled in HA's own plan. | CC-84 |
| V-19 | **Sentinel's answer matcher** reused stored answers for different questions (held-out #12, #15) and missed a paraphrase (#36). Was it fixed later? | Re-run those three questions against the current matcher. | Fixed: close. Not fixed: a Sentinel entry. | SN-31 |

## Decisions for Paul, not checks

| # | Question | Roadmap |
|---|---|---|
| V-20 | **Peer and student tokens** stay outside `api_tokens`, with `api.env`'s admin token as break-glass. Confirm that stays the design. | CC-15 |
| V-21 | **A Sentinel-side capture token panel:** the plan said "Sentinel/Dashboard panel", but only the dashboard card was built. Wanted? | SN-33 |
