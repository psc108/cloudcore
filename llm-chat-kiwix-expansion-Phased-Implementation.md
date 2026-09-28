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
| K1 | Download and checksum every ZIM into the host artifact cache | Not started |
| K2 | Replace the per-ZIM variables (3 each) with one ZIM manifest, each entry tagged with the panels that search it; TF + Ansible | Not started |
| K3 | Per-panel search: coding searches language + CS + low-level books, Linux Help searches systems + man pages + wikis; Wikipedia in both | Not started |
| K4 | Clickable citations: each reference links to the real kiwix page, proxied through verify-proxy (content paths only) | Not started |
| K5 | Measure search latency and hit relevance before/after, on real questions; decide on Stack Overflow from the numbers | Not started |

Methodology unchanged: build and verify live, log findings as F-NNN,
tear down after.
