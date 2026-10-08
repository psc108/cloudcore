# LFS OS Build — Findings Log

**Owner:** Paul Scott. **Plan:** `lfs-os-Phased-Implementation.md`.

Everything of value found while building the OS: problems with the books and their published data, upstream quirks, what llm-chat (the 14B) gets right and wrong, how the lab handles the build, and what tutoring it needed.

- **Where these go:** platform bugs found along the way (CloudCore, the lab machinery) go in `haFullStack-Findings-Log.md` as F-NNN.
- **Format:** each entry has the same fields as that log. Sentinel ingests this file into its knowledge base (headings `### LFS-NNN — title`), and its matches are fed back to llm-chat during the build (C4/C5).

### LFS-001 — LFS's stable-systemd source list also carries SysVinit-only files

**Where:** `https://www.linuxfromscratch.org/lfs/downloads/stable-systemd/wget-list` (LFS 13.1); `lfs/build-manifest.py`.

**Symptom:** found 2026-10-07 (A1). The systemd edition's `wget-list` has 99 entries, but its `md5sums` has 94. The 5 extra are `lfs-bootscripts`, `sysklogd`, `sysvinit` and its patch, and `udev-lfs`, which only the SysVinit edition uses.

**Root cause:** the download directory serves one shared `wget-list` for both editions; only `md5sums` is edition-specific.

**Fix:** the manifest takes the systemd set from `md5sums`, uses `wget-list` only for those files' URLs, and logs what it leaves out.

**Verified by:** 93 LFS files plus the book's kernel = the 94 `md5sums` entries, all mirrored and MD5-verified.

### LFS-002 — BLFS publishes no checksums for its patches, books or systemd units

**Where:** BLFS 13.1 (systemd): the efivar patch, the book tarballs, `blfs-systemd-units`; `api/lfs-mirror.py`.

**Symptom:** found 2026-10-07 (A1/A2). BLFS package pages give an MD5 for each source, but the "Required patch" links have none, and neither do the downloads directory's book and unit tarballs.

**Root cause:** BLFS publishes checksums only for package sources.

**Fix:** those files are pinned by SHA-256 at their first download over HTTPS, recorded in `artifacts/lfs/MIRROR.json`, and every later run must match.

**Verified by:** 4 such files recorded on the first run. A changed local copy is detected and replaced (tested).

### LFS-003 — LFS's advisories index links a page for 13.1 that doesn't exist

**Where:** `https://www.linuxfromscratch.org/lfs/advisories/` → `13.1.html`; the same for BLFS. `api/lfs_updates.py`.

**Symptom:** found 2026-10-07 (A5). The advisories index says "The advisories for LFS-13.1 … are at LFS-13.1" and links `13.1.html`, which returns 404, for LFS and for BLFS. The 13.1 errata page's "Known Security Vulnerabilities" section only points back to the advisories page.

**Root cause:** upstream: the per-version advisory page isn't published yet, or is misnamed.

**Fix:** the weekly update check watches the errata page, both advisories indexes and the per-version pages, reports a 404 as "not published yet", and reports when it appears.

**Verified by:** the first scheduled run (2026-10-07) logged both 404s and returned "nothing new".

### LFS-004 — Given the right book page, the 14B still answered generically instead of following it

**Where:** llm-chat (Qwen2.5-Coder-14B Q4), Linux Help, with the LFS ZIM in its Kiwix tier (A4b).

**Symptom:** found 2026-10-07. Asked "In Linux From Scratch, how do I build GRUB for 64-bit UEFI instead of BIOS?", llm-chat's top source was the right page, LFS 8.65 GRUB-2.14. Its answer was still a generic guide ("install dependencies", "download GRUB from the official website") without the book's `--target=x86_64 --with-platform=efi` configure line.

**Root cause:** search grounding gives the model a snippet to consult, not text to follow. A 14B leans on its general knowledge unless the exact steps are in front of it and the task is to carry them out.

**Fix (design, for C2):** the build's worker loop gives the model the exact section's commands for the subsection in hand (`sections-lfs-13.1.json`, A4a), and asks it to follow and check them, not to recall them.

**Verified by:** to be measured in C6, where the loop is proven on a small slice.

### LFS-005 — The book's headings carry no dot after the section number

**Where:** the LFS 13.1 HTML book's `<h1>` titles; `lfs/book-sections.py`.

**Symptom:** found 2026-10-07 (A4a). Parsing "8.65. GRUB-2.14" style headings found none: the rendered title text is "8.65 GRUB-2.14". The number ended up in the title and the package name ("8.65 GRUB").

**Root cause:** the HTML holds the number and the title in separate elements; the dot belongs to neither.

**Fix:** the dot after the number is optional. Titles like "Binutils-2.47 - Pass 1" are split into package, version and stage.

**Verified by:** GRUB → 8.65 / GRUB / 2.14; Binutils pass 1 → 5.2 / Binutils / 2.47 / "Pass 1"; the kernel → 10.3 / Linux / 7.1.8.

### LFS-006 — The build machine can't fetch its sources from the repo: the lab fence keeps it off the host

**Where:** the `lfs-build` lab VM (B4), on the isolated lab network `cclab0` (`api/setup-lab-network.sh`, table `inet cclab`).

**Symptom:** found 2026-10-07, testing B4. The plan's sources (A2, 783 MiB under `artifacts/lfs/`) are served by the host's package repo on 8090. From `cclab0` the host accepts only DHCP and DNS, and routed traffic to private ranges is dropped, so the build machine can't reach the repo.

**Root cause:** by design. Lab VMs run model-written commands, so the lab network fences them off from the host's services and the LAN. The build machine is a lab VM for the same reason: llm-chat proposes its commands.

**Fix (design, for C2):** the controller delivers what each step needs. It has access to the repo, and it copies the sources into the build machine over the control channel (SSH on 1022), verified against `MIRROR.json` on arrival. The fence is not loosened for the build.

**Verified by:** to be verified in C6.

Also noted while testing: the build machine's `/bin/sh` is `dash`, as Ubuntu ships it. LFS requires bash there (chapter 2.2's version check fails otherwise). The book makes that change part of host preparation, so it's llm-chat's first task in D1, not the broker's.

### LFS-007 — The LFS image must ship with an empty /etc/machine-id

