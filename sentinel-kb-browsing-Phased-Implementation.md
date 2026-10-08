# Sentinel: browsing the knowledge base by page, collection and subject — Phased Implementation

**Status:** draft for review, 2026-10-08. Work starts after LFS run 1 is complete (Paul: focus on run 1 first). **Owner:** Paul Scott.

## Context

Request (2026-10-08): Sentinel's knowledge base holds 272 findings, but the UI never shows them usefully. Paul wants:
- **Paging:** page through them (page 1 … 20 and so on).
- **Filtering by category:** list all LFS findings, or all findings on one subject, then page through those.

What exists today (measured 2026-10-08 on Llwyn-y-Groes):

| | Today |
|---|---|
| API | `GET /api/findings` returns **all** findings in one list, sorted by code (`sentinel/web/server.py`). |
| UI | The Knowledge Base tab draws them all as one table; the search box filters that list in the browser (`ui/index.html`, `renderKB`). |
| Collections | Each finding keeps the log it came from (`findings.source_doc`):<br>• `haFullStack-Findings-Log.md` (F-codes): **236**<br>• `LFS-Findings-Log.md` (LFS-codes): **29**<br>• `lfs-build-journal` (tutor lessons, `LFS-T…`): **7**<br>• Drafts by the 7B ingestion job carry an `llm-ingest:` prefix. |
| Subjects | **Not stored.** `findings.tags` exists but only the 7 tutor lessons use it; **265 are untagged**. The subject is implied only by each finding's *Where* (file paths) and title. |

A first rough rule set, run against the live KB, gives an idea of what rules can do and where they go wrong:
- **Placed:** 261 of 272 findings got at least one subject, and 11 got none (all F-codes).
- **Too loose:** 169 got several subjects, because broad words ("instance", "repo") match nearly everything.
- **The lesson:** rules on *Where*'s file paths are precise; rules on title words are not. Each finding needs **one primary subject**, with others secondary.

## Principles

- **The server pages and filters,** not the browser: the KB will grow (every build adds findings), and search must combine with the filters.
- **Categories are data, not guesses in the UI:** stored in the database, so the API, the matcher and the UI agree.
- **Deterministic first:** subjects come from rules on file paths, and an explicit line in new findings. A model only suggests, for the remainder, and a person approves.
- **Nothing lost:** the findings logs stay the source of truth. Re-ingesting rebuilds the derived subjects, except those a person set.
- **The existing API keeps working:** `GET /api/findings` with no parameters returns the full list, as now, for anything that uses it.

## Phase K1 — Paging and collections (no new data)

| # | Stage | Who |
|---|---|---|
| K1.1 | **The API.** `GET /api/findings` gains optional parameters:<br>• `page`, `per_page` (25, 50 or 100; default 50);<br>• `collection` (`platform`, `lfs`, `tutor`, `llm-drafted`);<br>• `q` (search code, title, symptom, cause and fix);<br>• `sort` (`code`, which sorts F-2 before F-10, or `newest`).<br>With any of these present it returns `{items, total, page, pages, facets}`, where facets are the counts per collection for the current search. With none, it returns the plain list as today. | Claude |
| K1.2 | **Collections** are derived from `source_doc`, with no schema change: F-codes are `platform`, LFS-codes `lfs`, `LFS-T` `tutor`, and the `llm-ingest:` prefix `llm-drafted`. A collection holds no secrets, so it's all in the API. | Claude |
| K1.3 | **The UI.**<br>• **A collection selector with counts:** "All (272) · Platform (236) · LFS build (29) · Tutor lessons (7)".<br>• **A pager:** "‹ 1 2 3 … 6 ›", plus a page-size choice.<br>• **Search sent to the server,** with a short pause while typing so it isn't sent on every key.<br>• **Expanded rows** work as now.<br>• **The view in the page address** (`#kb?collection=lfs&page=2&q=gcc`), so the back button and bookmarks work. | Claude |
| K1.4 | **Tests:**<br>• paging edges (an empty page, the last page, `per_page` out of range);<br>• facet counts;<br>• the natural sort;<br>• the no-parameter list unchanged;<br>• SQL built only from fixed fragments and bound values. | Claude |

## Phase K2 — Subjects

