# Sentinel: browsing the knowledge base by page, collection and subject — Phased Implementation

> **Roadmap (2026-10-09):** the detailed plan for `sentinel-Roadmap.md` SN-01 to SN-11; K5.1–K5.4 and K5.6's CloudCore part are tracked in `cloudcore-Roadmap.md` (CC-01 to CC-05).

**Status:** draft for review, 2026-10-08; K5 (data safety) added the same day. Work starts after LFS run 1 is complete (Paul: focus on run 1 first). **Owner:** Paul Scott.

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

## Phase K4 — Exporting findings to Markdown and PDF

Request (Paul, 2026-10-08): each knowledge-base article can be written out as Markdown or PDF.

| # | Stage | Who |
|---|---|---|
| K4.1 | **One finding as Markdown.** `GET /api/findings/<id>/export.md` returns the finding in the findings-log format it came from: `### CODE — title`, then **Where**, **Symptom**, **Root cause**, **Fix** and **Verified by**, plus its collection, its subjects and when it was ingested. The file name is the code and a short title (`LFS-019-gcc-pass-1-out-of-memory.md`). An exported finding can be re-ingested unchanged. | Claude |
| K4.2 | **One finding as PDF,** from the same content, with a title block (code, title, collection, subjects, date) and page numbers. How the PDF is made is open decision K4-a. | Claude |
| K4.3 | **In the UI:** "Markdown" and "PDF" buttons on each expanded finding. | Claude |
| K4.4 | **Several at once (optional):** the current filtered view (e.g. "LFS build, 14B mistakes") as one Markdown or PDF document, with a contents list. That's handy for a write-up like LFS Phase D7. One finding per page in the PDF. | Claude |
| K4.5 | **Tests:** the Markdown round-trips through the KB parser to the same finding; every field present; safe file names; a PDF produced and readable (page count, text extractable); unknown ids give 404. | Claude |

## Phase K5 — Keeping the data safe

Paul (2026-10-08): no move to MySQL or PostgreSQL any time soon, but **no data or logs we already have may be lost.** SQLite is ample at this scale (Sentinel 469 MB; CloudCore about 45 MB a host). What matters is copies, retention and a clean data layer.