**Where:** LFS 13.1 (systemd), the systemd section of chapter 8, which runs `systemd-machine-id-setup`; D6 (the image import).

**Symptom:** found 2026-10-07 while testing B2 (see F-232). An image imported from a booted system carries its `/etc/machine-id`. systemd-networkd's DHCP client ID derives from it, so every instance of the image asks for the same address.

**Root cause:** LFS creates the machine ID during the build. That's right for one machine, but every copy of an image would share it.

**Fix (for D6):** before the disk is imported as an image, truncate `/etc/machine-id` to empty, not delete it. systemd then generates a new one at each instance's first boot.

**Verified by:** to be verified in D6, by booting two instances of the LFS image together and checking they get different machine IDs and addresses.

### LFS-008 — The book puts command alternatives inside notes, and their headings aren't subsections

**Where:** the LFS 13.1 HTML book's admonition boxes (`div class="admon"`: Warning, Note, Important); `lfs/book-sections.py`; `api/lfs_build.py`.

**Symptom:** found 2026-10-07 (C1). Section 10.4, "Using GRUB to Set Up the Boot Process", came out with commands under subsections called "Warning" and "Note". Among them were alternatives the build must choose between: a rescue CD (`grub-mkrescue`, `xorriso` to `/dev/cdrw`), BIOS install (`grub-install --target=i386-pc`), UEFI install (`grub-install --target=x86_64-efi --removable`) and an optional `efibootmgr` entry. So nothing could be filtered by boot method.

**Root cause:** the extractor treated every `<h3>` as a new subsection, including an admonition box's own heading, so the real subsection ("10.4.4.1 Booting With BIOS", "10.4.4.2 Booting With UEFI") was lost.

**Fix:**
- **Notes keep their subsection:** headings inside admonitions now label the commands (`note`) without replacing the subsection.
- **Boot method:** the queue skips BIOS and 32-bit UEFI subsections by name.
- **Single commands:** a per-command skip list records the reason, for example the rescue CD: "an optional rescue CD; the lab machine has no CD writer".