| # | Stage | Who |
|---|---|---|
| K2.1 | **The schema.** A `finding_subjects` table (`finding_id`, `subject`, `primary` 0/1, `origin`: `rule`, `declared`, `suggested` or `person`). One finding can have several subjects and has at most one primary. An additive migration in `db._migrate`, like the others. The unused `tags` column stays, for compatibility. | Claude |
| K2.2 | **The subject list,** kept short and stable. A proposal, to agree in review: llm-chat · lab VMs · compute (instances, images, snapshots) · networking (bridges, WireGuard, firewall, security groups) · HA stack · Terraform provider · auth · peers / two-host · package repo · logging · scheduler · dashboard UI · Sentinel · LFS build. | **Paul** agrees the list |
| K2.3 | **Rules at ingest, tuned against the live KB.** Rules match *Where*'s file paths first (`examples/llm-chat/` → llm-chat, `api/compute.py` → compute, `provider/` → Terraform provider, `lfs/` → LFS build …). The **first path** in *Where* sets the primary subject; further paths add secondary ones. Title words are used only when *Where* names no file.<br>**Target:** at least 90 % of findings get a primary subject, with secondaries the exception, not the rule. Each rule-set change is run as a dry run that prints what changes. | Claude |
| K2.4 | **Declared subjects in new findings.** The KB parser reads an optional line `**Subjects:** llm-chat, networking` (the first listed is primary). New F- and LFS- findings carry it from then on. A declared subject beats a rule. | Claude writes; the parser reads |
| K2.5 | **Suggestions for the rest.** For a finding with no primary subject, the 14B suggests one from the agreed list, using the same pattern as the 7B ingestion job. The suggestion shows in the UI for a person to accept or change, and only then counts as `person`. | llm-chat suggests; **Paul or Claude** approves |
| K2.6 | **The API and UI:** `subject=` filters the list; facets add counts per subject; a subject selector sits beside the collection one; a finding's subjects show as chips in its row. A person can change a subject from the row (`POST /api/findings/<id>/subjects`), which records `origin: person`, so re-ingesting never overwrites it. | Claude |
| K2.7 | **Tests:** the rules against fixtures from real *Where* lines, declared beating rules, `person` surviving a re-ingest, and the filters combined with paging and search. | Claude |

## Phase K3 — LFS-specific subjects

These are useful for the LFS build itself, not only for browsing:

| # | Stage | Who |
|---|---|---|
| K3.1 | **By chapter and section:** an LFS finding names its task (e.g. "task 11 (5.3 GCC Pass 1)"), so its chapter and package are read from that: toolchain (ch. 5), temporary tools (ch. 6), chroot (ch. 7), final system (ch. 8) … | Claude |
| K3.2 | **By fault class:** controller fault · 14B mistake · platform · book. This is the classification LFS Phase F1 needs to compare run 2 with run 1 fairly. From K2.4 on, each LFS finding declares it; the 29 existing ones are classified once, by Claude, for Paul to check. | Claude classifies; **Paul** checks |
| K3.3 | **In the UI,** under the LFS collection: filter by chapter and by fault class, e.g. "all 14B mistakes in chapter 5". | Claude |

## Order and dependencies

- **K1 first.** It needs no new data and fixes the "never see all 272" problem on its own.
- **K2 after K1:** K2.2's list is agreed before K2.3's rules are tuned.
- **K3 after K2,** and before LFS Phase F (run 2), which uses K3.2's fault classes.
- **All of it waits for LFS run 1 to complete** (Paul, 2026-10-08).

## Risks

- **Over-matching rules:** the first trial put 169 findings under several subjects. Mitigation: one primary subject from file paths, a dry run per rule change, and the 90 % target measured, not assumed.
- **A list that drifts:** too many subjects make the filter useless. Mitigation: a short agreed list (K2.2); adding a subject is a decision, not a side effect.
- **Model suggestions taken as fact:** K2.5's suggestions never count until a person approves them.
- **Breaking callers:** the matcher, `/api/status` and CloudCore's relay read findings. Mitigation: the no-parameter `GET /api/findings` is unchanged, and the matcher doesn't use subjects.

## Open decisions

- **K2.2:** the subject list.
- **Page size:** the default of 50, and whether 25/50/100 is the right choice.
- **K2.5:** whether to have model suggestions at all, or leave the few unplaced findings to be done by hand.
- **K3.2:** the fault classes. Is "book" (the book's own text misleads) a class of its own?
