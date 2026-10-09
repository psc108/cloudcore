# Sentinel — Roadmap (all open work)

**Status:** 2026-10-09, after the review; items in question are in `roadmap-Verify-Items.md`. Consolidated from the plan documents (see `cloudcore-Roadmap.md` for the method and the source abbreviations). **Owner:** Paul Scott.

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
| SN-05 | **Open decisions** for SN-01 to SN-04: the subject list, the page size, model suggestions, "book" as a fault class, how PDFs are made, the house style, the multi-finding export. | decision (when built) | SKB open decisions |

## 2. Sentinel's own data

| # | Item | Status | Source |
|---|---|---|---|
| SN-10 | **Retention:** archive old events and suggestions into the backup, then remove them (52,272 suggestions against 272 findings). | open | SKB K5.5 |
| SN-11 | **A data-access layer** in Sentinel, built as K1 and K2 reshape the queries. | open | SKB K5.6 |

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