**Verified by:** 10.4 now serves the UEFI `grub-install`, the efivars/efibootmgr note commands and the `grub.cfg` creation. The BIOS command and the rescue CD are left out, with reasons. 8.65 (GRUB's build) skips its BIOS and 32-bit UEFI subsections.

### LFS-009 — Asking the 14B to copy the book's commands back made planning slow and risky

**Where:** `examples/llm-chat/files/lfs_worker.py` (C2: the plan the model writes for each task).

**Symptom:** found 2026-10-07, on the first plan-only run (task 1, 2.2 Host System Requirements). The plan format asked the model to return every book command verbatim, with any changes. 2.2's one command is the book's ~60-line `version-check.sh`. On these CPUs the 14B reads about 4 tokens/s and writes about 2. Reading the prompt plus re-typing the script in JSON ran past 20 minutes, and the run's own time limit stopped it before any plan arrived.

**Root cause:**
- **Cost:** a planning call cost was proportional to the section's length, not to the work it needed.
- **Risk:** a 14B re-typing a long script can silently change it, so the copying was also a correctness risk.

**Fix:** the model now writes **only differences**: a command changed (with the filled-in text), left out, moved to another user, or added (`after: n`, or `-1` for first), each with a reason. Every other command runs exactly as the book prints it, inserted by the controller.
- **Guards kept:** a command still holding a placeholder (`/dev/<xxx>`) must be changed; nothing may touch the system disk; every change needs a reason.
- **Offline tests:** "as the book", fill-in plus omission, a commandless section's added steps (kept in the order given; an ordering bug was found and fixed by this test), a refused system-disk command, a bad reference.

**Verified by:** the re-run of task 1's plan (journalled), and C6.

### LFS-010 — The first plan took 47+ minutes on a busy desktop host while a faster model sat idle

**Where:** the LFS worker's first plan (task 1, 2.2), on the lab's 14B on Stourport.

**Symptom:** found 2026-10-07. Prompt ~2,700 tokens; the model was writing at ~0.4 tok/s and reading at ~1.8, against a benchmark of 1.9/3.9. Stourport is Paul's desktop, and was under load (5.4 on 4 threads). The coordinator's own model on Llwyn-y-Groes was idle throughout.

**Root cause:** routing chose by "free slot", not by expected speed. That's a platform gap, logged as F-236.

**Fix:** F-236 (the live-speed router). For the build: planning cost now scales with live speed and with the size of the request (LFS-009 cut the output side).

**Verified by:** the LFS worker's next plans; their journal records which endpoint answered.

### LFS-011 — The 14B's plan wasn't valid JSON twice in a row, and the worker didn't keep what it said

**Where:** `examples/llm-chat/files/lfs_worker.py` (C2: `plan`, `ask_model`).

**Symptom:** found 2026-10-07, on the first real run (task 1, 2.2). Both planning tries came back unusable ("the reply wasn't the JSON asked for"), about 20 minutes in all, and the task was marked stuck. The same task's plan-only run minutes before had replied with clean JSON (in a code fence, which the worker handles), so the model can do it, just not reliably.
- **A gap in the worker:** a rejected reply wasn't journalled, so what the model actually wrote was lost.

**Root cause:** a 14B asked for JSON in prose instructions drifts sometimes: extra text, a truncated object, or the wrong shape. Nothing made valid JSON certain.

**Fix:**
- **Constrained output:** every model call now sends a JSON schema in `response_format`, so llama-server's grammar keeps the model to that shape (plan changes, repair, test judgement, output judgement). Tested on the coordinator's model: the reply came back in exactly the requested shape.
- **The raw reply:** any plan that can't be used is now journalled with what the model wrote.

**Verified by:** the re-run of tasks 2.2–2.7.

### LFS-012 — The 14B wrote the right repair in its explanation and left the command field empty

**Where:** `examples/llm-chat/files/lfs_worker.py` (C2: the repair call, `FIX_SCHEMA`).

**Symptom:** found 2026-10-07, task 1 (2.2). The version check printed `ERROR: sh is NOT Bash` with exit 0. The worker caught it, and the 14B judged it correctly ("NOT met ... /bin/sh should be a symbolic or hard link to bash"). Its repair said, in the free-text `why`: "You can do this by running 'sudo ln -sf /bin/bash /bin/sh' before running the script again", but left `before` (the commands to run) **empty**. Nothing ran, the check failed twice more, and the task stopped for help.

**Root cause:** the schema had three free-text strings, one optional in practice. The model's understanding was right, but the shape let it put the action in the explanation.

**Fix:**
- **A required list:** `commands`, a non-empty list of commands (`minItems: 1`), kept separate from `cause`; and `then`: "rerun the step" or "run this instead".
- **The prompt:** says plainly that only `commands` is run, nothing written in `cause`.

**Verified by:** the same failure put to the coordinator's 14B returned `"commands": ["ln -sf bash /bin/sh"], "then": "rerun the step"` in 58 s, the book's own fix. Next: the re-run of 2.2–2.7.

**Lesson for the protocol:** with a small model, put every action in a constrained field of its own. Prose is for reasons, never for anything the controller must act on.

### LFS-013 — In a section with no book commands, the 14B's steps were sound but rejected, and it reached into later sections

**Where:** `examples/llm-chat/files/lfs_worker.py` (C2: `plan`, the added-step rules); task 2, 2.4 "Creating a New Partition", which has no commands (the book uses interactive cfdisk).

**Symptom:** found 2026-10-07. The 14B's two plans for 2.4 were sound UEFI layouts with `sgdisk`: a GPT table, an EFI system partition (`ef00`; 100 MiB, then 512 MiB on the second try) and an ext4 root. Both were rejected:
- **Reply 1:** positions added steps with `after: 0, 1, 2…`, numbering its *own* steps. The checker only accepted book command numbers, and this section has none.
- **Reply 2:** told so, it dropped the positions altogether, which was rejected as "refers to command [None]".
- **Scope:** both plans also formatted and mounted the partitions, which is the work of 2.5 and 2.7. Run, 2.5 would have reformatted them and 2.7 would have failed on "already mounted".

**Root cause:**
- **The format (the worker's):** "after" was ambiguous for a section with no commands.
- **The scope (the model's):** it wasn't told which later sections exist, so it reasonably did the whole job.

**Fix:**
- **Order:** an added step without a valid book reference keeps the order given (after the steps added so far); `after: -1` still means first. Both real replies now parse in order.
- **Scope:** the prompt names the next three sections ("NOT yours to do now") and says to do only this section's work.

**Verified by:** offline, on both of the 14B's actual replies, and mixed cases. Live: the re-run of 2.4.

**Note for the tutor role:** the model's grasp of a UEFI layout was right without help. What it needed was clearer boundaries, not knowledge.

### LFS-014 — The 14B misread the book's alternatives (the EFI partition's FAT32 as "swap"); one tutoring note fixed it

**Where:** tasks 3 and 5 (2.5 Creating a File System, 2.7 Mounting), on the 14B; the first tutoring through the journal.

**Symptom:** found 2026-10-07.
- **2.5:** the 14B left out `[2] mkfs.fat -F 32 /dev/<yyy>` and `[3] mkfs.vfat` as "No swap partition is needed": they are the book's commands for the **EFI system partition**. `blkid` confirmed `/dev/sdb1` had no filesystem.
- **2.7:** the plan then mounted the EFI partition with `-t ext4`, and its rewrite of command [0] dropped the book's own `mkdir -pv $LFS`.
- **Its repairs were shallow:** `mkdir` without `-p`, then `rm -rf $LFS/home` to make a retry pass. The task stopped for help.

**Root cause:** the model. It understood the layout (its partitioning in 2.4 was right) but misread a list of alternatives in the book. Its repairs treated symptoms, not causes.

**Fix (tutoring, by hand: C5's automation isn't built yet):**
- **Lessons:** Claude wrote one lesson each for 2.5 and 2.7 into the journal, explaining why: the firmware reads FAT32 only; [1] is the book's example for a separate /home; mount the EFI partition as vfat after `mkdir -pv`; fix causes, don't delete directories.
- **The worker:** now puts a section's tutor notes in front of the model ("your tutor's notes for this section: follow them").
- **The machine:** the half-done mounts were cleared, and the 14B redid both tasks itself.

**Verified by:**
- **2.5:** ext4 on `/dev/sdb2`, `mkfs.fat -F 32 /dev/sdb1`, swap left out. It also left `[3]` out on its own correct reasoning ("does the same as mkfs.fat -F 32").
- **2.7:** `/dev/sdb2` ext4 on `/mnt/lfs`, `/dev/sdb1` vfat on `/mnt/lfs/boot/efi`.
- **The machine:** `blkid` and `findmnt` agree, and the worker recorded the mounts.
- **Residual:** it again dropped `mkdir -pv $LFS` from [0], against the note. Harmless here (the directory existed), but notes aren't followed perfectly.

**Score so far (tasks 2.2–2.7, the judgement-heavy start):** of six stops, four came from the worker's own design (LFS-009, -011, -012, -013) and two from the model's knowledge (this one). One tutoring note per section was enough.

### LFS-015 — A repair deleted the build's freshly delivered sources

**Where:** task 6 (3.1 Introduction); `examples/llm-chat/files/lfs_worker.py` (3.1 delivery, the repair rules).

**Symptom:** found 2026-10-07.
- **The delivery:** the controller delivered and SHA-256-checked 100 source files (633 MiB).
- **The failure:** the book's `mkdir -v $LFS/sources` then failed: "File exists".
- **The repair:** the 14B's repair was `rm -rf /mnt/lfs/sources`, run **twice**. The sources were gone, and the book's next command (`chown root:root $LFS/sources/*`) failed with nothing to act on.
- **A sign of the habit:** the same "delete to make the retry pass" as in 2.7 (LFS-014), despite the tutor's lesson there, which was a lesson for that section only.
- **Also:** the 14B left out the book's `md5sum -c` as "not necessary" because the controller verifies. Defensible, but against the book.

**Root cause:**
- **The worker's:** it created and filled `$LFS/sources` *before* the book's own `mkdir`, so a correct book command failed.
- **The model's:** with "already exists" in front of it, its only way forward was deletion, and nothing stopped a destructive repair.

**Fix:**
- **Order:** the delivery now stands **where the book's wget is**, after the book's own `mkdir` and `chmod`, before its `md5sum -c`. The delivery also makes sure the directory exists.
- **A non-destructive way out:** "the step's work is already done" is now a repair option. The controller accepts it only for an "already exists" failure, and only if what the step makes is really there.
- **A hard guard:** a repair that deletes directories (`rm -r…`) is refused, except inside the package's own unpacked tree (clearing a `build` directory is a normal LFS retry). Tested offline: `rm -rf $LFS/sources`, `$LFS/home` and `/usr/lib` are refused; `rm -rf build` and single-file `rm` are allowed.
- **The prompt:** "never delete files or directories to make a step pass".

**Verified by:** the re-run of 3.1 onward. The sources are delivered again.

**Lesson for the protocol:** a section's tutor notes don't carry over to other sections. General habits, like "don't delete to retry", belong in the system rules and in hard guards, not only in notes.

### LFS-016 — The 14B invented reasons to leave out 4.2's commands: a non-existent command [3], an "editors' note", work "already done by the controller"

**Where:** task 7 (4.2 Creating a Limited Directory Layout); `examples/llm-chat/files/lfs_worker.py` (the plan's facts and the feedback on a rejected plan).

**Symptom:** found 2026-10-07. The section has two commands: [0] a multi-line block (`mkdir`, a `for` loop of `ln -sv`, a `case` creating `$LFS/lib64`) and [1] `mkdir -pv $LFS/tools`.
- **First plan:** "omit command [3]" (the `case` block, counted as its own command) because `/lib64` "is not desired according to the LFS editors' notes". There's no such note; on x86_64 the book wants it.
- **Second plan:** after "command [3] isn't in the list", it left out **both** commands as "already created by the controller's setup". Nothing had created them.
- **Result:** the worker rightly rejected a plan that would run nothing, and the task stopped.

**Root cause:**
- **The model's:** under correction it reached for a plausible justification rather than the book. It invented an authority, then a fact.
- **The worker's:**
  - The facts didn't say what exists under `$LFS`, so "the controller already did it" couldn't be checked against anything in front of the model.
  - The rejection named the bad index but not the valid ones, nor that a multi-line block is one command.

**Fix:**
- **The facts:** they now list what's in `$LFS` right now, and say the controller does nothing for the model beyond delivering sources and entering the context.
- **The rejection:** it now gives the valid command numbers and says a multi-line `case`/`for` block is ONE command.
- **Tutoring:** a lesson for 4.2: run both as the book; keep lib64; nothing here has been done yet; never claim work is done unless the facts show it.

**Verified by:** the re-run of 4.2 onward.

**Pattern (with LFS-014, -015):** when the 14B is pushed back, its weak move is to remove or delete work, not to find the real cause. The guards now catch the destructive form; the facts make the "already done" claim checkable.

### LFS-017 — 4.4's "As the root user, run:" command ran as lfs; the book's -j32 illustrations were offered as commands

**Where:** task 9 (4.4 Setting Up the Environment); `lfs/book-sections.py`, `api/lfs_build.py`, `examples/llm-chat/files/lfs_worker.py`.

**Symptom:** found 2026-10-07.
- **The root command:** the book's `mv -v /etc/bash.bashrc /etc/bash.bashrc.NOUSE` is introduced by "As the root user, run:". The worker ran it as `lfs` ("Permission denied").
- **The repairs:** the 14B twice tried `sudo mv ...`, which can't work with no terminal and no password. The task stopped.
- **The illustrations:** the book's `make -j32` and `export MAKEFLAGS=-j32` (shown for a 32-core i9) reached the model as commands [3] and [4]. It planned both "as the book", and misread [5] (`MAKEFLAGS=-j$(nproc)`) as setting -j32, rewriting it as `-j4`.

**Root cause:** the parser's.
- **Root marking:** it only knew `<pre class="root">`. LFS marks some root commands only in the sentence before them.
- **Illustrations:** it had no way to tell a book's illustration from a command to run. The model's `sudo` was a reasonable guess in a context it couldn't see.

**Fix:**
- **The parser:** a command is root when the sentence before it ends "as (the) root (user) ...:". Regenerated: 4.4 [2], the 4.2/4.3 host commands, 8.2's diagnostic, 10.3's `mount /boot`, and about 70 BLFS commands are now marked root.
- **A skip rule:** `make -j32` and `export MAKEFLAGS=-j32` are skipped as "the book's illustration for a 32-core CPU". Skip rules now apply at read time too, so a new rule reaches builds planned before it.
- **The worker:** a book command marked root runs as root inside an lfs-user section.
- **Also skipped:** 4.4's `source ~/.bash_profile`. Its `exec env -i /bin/bash` would start an interactive shell; the controller gives each lfs step the `.bashrc` environment itself.

**Verified by:** the re-run of 4.4.

### LFS-018 — Each step started in the task's fixed directory, so the book's `cd build` was lost; the 14B added a copy of the book's build commands

**Where:** task 10 (5.2 Binutils-2.47 Pass 1), the first compile; `examples/llm-chat/files/lfs_worker.py` (the launcher, the step loop, the plan check).

**Symptom:** found 2026-10-07. Every run of `../configure` failed with "No such file or directory".
- **The model's step:** an added step, placed first: `time { ../configure ... && make && make install; }`, with the reason "the book's command does not include the time measurement". The book's 4.5 suggests timing the first package to measure the SBU.
- **Where it ran:** before the book's own `mkdir -v build; cd build`, so it ran in the source directory, where `../configure` doesn't exist.
- **The repairs:** two attempts, re-extracting the tarball over itself and then `cd` to the source directory, both missed the cause.

**Root cause:**
- **The worker's:** it ran each step as its own script, starting in the task's fixed directory. The book's `cd build` in command [0] would never have reached [1]: even the book's unchanged order would have failed. Chapter 2–4 sections never changed directory, so this is the first section to show it.
- **The model's:** it added a duplicate of three book commands rather than changing one, and placed it first.

**Fix:**
- **The directory:** every step's script reports, on exit, the directory it ended in (a trap printing a marker line). The worker takes it from the output, removes the marker before journalling, and starts the next step there, as one shell would. Tested locally: carried on success and on failure, marker stripped.
- **The plan check:** an added step that contains a book command (12+ characters) is rejected: "the book's commands already run; to change one, give a change with its 'book' number".

**Follow-up:** on the re-run, the 14B tried the timed copy twice more; the new check rejected both. The book's 4.5 suggests timing this package to measure the SBU, and the model kept to that. Added to the planning rules, for every section: "the controller times every step and records it: never add `time` or SBU measurements", and "never add a copy of the book's own commands". The worker journals every step's duration already.

**Verified by:** 5.2 done 2026-10-07 16:32, on the third plan.
- **The run:** `mkdir build; cd build` carried into `../configure`; `make` took 80 s, then `make install`.
- **The result:** `/mnt/lfs/tools/bin` holds the 16 `x86_64-lfs-linux-gnu-*` tools; `ld --version` gives GNU ld 2.47.
- **Residual (harmless):** the 14B replaced `$LFS_TGT` with its literal value, on the mistaken reason that "the book's command does not specify the target". The value is identical; the reasoning was wrong. Its explanations still need reading with care even when its commands are right.

### LFS-019 — GCC pass 1 ran the build machine out of memory; the first live climb of the escalation ladder

**Where:** task 11 (5.3 GCC-16.2.0 Pass 1); the build machine (standard.large: 4 vCPU, 3.9 GB, no swap); `examples/llm-chat/files/lfs_worker.py` (machine set-up); Sentinel's ladder (C5).

**Symptom:** found 2026-10-07, 18:36–20:48 UTC.
- **Attempt 1:** the 14B rewrote command [0] as six commands with absolute paths, calling the book's commands "placeholders", and dropped configure, make and install. Step [6] then failed ("x86_64-lfs-linux-gnu-gcc: command not found"); its PATH repairs couldn't help. Stuck.
- **Rung 2 (Sentinel):** posted its three closest KB findings (LFS-015 at 0.44, LFS-016 at 0.32 …). None was relevant: the KB has no build-failure findings yet, and generic words matched.
- **Attempt 2:** the 14B first left out every command as "already done", then added a `wget`. The plan check rejected neither second plan (see the residual below). The book's commands then ran: `make` worked for 1073 s and died at the final links, with `ld terminated with signal 9 [Killed]`. Its repairs were `sudo sysctl` (no password) and `make clean`, which threw the work away. Stuck.
- **Rung 3, session 1** ($0.07-scale, about 3 min): the tutor diagnosed the out-of-memory correctly: 3.9 GB, no swap, `MAKEFLAGS=-j4` linking cc1, cc1plus, lto1 and lto-dump at once. Its lesson was `make || make -j1`. The 14B followed it exactly.
- **Attempt 3:** the parallel `make` was killed again; `make -j1` failed in 10 s with "cannot execute 'cc1'". Stuck.
- **Rung 3, session 2** (40 s, $0.55): the tutor found the cause its first lesson missed. The killed linker leaves a half-written `cc1` with a fresh timestamp and no execute bit, so `make` never relinks it. New lesson: `make || { rm -f gcc/cc1 gcc/cc1plus gcc/lto1 gcc/lto-dump; make -j1; }`, with explicit do-nots (`make clean`, `sudo`, repeating the failed repair).

**Root cause:** the platform's, not the model's. The `lfs-build` machine fell back to `standard.large` (3.9 GB) with no swap; GCC's parallel final links need more. The tutor's lesson works around it; it doesn't fix it, and GCC pass 2 and chapter 8's GCC will hit it again.

**Fix:**
- **Swap:** the worker's machine set-up now ensures an 8 GB swap file on the machine's own system disk (`ensure_swap`: idempotent, kept in `/etc/fstab`). That is the controller's environment, like the mounts. Applied live at 20:52 UTC, between attempts.
- **The lesson stays:** it's correct, and harmless with swap.

**Verified by:**
- **Swap:** `swapon --show` shows `/swapfile` at 8 GB.
- **5.3 done on attempt 5 (21:21 UTC):** the 14B planned the tutor's command exactly. `make || { rm -f …; make -j1; }` exited 0 after 1103 s, then `make install` and the book's `limits.h` step ran. With swap, the parallel `make` finished on its own, and the fallback wasn't needed.

**What the ladder showed (C5's first live run):**
- **Rungs 2 → 3 → 3 ran unattended,** each recorded in the journal.
- **The tutor reached the right diagnosis** on its own evidence: the 16 KB output tails and the facts the 14B saw.
- **Its second session corrected its first.**
- **Rung 2's nudge was noise.** A KB of platform findings matches build failures on generic words. It will improve as build findings and tutor lessons (`LFS-T…`) accumulate; until then a nudge costs one retry.

**Residual:**
- **An unchecked plan, now closed:** attempt 2's second plan added a `wget` (an internet download, against LFS-006) and the plan check let it through. A `wget`/`curl` of an http(s)/ftp URL is now refused in plans and repairs (`_DOWNLOAD`).
- **The cost of a retry:** each retry re-unpacks the package, so a retry of GCC pays the full 17-minute compile again.

### LFS-020 — 5.4 "done" without installing the headers; in 5.5 the 14B ran grub-install as root on the build machine's own disk

**Where:** tasks 12 (5.4 Linux API Headers) and 13 (5.5 Glibc-2.44); `examples/llm-chat/files/lfs_worker.py` (the plan and repair checks), `lfs/lfs-tutor.py` (the brief).

**Symptom:** found 2026-10-07, 21:29–22:36 UTC.
- **5.4:** the 14B "changed" the book's command [1] (`make headers`, `find usr/include … -delete`, `cp -rv usr/include $LFS/usr`) to just `make headers`, giving the kernel version as its reason. Both steps exited 0, and the task was marked done. The headers never reached `$LFS/usr/include`.
- **5.5:** glibc's configure failed: "GNU libc requires kernel header files from Linux 3.2.0 or later".
  - **Its repairs:** `sudo apt-get install linux-libc-dev`, four times (no password; the wrong idea anyway).
  - **Invented options:** `--enable-efi --with-elf=yes`.
  - **The damage:** after Sentinel's nudge, the 14B added steps run **as root**: `mount`, `grub-install --target=x86_64-efi --efi-directory=$LFS/boot/efi` and `grub-mkconfig`. `grub-install` exited 0, writing GRUB modules into the **build machine's own `/boot/grub` on `/dev/sda`**, a GRUB EFI binary onto the LFS EFI partition, and a `grub.cfg` listing the host's kernels.
- **The ladder:**
  - **Rung 2:** the nudge was irrelevant (the GCC lesson LFS-T339 at 0.21).
  - **Rung 3 (32 s, $0.42):** the tutor answered **"needs Paul"**. It traced the failure to 5.4's missing result, found and described all of the GRUB damage, and recommended a rollback plus three controller guards.
  - **Rung 4:** the build paused.

**Root cause:** the controller's.
- **Dropped lines:** a "changed" book command could drop lines; nothing compared the change with the book's.
- **Root steps:** an added step in an lfs-user section could run as root.
- **System commands:** nothing kept them (`grub-install`, `mount`, `sudo`, `apt-get` …) out of sections whose book commands don't use them.
- **No result check:** nothing checks that a section produced its result (follow-up).

**Fix:**
- **Rollback:** restored checkpoint `before-012-5-4-linux-7-1-8-api-headers` through the broker in 6.3 s. That undid the GRUB damage on both disks; the cross tools and swap were intact. **This proves C6's checkpoint restore.**
- **Dropped lines:** a change to a book command must keep that command's other lines. Each must still be there, at least 60 % similar (a version, a filled placeholder, a variable written out and options added all pass). Tested: 5.4's `make headers` alone is rejected, naming the dropped `find` and `cp`; 5.2's literal target, 5.3's tutor `make ||`, 2.7's filled placeholder and 4.4's `-j4` pass; 2.7's dropped `mkdir -pv $LFS` would have been caught.
- **System commands:** plans and repairs may use `grub-*`, `efibootmgr`, `mount`, `mkfs`, `sgdisk`, `mkswap`, `swapon` and `chroot` only where the section's own book commands do. A commandless section (2.4) is exempt; `_DANGER` still guards it. `sudo`, `apt-get`, `apt`, `dnf` and `yum` are refused everywhere.
- **Root steps:** in an lfs-user section, only the book's own root commands run as root.
- **The tutor's brief:** it now includes the commands that actually ran in the last three finished sections. A cause can lie a section back.

**Verified by:** the restore (above); the offline tests (above); the re-run of 5.4 and 5.5.

**Follow-up:** checks on a section's result (e.g. 5.4 → `$LFS/usr/include/linux/version.h` exists). The book's own sanity checks cover some sections (5.5's `readelf` test); most have none.

### LFS-021 — Told to "change version-specific names", the 14B rewrote 5.4's version-free commands; the new drop check rejected every plan

**Where:** task 12 (5.4 Linux API Headers, this build's kernel 7.2.9 against the book's 7.1.8); `examples/llm-chat/files/lfs_worker.py` (the plan prompt; the rejection message).

**Symptom:** found 2026-10-08, after the LFS-020 rollback.
- **Three plans, all rejected:** each "changed" commands that name no version. `make mrproper` was rewritten unchanged; command [1] was cut to `make headers`, or split into three changes to [1], or into commands [1], [2] and [3]. Every reason was "the build uses version 7.2.9 … change version-specific names accordingly".
- **The guard worked:** LFS-020's drop check rejected all three, so nothing wrong ran. But the task went stuck.
- **Rung 2:** Sentinel's nudge this time was relevant (LFS-020 at 0.51).

**Root cause:** the controller's prompt.
- **The version note:** for any task with a version override, the prompt said "change version-specific names accordingly", whether or not a command named a version. The 14B took it as an order to change something.
- **The rejection message:** it said lines were dropped, but not that the command is one multi-line command given whole, or that leaving it out of `changes` runs it as the book.

**Fix:**
- **The version note:** it now names the commands that contain the book's version, saying "change 'X' to 'Y' in command(s) [n] and nothing else". If none does: "None of this section's commands names a version, so the version needs NO change to them". Tested.
- **The rejection message:** "Command [n] is ONE command of k lines: a change gives the WHOLE command in 'run', with every line. If nothing in it must change, leave it out of 'changes' and it runs as the book has it."

**Verified by:** 5.4 done (see LFS-022's verification): its plan was as the book.

### LFS-022 — The 14B's "no changes needed" was refused as an empty added step; the build paused at rung 4 over a controller bug

**Where:** task 12 (5.4 Linux API Headers); `examples/llm-chat/files/lfs_worker.py` (`plan`).

**Symptom:** found 2026-10-08, 23:35–23:47 UTC, after LFS-021's fix.
- **The plans were right:** four times, the 14B's plan was correct, both commands as the book. Each reply carried one entry in `changes` with only a reason, for example `{"why": "The section's commands do not need any changes."}`.
- **The worker refused them:** it read the entry as an added step with no `run` and rejected every plan.
- **The ladder:** a tutor session (about 2 min) correctly said nothing had failed on the machine and wrote a "no changes" lesson. The 14B answered with the same remark, so rung 4 paused the build.

**Root cause:** the controller's. An entry with no `book`, `run`, `omit`, `as` or `after` was treated as a step, not a remark.

**Fix:** such an entry is ignored. The tutor's lesson couldn't have helped: the model was right.

**Verified by:** 5.4 done on its next run, as the book has it. `$LFS/usr/include` holds 1025 headers, and `linux/version.h` gives `LINUX_VERSION_CODE 459273` (7.2.9), checked on the machine.

**Pattern:** the last three stops (LFS-020's guards, LFS-021, LFS-022) have each been the controller tightening, then over-tightening. Each guard now has a test case; `tests/lfs_plan_check.py` now replays the recorded replies (19 cases, LFS-014 to -022) through the real `plan()`, offline; run it before deploying a worker change.

### LFS-023 — 5.6 ran with nothing unpacked: the controller found no tarball for "Libstdc++ from GCC" (and four more sections)

**Where:** task 14 (5.6 Libstdc++ from GCC-16.2.0); `examples/llm-chat/files/lfs_worker.py` (`source_file`); `lfs/lfs-tutor.py` (the tutor's rules).

**Symptom:** found 2026-10-08, 00:17–01:45 UTC.
- **Nothing to build in:** no "unpacked" note, so every step ran in `$LFS/sources`. `../libstdc++-v3/configure` didn't exist (exit 127).
- **The 14B's repairs** `cd`'d into a `gcc-16.2.0` that wasn't there.
- **The ladder:**
  - **Rung 2:** irrelevant.
  - **Tutor session 1:** diagnosed it exactly ("No GCC source tree existed for this section … the controller did not unpack"). But it chose a **workaround lesson**, telling the 14B to unpack GCC itself with an added step.
  - **The 14B:** put that step last.
  - **Tutor session 2:** named the controller's cause again ("it looks for a tarball named after the section title") and again gave a workaround, with absolute paths.
  - **The guards:** LFS-020's dropped-line check refused that, and rung 4 paused the build.
- **Meanwhile, 5.5 (glibc) built cleanly** in 8 min, with the book's sanity checks.

**Root cause:** the controller's. `source_file` matched tarballs by `<package>-<version>`. Five of the 115 package sections name their package differently from its tarball:

| Section | Package name | Tarball |
|---|---|---|
| 5.6 | Libstdc++ from GCC | gcc-* |
| 8.50 | Libelf from Elfutils | elfutils-* |
| 8.52 | Sqlite | sqlite-autoconf-* |
| 8.55 | Flit-Core | flit_core-* |
| 8.78 | D-Bus | dbus-* |

Also the tutor's: it saw a controller fault and handed the 14B a workaround that the controller's own guards then fought.

**Fix:**
- **The tarball lookup:** "X from Y" means Y's tarball. Otherwise names are compared with non-alphanumerics removed, allowing a short suffix (`autoconf`), and only tarballs count. All 115 package sections now map to a tarball containing their version; the offline check covers the five.
- **The tutor's rules:** a controller fault (a package section never unpacked, the wrong user, state lost between steps) is "needs Paul" at once. Lessons that work around the controller make the 14B fight its guards.
- **Cleanup:** the stray `$LFS/sources/build` and the 14B's own unpacked `gcc-16.2.0` are removed; the controller unpacks afresh.

**Verified by:** `tests/lfs_plan_check.py` (19 plan cases and 7 tarball cases); the re-run of 5.6.

### LFS-024 — After LFS-023's fix, 5.6's old workaround lessons kept steering the 14B, and its used-up ladder went straight to rung 4

**Where:** task 14 (5.6 Libstdc++); `examples/llm-chat/files/lfs_worker.py` (`tutor_notes`); `sentinel/lfs_watch.py` (`climb`).

**Symptom:** found 2026-10-08, 01:48–02:10 UTC.
- **The fix worked:** with LFS-023 fixed, the controller unpacked `gcc-16.2.0.tar.xz` and started 5.6 in it.
- **The old lessons won:** the 14B followed the two earlier tutor lessons, "unpack GCC yourself with an added step" and "rewrite [0] with absolute paths". Both were workarounds for the fault just fixed. The dropped-line check refused the rewrite twice, so the task went stuck.
- **Sentinel went straight to rung 4:** the task's nudge and both tutor sessions were already spent on the old fault, so the build paused at once.

**Root cause:** two gaps in the ladder's design.
- **Lessons:** they can't be withdrawn. Every lesson ever written for a section goes into the 14B's prompt.
- **The ladder:** a human fix doesn't reset it, so rungs spent on a fault that no longer exists still count.

**Fix:**
- **The worker:** a lesson with `{"supersedes": true}` retires every lesson before it in that section.
- **Sentinel:** a journal note with `{"ladder": "reset"}` starts the climb again; rungs before it no longer count. Tested (8 Sentinel tests; 26 offline plan cases still pass).
- **The protocol:** a human fix that makes earlier lessons wrong posts a superseding lesson and a ladder reset with the task reset.

**Verified by:** the re-run of 5.6.

### LFS-025 — Chapter 7 checked before it ran: an interactive login shell and a backup that leaves the chroot (prevented)

**Where:** sections 7.6, 7.15 and 8.39; `api/lfs_build.py` (`_SKIP_COMMANDS`).

**Symptom:** found 2026-10-08 by reading the chroot chapter's commands before the run, after chapters 5 and 6 finished (31 of 145 tasks done; chapter 6 had 16 of 17 sections done on the first attempt).
- **7.6 [5] and 8.39 [5]:** `exec /usr/bin/bash --login`, an interactive login shell. Run as a step, it would hang or end the step's shell.
- **7.15's backup subsection:** `exit` the chroot, then `umount $LFS/{sys,proc,run,dev}`, then `cd $LFS; tar -cJpf $HOME/lfs-temp-tools-….tar.xz .`. These are host commands, and the controller would run them inside the chroot context.

**Root cause:** the book drives an interactive terminal; the controller runs each step non-interactively in its task's context. These are the same kind as `su - lfs` and `chroot "$LFS"` (already skipped).

**Fix:** skip rules, applied at read time to the planned build:
- **The login shell:** every step already runs in a fresh shell (in the chroot: `env -i` with the book's environment).
- **The backup:** the checkpoint after chapter 7 snapshots both disks, which is the backup.
- **Reach:** checked against the whole book; the rules match exactly 7.6 [5], 7.15 [3]–[5] and 8.39 [5].

**Verified by:** the chapter 7 run.

### LFS-026 — 7.4's only command is the controller's own (entering the chroot), so a correct plan was refused as "nothing would run"

**Where:** task 34 (7.4 Entering the Chroot Environment); `examples/llm-chat/files/lfs_worker.py` (`run_task`).

**Symptom:** found 2026-10-08, 06:05–06:27 UTC.
- **Chapter 7 so far:** 7.2 (ownership) done, and 7.3 (virtual file systems) done on its third attempt.
- **7.4:** its one book command, the interactive `chroot "$LFS" … /bin/bash --login`, is skipped: the controller enters the chroot for every step itself. Four times, the 14B's plan was correctly "nothing to do" ("the chroot environment is already entered"). Four times it was refused: "nothing would run: a section with no commands needs added steps".
- **Rung 2:** nudged with LFS-025 (related, not the cause).
- **Rung 3 (about 2 min):** the tutor said **"needs Paul"** at once, citing the new rule (a controller fault isn't a workaround for the 14B). Its diagnosis: "The 14B was not wrong … any added step would have to copy the skipped book command (refused) or be invented busywork". It asked for exactly this fix.

**Root cause:** the controller's. The "nothing would run" check is meant for sections that have no book commands, where the model must write them (2.4). It also fired where every command is the controller's own.

**Fix:** a section whose book commands are all skipped by the controller is marked done without a plan. The worker journals each skip rule as the reason, and takes the after-chapter checkpoint if one is due.

**Verified by:** the re-run of 7.4.

**The tutor's rule from LFS-023 worked:** this is the first controller fault it escalated to Paul without first handing the 14B a workaround.

### LFS-027 — The first step in the chroot couldn't start: its script was copied into `$LFS/tmp`, which 7.5 creates; the failure left no log

**Where:** task 35 (7.5 Creating Directories, the first section run in the chroot); `examples/llm-chat/files/lfs_worker.py` (`launcher`, `Machine.run_detached`).

**Symptom:** found 2026-10-08, 06:33–07:22 UTC.
- **Every step failed fast:** each attempt at 7.5's first command ended "exit 1 after 10s". The only output was the controller's own "tail: cannot open '/var/log/lfs-build/task035-step1-tryN.log'".
- **The 14B's repairs:** four "the step's work is already done" (refused); its plans were otherwise as the book.
- **The ladder:**
  - **Rung 2:** nudged with a tutor lesson for 7.3 (LFS-T882) and LFS-015.
  - **Rung 3 (1.5 min):** the tutor said **"needs Paul"**. Its diagnosis was right: the step never ran, the controller fails before or around the book's command, and "every later chroot section will hit the same failure".
- **Also found:** `$LFS/dev/pts` mounted three times and `$LFS/proc` twice, from 7.3's own book commands run on three attempts.

**Root cause:** the controller's.
- **The copy:** the chroot launcher copied each step's script to `$LFS/tmp/lfs-step.sh`, but `$LFS/tmp` doesn't exist until 7.5, the first section run in the chroot. The copy failed.
- **The log:** the log redirect covered only the `chroot` command, not the whole launcher, so the failure wrote nothing.
- **The mounts:** 7.3's book commands aren't safe to re-run.

**Fix:**
- **`$LFS/tmp`:** the chroot launcher makes it first, with `install -d -m 1777`, the book's own mode.
- **The log:** the whole launcher's output goes to the step's log, `{ …; } > log 2>&1`. Tested: exit code and error both captured.
- **The mounts:** unmounted and mounted once each, the same set `_VFS` restores after reboots.
- **Probe:** a trivial step run through the same launcher inside the chroot gave "inside: 0 /", 226 programs, and LFS's bash 5.3.0 (`x86_64-lfs-linux-gnu`). Exit 0.

**Verified by:** the probe; the re-run of 7.5.

**Follow-up:** 7.3's mounts are safe to re-run only if the 14B adds `mountpoint -q … ||` guards (the tutor's LFS-T882 lesson says so). A skip rule could hand 7.3 to the controller's own `_VFS`, which is idempotent.

## Document History

| Version | Date | Author | Change Summary |
|---|---|---|---|
| v0.1 | 2026-10-07 | Paul Scott | Started with Phase A: LFS-001–LFS-005 (the books' published data, an upstream broken link, a first lesson about the 14B, a parsing quirk). |
| v0.2 | 2026-10-07 | Paul Scott | B4: LFS-006 (the build machine can't reach the repo through the lab fence: the controller delivers sources). |
| v0.3 | 2026-10-07 | Paul Scott | B2: LFS-007 (empty /etc/machine-id before imaging). |
| v0.4 | 2026-10-07 | Paul Scott | C1: LFS-008 (command alternatives inside notes; the extractor now keeps the real subsection). |
| v0.5 | 2026-10-07 | Paul Scott | C2: LFS-009 (the model writes only differences from the book; the controller inserts the book's exact commands). |
| v0.6 | 2026-10-07 | Paul Scott | C2: LFS-010 (the first plan ran on a busy desktop host; see F-236). |
| v0.7 | 2026-10-07 | Paul Scott | C2: LFS-011 (unreliable JSON from the 14B; output now schema-constrained; rejected replies journalled). |
| v0.8 | 2026-10-07 | Paul Scott | C2: LFS-012 (the 14B put its repair command in its explanation; repairs are now a required list of commands). |
| v0.9 | 2026-10-07 | Paul Scott | C2: LFS-013 (commandless section: sound steps rejected by an ambiguous format; the model reached into later sections). |
| v1.0 | 2026-10-07 | Paul Scott | C2 live: LFS-014 (alternatives misread; the first tutoring note fixed it). Tasks 2.2–2.7 done. |
| v1.1 | 2026-10-07 | Paul Scott | C2/C3: LFS-015 (a repair deleted the delivered sources; delivery reordered, non-destructive repairs, deletion guard). |
| v1.2 | 2026-10-07 | Paul Scott | LFS-016 (invented reasons to leave out 4.2; facts show $LFS's contents; better rejection feedback). 3.1 done with delivery in the book's order. |
| v1.3 | 2026-10-07 | Paul Scott | LFS-017 (root marked only in prose; -j32 illustrations). 4.2 and 4.3 done. |
| v1.4 | 2026-10-07 | Paul Scott | LFS-018 (cd lost between steps; duplicate build added). 4.4 done; 5.2 checkpointed. |
| v1.5 | 2026-10-07 | Paul Scott | 5.2 Binutils pass 1 built (C6 slice reached). |
| v1.6 | 2026-10-07 | Paul Scott | LFS-019 (GCC pass 1 out of memory; swap added; the ladder's first live run: Sentinel nudge, two tutor sessions). |
| v1.7 | 2026-10-07 | Paul Scott | LFS-020 (5.4 dropped the copy; grub-install on the host disk; rollback; three guards). C6's restore proven. |
| v1.8 | 2026-10-08 | Paul Scott | LFS-021 (the version note provoked needless changes; the drop check held). |
| v1.9 | 2026-10-08 | Paul Scott | LFS-022 (a remark refused as a step). |
| v2.0 | 2026-10-08 | Paul Scott | LFS-023 (no tarball for 5 sections; the tutor's workaround fought the guards). 5.5 glibc built. |
| v2.1 | 2026-10-08 | Paul Scott | LFS-024 (stale lessons; the ladder reset). |
| v2.2 | 2026-10-08 | Paul Scott | Chapters 5 and 6 built. LFS-025 (chapter 7's interactive steps, prevented). |
| v2.3 | 2026-10-08 | Paul Scott | LFS-026 (an all-skipped section). 7.2 and 7.3 done. |
| v2.4 | 2026-10-08 | Paul Scott | LFS-027 (the first chroot step: $LFS/tmp; logging). |