**What exists already** (two-host S6, `cloudcore-two-host-Phased-Implementation.md`):
- **The schedules:** the dashboard scheduler's `host_backup` kind runs `api/backup-host.py` nightly. Stourport → Llwyn-y-Groes at 02:00 UTC; Llwyn-y-Groes → Stourport at 02:30 UTC. Both succeeded on 2026-10-08.
- **What each run copies:** consistent, integrity-checked snapshots (SQLite's online backup API) of `cloudcore.db`, which includes the LFS build's tasks and journal; each example's OpenTofu state; and Sentinel's database and models. Plus a SHA-256 manifest.
- **The other host:** daily folders, unchanged files hard-linked, 7 days kept, mirrored by rsync over an SSH key confined to one directory (rrsync). `api/restore-from-backup.py` restores; restores were verified in S6.

| # | Stage | Who |
|---|---|---|
| K5.1 | **A third copy, on the USB device on Llwyn-y-Groes.** It is a SanDisk of 115 GB, label "Ubuntu-backups", currently NTFS and almost empty (69 MB used).<br>• **`backup-host.py` gains a local target** (`--to-dir <path>`) beside `--to <user>@<host>`, and `host_backup` takes `var_overrides.to_dir`.<br>• **A new schedule:** "Daily: back up this host to the USB device", after the 02:30 run.<br>• **Longer retention there,** since space allows: for example 30 daily copies plus 12 monthly. Unchanged files are hard-linked, so each extra day costs only what changed. | Claude |
| K5.2 | **The device mounted for good, and encrypted** (root steps, for Paul):<br>• **Today it mounts only while Paul is logged in** (`/run/media/scottp/…`, by the desktop session), so a 03:00 run could find it absent. It needs a fixed mount by UUID, with `nofail`.<br>• **It isn't encrypted,** while the host's own disk is LUKS. The backups include credentials data (API token records, peer data). Proposed: re-format it as LUKS + ext4. It's empty enough, but its 69 MB is checked with Paul first. It unlocks at boot with a key file kept on the host's encrypted root disk (`crypttab`, `nofail`).<br>• **ext4 also keeps** Unix ownership and permissions, and hard links behave as on the host.<br>• **If the device is missing,** the run reports "failed: USB device not mounted" in the schedule history, and Sentinel raises an event. It never writes into the empty mount point on the root disk. | **Paul** runs the root steps; Claude writes them |
| K5.3 | **What isn't covered yet** (checked 2026-10-08 against the 2026-10-08 backup's manifest). Each needs a decision: back it up, or record why not.<br>• **Loki's raw logs:** 37 MB (Llwyn), 102 MB (Stourport). Small; proposed: back up.<br>• **Keys and tokens** (`~/.config/cloudcore`): 16 KB a host. Proposed: back up, encrypted. Losing them means re-pairing the hosts and re-issuing every key.<br>• **The LFS build machine's disks,** checkpoints included: 44 GB of the 56 GB of instance disks on Llwyn. The journal survives without them, but the compiled system doesn't. Proposed: to the USB device only, while a build runs.<br>• **llm-chat's coordinator state** (model speeds, the worker's key and logs): small, inside its VM; relearned or re-keyed if lost.<br>• **Grafana:** 449 MB on Llwyn, mostly recreated by its setup script.<br>• **Tutor session records** on Stourport: 88 KB. The lessons themselves are in the journal.<br>• **The package repo mirror:** 216 GB a host. Both hosts hold a copy, and it can be re-fetched and checked against stored hashes. Proposed: not backed up.<br>• **Off-site:** every copy is in the same house. An encrypted copy of the small items to S3 (eu-west-2) is an option. | Claude proposes; **Paul** decides |
| K5.4 | **Restores, actually tested:** a monthly restore of the latest USB and peer copies into a scratch directory, with `PRAGMA integrity_check` and the manifest's hashes checked, reported in the schedule history. A backup that has never been restored isn't yet a backup. | Claude builds; the scheduler runs |
| K5.5 | **Retention inside Sentinel's database,** with the three conditions in `sentinel-Roadmap.md` SN-10 (2026-10-09): never archive human-judged suggestions; events stay available for learning (train the anomaly scorer first, and keep the archive or a de-duplicated corpus readable by training); collapse duplicate unreviewed suggestions into counted rows. Then archive old events and collapsed suggestions into the backup, and remove them after an agreed age. Nothing is deleted before it is in a backup. | Claude; **Paul** agrees the ages |
| K5.6 | **One data-access layer** in Sentinel (and later CloudCore): all SQL in one module per area, so a later move to PostgreSQL is a contained change, not a rewrite. Done as code is touched (K1 and K2 already reshape the findings queries), not as a big-bang refactor. | Claude |

## Order and dependencies

- **K1 first.** It needs no new data and fixes the "never see all 272" problem on its own.
- **K2 after K1:** K2.2's list is agreed before K2.3's rules are tuned.
- **K3 after K2,** and before LFS Phase F (run 2), which uses K3.2's fault classes.
- **K4 needs only K1** (K4.4's filtered export uses K2's subjects when they exist). It can go alongside K2.
- **K5 stands alone.** K5.1 and K5.2 come first, since losing data is the one thing that can't be undone, and K5.2's root steps can be done whenever Paul likes. K5.6 travels with K1/K2.
- **All of it waits for LFS run 1 to complete** (Paul, 2026-10-08).

## Risks

- **Over-matching rules:** the first trial put 169 findings under several subjects. Mitigation: one primary subject from file paths, a dry run per rule change, and the 90 % target measured, not assumed.
- **A list that drifts:** too many subjects make the filter useless. Mitigation: a short agreed list (K2.2); adding a subject is a decision, not a side effect.
- **Model suggestions taken as fact:** K2.5's suggestions never count until a person approves them.
- **Breaking callers:** the matcher, `/api/status` and CloudCore's relay read findings. Mitigation: the no-parameter `GET /api/findings` is unchanged, and the matcher doesn't use subjects.

## Decisions (Paul, 2026-10-10)

Made when implementation started, after LFS run 1.

| Decision | Choice |
|---|---|
| **K2.2** the subject list | The 14 as proposed: llm-chat · lab VMs · compute · networking · HA stack · Terraform provider · auth · peers / two-host · package repo · logging · scheduler · dashboard UI · Sentinel · LFS build |
| **Page size** | 50 by default; 25, 50 or 100 to choose from |
| **K2.5** model suggestions | Yes: the 14B suggests a subject for findings the rules can't place; nothing counts until a person approves it |
| **K3.2** fault classes | Five: controller · 14B mistake · tutor (Claude) · platform · book (the book's own text misleads). As in the run 1 report |
| **K4-a** PDFs | WeasyPrint on the server (a root step on Llwyn for its system libraries) |
| **K4-b** style | The house style (navy/green, title block, Courier New code) |
| **K4.4** the filtered export | Now, with single findings |
| **K5.1** USB retention | 30 daily and 12 monthly |
| **K5.2** the USB device | Re-formatted as LUKS + ext4, after Paul checks its 69 MB; unlocked at boot from a key file on the host's encrypted disk |
| **K5.3** what else is backed up | **Keys and tokens** (`~/.config/cloudcore`, encrypted) and **Loki's raw logs**. **Not** the LFS build disks or images: they can be rebuilt. **One LFS image is kept, the latest built,** for testing; older images (and the build VMs behind them) are removed when a newer one replaces them. No off-site copy for now |
| **K5.5** Sentinel retention | Events and collapsed unreviewed suggestions older than **90 days** are archived to the backup and removed from the live database, after SN-13's first training and only once they're in a backup. Human-judged suggestions are never archived |

## Open decisions (as first written)

- **K2.2:** the subject list.
- **Page size:** the default of 50, and whether 25/50/100 is the right choice.
- **K2.5:** whether to have model suggestions at all, or leave the few unplaced findings to be done by hand.
- **K3.2:** the fault classes. Is "book" (the book's own text misleads) a class of its own?
- **K4-a, how PDFs are made:**
  - **Server-side with WeasyPrint** (HTML+CSS to PDF): a consistent document, a real download, works for K4.4's multi-finding export. It adds a Python dependency with system libraries (Pango) on the Sentinel host. **Recommended.**
  - **The browser's own print-to-PDF,** from a print stylesheet: no new dependencies, but the user goes through the print dialog, and the result varies by browser.
  - **Pandoc with a LaTeX engine:** the best typography, but a large install (TeX), for little gain here.
- **K4-b, house style:** plain and readable, or your navy/green house style (colours, title block, Courier New for code)? The house style is a stylesheet, so it's easy either way.
- **K4.4:** wanted now, or later?
- **K5.1:** retention on the USB device (30 daily + 12 monthly proposed).
- **K5.2:** re-format the USB device as LUKS + ext4 (after checking its 69 MB), or keep NTFS and encrypt the backup files themselves instead.
- **K5.3:** which of the uncovered items to back up.
- **K5.5:** the ages after which Sentinel's events and suggestions are archived.
