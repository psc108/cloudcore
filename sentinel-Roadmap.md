# Sentinel — Roadmap (all open work)

**Status:** 2026-10-10: implementation starting after LFS run 1, decisions made (SKB). Before that 2026-10-09, after the review; items in question are in `roadmap-Verify-Items.md`. Consolidated from the plan documents (see `cloudcore-Roadmap.md` for the method and the source abbreviations). **Owner:** Paul Scott.

## How this works

The same rules as CloudCore's roadmap:
- **One list:** every open Sentinel item, each linked to its source.
- **New ideas** go here as entries.
- **A plan document** only when an item is about to be built.

The detailed plan for the knowledge base work is `sentinel-kb-browsing-Phased-Implementation.md` (SKB).

## 1. The knowledge base

| # | Item | Status | Source |
|---|---|---|---|
| SN-01 | **Paging and collections** (server-side paging, search and sort; collection filter with counts). | open | SKB K1 |
| SN-02 | **Subjects:** a subjects table; rules on file paths; `**Subjects:**` lines in findings; model suggestions approved by a person. | open | SKB K2 |
| SN-03 | **LFS subjects:** by chapter and by fault class (controller, 14B, platform, book). Needed by LFS run 2's comparison. | open | SKB K3; LFS F1 |
| SN-04 | **Export findings to Markdown and PDF,** singly or a filtered view. | open | SKB K4 |
| SN-05 | **Open decisions** for SN-01 to SN-04: the subject list, the page size, model suggestions, "book" as a fault class, how PDFs are made, the house style, the multi-finding export. | **decided** (Paul, 2026-10-10; see SKB's Decisions) | SKB open decisions |

## 2. Sentinel's own data

| # | Item | Status | Source |
|---|---|---|---|
| SN-10 | **Retention: keep the live database small without losing what Sentinel learns from.** Checked 2026-10-09:<br>• **Who reads these tables:** the 14B doesn't. Its grounding is approved answers, the code index, Kiwix and lab facts, and its LFS nudges come from findings. So archiving can't change what it knows. Sentinel's own `sentinel train` does read them: calibration uses suggestions a person has judged, and the anomaly scorer learns from every event's text.<br>**Three conditions:**<br>1. **Never archive human-judged suggestions** (acknowledged or dismissed: 60 today). They're calibration's training labels.<br>2. **Events stay available for learning:** train the anomaly scorer before any archiving (it has never been trained; 17,898 events, 16,950 from September). Then either have training read the archive files too, or keep a permanent de-duplicated corpus of event texts.<br>3. **Collapse duplicate unreviewed suggestions** (52,351 of them, mostly the same few findings matched repeatedly) into one row per finding and pattern, with a count and first and last seen, rather than just archiving them.<br>Only then are old events and collapsed suggestions archived (exported to a dated file in the backup) and removed after an agreed age. Nothing is removed before it's in a backup. | open | SKB K5.5 |
| SN-11 | **A data-access layer** in Sentinel, built as K1 and K2 reshape the queries. | open | SKB K5.6 |
| SN-12 | **Evidence-based approval** (Paul, 2026-10-09: a person can't reliably approve findings by reading them). A finding or answer is approved for reuse by **proof that it worked**, not by review.<br>• **Proof means evidence the model didn't write:**<br>&nbsp;&nbsp;– **LFS:** the plan or repair visibly used the finding (applied its fix, or cited it), the step that failed before passed, and the section completed;<br>&nbsp;&nbsp;– **Linux Help:** lab verified (every step ran and every goal check passed), as CC-63.<br>• **No credit for coincidence:** no success counts if a human or controller fix (a ladder reset) came in between. In run 1 several retries passed after irrelevant nudges, because the controller had just been fixed.<br>• **A trust score, not a switch:** uses and proven successes are recorded per finding. It is promoted to approved after 3 independent proven successes, and demoted automatically if a later use fails.<br>• **Only as good as the checks:** weak checks (the lab backlog's #25 and #30) are fixed first, or don't count as proof.<br>• **Never auto-approved:** credentials, security settings, deletion. In run 1 a password-setting repair "worked" (LFS-035).<br>• **People audit, not approve:** a weekly random sample of auto-approved items (1 in 10), plus anything demoted or disputed.<br>• **Provenance:** each approval keeps its evidence (task, step, before and after), so a bad one can be traced and reversed.<br>• **Evidence to test it on:** LFS run 2 (CC-90) and the lab loop (CC-60). | open | Paul 2026-10-09; relates to SN-03, SN-20, SN-31, CC-63 |
| SN-13 | **Scheduled training, with safeguards** (Paul, 2026-10-09). `sentinel train` is manual today, deliberately: its docstring says changing detection on a live watch loop needs a person's decision. So a scheduled run (e.g. weekly) **proposes** rather than switches:<br>• **Shadow:** it trains a *candidate* (anomaly scorer, calibration) and scores it beside the live one for a while, without acting on it.<br>• **Promote on evidence:** the candidate takes over only if it doesn't do worse on a held-out set of past events and judged suggestions. Otherwise it is kept for a look and not used.<br>• **Rollback:** the previous model is always kept, and one step restores it.<br>• **A record:** each run's samples, changes and outcome, shown in Sentinel's UI.<br>• **Ordering:** the first training comes **before** any archiving (SN-10). The anomaly scorer has never been trained, and its 17,898 events are its training material. Calibration later uses SN-12's proven successes, not only manual acknowledgements.<br>• **Where it runs:** a Sentinel-side timer on its host, or a CloudCore scheduler kind that calls Sentinel's API. Decided when built. | open | Paul 2026-10-09; `sentinel/train.py`; relates to SN-10, SN-12 |

## 3. The LFS escalation ladder

| # | Item | Status | Source |
|---|---|---|---|
| SN-20 | **Rung 2's relevance:** the knowledge-base nudge often matched noise early on. Measure which nudges helped (LFS run 2's comparison) and tune: a higher threshold, LFS findings first, subjects (SN-03). | open | LFS-Findings-Log (LFS-019, -023 to -034) |
| SN-21 | **Rung 4's phone channel** (`SENTINEL_NOTIFY_URL`). Paul: not now. | deferred | LFS C5 |
| SN-22 | **The tutor's daily cap:** spent by midday on 2026-10-08, mostly on controller faults that are now fixed. Revisit after run 1 with the counts. | open | LFS-Findings-Log LFS-029 |
| SN-23 | **Lessons before planning:** whether the 14B gets a section's knowledge-base lessons before it plans (not only after a stop). | decision | LFS F2 |

## 4. llm-chat's grounding and review (data Sentinel holds)

| # | Item | Status | Source |
|---|---|---|---|
| SN-30 | **Watch the API's AUDIT lines in Loki,** once CloudCore ships them (CC-11). | open (after CC-11) | AUTH follow-up 2 |
| SN-31 | **The answer matcher reused stored answers for different questions** (held-out #12, #15) and missed a paraphrase (#36). No fix recorded. | verify (V-19) | LAB held-out evaluation |
| SN-32 | **An answer waiting for a person's review:** #35, the kernel-module udev answer (it ran clean another way). | open | LAB results after L11 |
| SN-33 | **A Sentinel-side capture token panel:** the plan said "Sentinel/Dashboard panel", but only the dashboard card was built. Confirm whether one is wanted. | decision (V-21) | EXT Stage 13 design |
