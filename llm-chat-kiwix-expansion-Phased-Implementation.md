# llm-chat — Kiwix Expansion: Phased Implementation

Paul Scott | Direction agreed 2026-09-28

---

## Context

llm-chat grounds answers against a kiwix-serve VM (F-132 onwards). Code
is also grounded by really running it, but conceptual answers
(architecture, Linux, protocols) have no other check. The kiwix VM
currently carries 17 ZIMs: Wikipedia, man pages, the Arch Wiki, and the
Python docs and PEPs plus 12 Python-ecosystem DevDocs sets. It has
nothing for Go, C, C++, Rust, assembler or systems architecture.

Direct request: "as much kiwix education material in programming and
system architecture (compute, not bricks and mortar) as well as python,
go, c, c++, rust and assembler", then "i'm in favour of gathering as
much information as it's possible".

**Decisions (2026-09-28):**
- Content goes into **llm-chat's own kiwix VM**, not the standalone
  `kiwix-library` example. The learning happens in llm-chat.
  `kiwix-library` is left as it is.
- All four tiers below go in.
- **Stack Overflow (107GB) waits** until search latency and relevance
  have been measured with everything else loaded. It would not fit on
  the kiwix VM's 100GB disk anyway, and would need its own volume.

## Content (from the live Kiwix OPDS catalog, 2026-09-28)

| Tier | ZIMs | Size |
|---|---|---|
| 1. Language & tool references | DevDocs: Go, C, C++, Rust, GCC, GNU Make, CMake, Bash, Git, Docker, Kubernetes, Terraform, Ansible, nginx, HTTP, Node, JavaScript, TypeScript; Go by Example | ~65MB |
| 2. CS & architecture | *Algorithms* (Erickson), *Open Data Structures*, CS, CS Theory, Software Engineering, Code Review, Network Engineering, Security, DevOps and Programming Language Design Q&A; Cloudflare Learning Center; freeCodeCamp; LibreTexts Engineering | ~3.5GB |
| 3. Systems & ops | Unix & Linux, Server Fault, Super User, Ask Ubuntu Q&A; Gentoo and Alpine wikis | ~9.3GB |
| 4. Low-level & textbooks | Wikibooks (text-only), Reverse Engineering, Retrocomputing, Electronics Q&A | ~7.6GB |

**Gaps, stated rather than papered over:** the catalog has no dedicated
assembler reference (no x86/ARM instruction reference, NASM manual or
OSDev wiki). Assembler coverage comes via Wikibooks (x86 Assembly, x86
Disassembly, MIPS, 6502) and the Reverse Engineering and Retrocomputing
Q&A. For Rust, only the standard-library docs are available, not the
Rust Book. Building our own ZIMs for these is possible later.

**Disk:** the kiwix VM (`standard.2xlarge`) has 100GB; the total after
this is ~80GB including the OS.

## Stages

| # | Stage | Status |
|---|---|---|
| K1 | Download and checksum every ZIM into the host artifact cache | Done — 42 new, 59 total (73GB), all verified against Kiwix's published sha256 |
| K2 | Replace the per-ZIM variables (3 each) with one ZIM manifest, each entry tagged with the panels that search it; TF + Ansible | Done — verified live (59 of 59 registered) |
| K3 | Per-panel search: coding searches language + CS + low-level books, Linux Help searches systems + man pages + wikis; Wikipedia in both | Done — `fill`: specialists first, Wikipedia for empty slots (chosen by K5) |
| K4 | Clickable citations: each reference links to the real kiwix page, proxied through verify-proxy (content paths only) | Done — verified live through the LB |
| K5 | Measure search latency and hit relevance before/after, on real questions; decide on Stack Overflow from the numbers | Done — see results below |
| K6 | Serve the library from the host over read-only NFS instead of copying it into the VM | Done — verified live (2 min boot, 1.7GB disk) |
| K7 | Stack Overflow (107GB): download, decide its place by benchmark, verify live | Done — reserved slot; verified live |
| K8 | Weekly update check: fetch newer ZIM releases that grew meaningfully, verify, update and push the manifest | Done — scheduler kind `kiwix_update`, Sun 03:00 UTC (F-173) |

## Results (2026-09-28)

**Latency (K5).** Baseline, 17 ZIMs, one combined search: median 0.05–0.09s, max
0.37s. Full library, 59 ZIMs, `fill`: locally median 0.05s coding / 0.15s Linux,
max 0.27s; live through verify-proxy, median 0.14–0.15s, max 0.27s once warm.
A cold index took 7.8s for its first query, so the kiwix VM now warms its
indexes at boot (F-169). Latency leaves plenty of room for Stack Overflow.

**Relevance (K5)**, 26 real queries, every hit judged by hand:

| Strategy | Coding (32 slots) | Linux (20 slots) |
|---|---|---|
| combined | ~26 | ~19 |
| **fill** (chosen) | **~28** | **~19** |
| split | ~27 | ~14 |

After also skipping Stack Exchange tag pages and recovering empty-snippet hits
(F-168), `fill` gives, for example: x86 stack frame → Wikibooks *X86
Disassembly/Functions and Stack Frames* + NASM Q&A; Rust lifetimes → Rust
docs; Big O → Software Engineering Q&A + Wikipedia *Big O notation*. Still only
fair: Rust borrow-checker and segfault land on tangential Q&A threads, a
content limit (no Rust Book in the catalog).

**Found on the way:** F-167 (the codebase tier pre-empted kiwix for generic
questions), F-168 (kiwix search semantics; empty-snippet hits were silently
dropped; tag pages), F-169 (cold-index latency).

**Stack Overflow (K6/K7, 2026-09-29).** Disk ruled out a second copy, so the
kiwix VM now reads the host's artifact cache over a read-only NFS export
(F-170): ready 2 minutes after apply instead of ~20, 1.7GB of VM disk instead
of 75GB. Downloaded in ~20 minutes with aria2c across 7 Kiwix mirrors
(~108MB/s) and verified against Kiwix's published sha256.

Where it sits was decided by measurement (F-172):

| Placement | Result |
|---|---|
| First tier | 25/32 coding slots; displaced LibreTexts, Wikibooks and the Rust docs; median latency 5–10× |
| Fallback | Never used: weak curated hits filled both slots, even for error messages |
| **Reserved slot (chosen)** | Slot 1 = best curated, slot 2 = Stack Overflow: exact threads for every error message tested, textbooks kept |

Live over NFS: cold, never-seen queries took up to 3.1s, so the mount was
tuned, the boot warm-up now covers error-message vocabulary, and the kiwix
search budget went from 3s to 8s. End to end: an `UnboundLocalError` Ask was
answered correctly, the fix was verified in the sandbox, and it cited the
Python docs' *Execution model* plus the exact Stack Overflow thread.

**Keeping it current (K8, 2026-09-29).** A CloudCore schedule, "Weekly:
kiwix ZIM update check" (Sundays 03:00 UTC), compares every manifest entry
with the live Kiwix catalog. A newer release is taken if it grew by ≥5% or
≥200MB. It must leave 20GB free, and it is sha256-verified before the
manifest changes. The manifest alone is then committed and pushed. Replaced
files stay for 14 days and are never removed while a kiwix VM is running.
Running builds keep their files, and the next build picks up the new ones.
Thresholds, reserve and git mode are set per schedule in the Dashboard
(F-173).

Methodology unchanged: build and verify live, log findings as F-NNN,
tear down after.
