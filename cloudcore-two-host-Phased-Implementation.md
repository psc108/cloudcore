# CloudCore on two equal hosts — Phased Implementation

**Status:** planned 2026-10-02. **Owner:** Paul Scott.
**Live tracker:** https://claude.ai/artifact/5QPbFhinqaALvyUgca2ZMp (private). It shows each stage's status, checklist, results and next step, updated as the work goes.

## Context

Direct request (2026-10-02): "I'm worried we're making stourport too important in this. llwyn-y-groes ought to be able to run all of this as well."

CloudCore's code is the same on both hosts, but the services guests depend on point at Stourport's bridge address, which is hard-coded **209 times in 53 template and script files**:

| Reference | Times |
|---|---|
| `192.168.100.1:8090` (packages, artifacts) | 155 |
| `192.168.100.1:3100` (Loki) | 35 |
| `192.168.100.1:8083` (capture, lab-VM broker) | 6 |
| `192.168.100.1:3000` (Grafana) | 2 |
| `192.168.100.1` (other services, e.g. the NFS artifact export) | 11 |

A guest on Llwyn-y-Groes, the llm-chat coordinator included, reaches across the WireGuard link to Stourport for all of it. If Stourport is down, nothing can be built anywhere, and running guests lose packages, logging, capture and the lab broker.

## Aim

Every host runs the full set of services, and guests use their own host's copy. Guests already get DNS from their own host's dnsmasq, so templates use a name (`services.cloudcore.internal`) that each host answers with its own bridge address. Placement needs no plumbing.

## Stages

| # | Stage | Who |
|---|---|---|
| S1 | **Bring Llwyn-y-Groes up to date:** F-201, layer 1 auth, `api.env`, lab network (`peer-update-checklist.md`) | You, then Claude verifies |
| S2 | **One service name instead of Stourport's address:** each host's DNS answers it with its own gateway; the 209 references move to the name | Claude |
| S3 | **Packages and artifacts on both hosts:** repo service on the peer, 215 GB artifact store copied and checksum-verified, kept in step | Both |
| S4 | **Logging on both hosts:** Loki per host; Grafana and Sentinel read both | Both |
| S5 | **Broker local to each host:** the coordinator asks its own host's lab-VM broker (`broker`); capture stays on one home host because it's a record (S6 makes it movable) | Claude |
| S6 | **Capture, Sentinel and build state movable:** nightly backups to the other host, verified restores, a runbook | Both |
| S7 | **Prove it:** build and run llm-chat on Llwyn-y-Groes with Stourport's services stopped | Both |

The full-VM switch (F5) waits for S1, S2 and S5, so the coordinator doesn't get more tied to Stourport.

Methodology unchanged: build and verify live, log findings as F-NNN, tear down after.

## Progress

| # | Done | Result |
|---|---|---|
| S1 | 2026-10-02 | Peer on current code; F-201 closed there; lab network verified (F-207) |
| S2 | 2026-10-02 | Six names, `repo`, `logs`, `grafana`, `capture`, `artifacts`, `sentinel` (`.cloudcore.internal`), replace 207 references in 69 files; each host's dnsmasq answers them from `/etc/cloudcore/services.conf` (F-208) |
| S3 | 2026-10-03 | Peer holds a checksum-verified copy of the repo (203 files, 212.5 GB) via `api/sync-package-repo.py`; `repo_sync` jobs keep it in step daily; peer guests use their own host's repo and NFS export |
| S4 | 2026-10-03 | Loki and Grafana on both hosts; guests ship to their own host's Loki (`logs`); each Grafana has a datasource per peer Loki (`api/setup-loki-datasources.sh`); Sentinel reads every host's Loki, found from CloudCore's peer list (F-210) |
| S5 | 2026-10-03 | Guests use their own host's lab-VM broker (`broker`); hosts share the broker token (`api/import-labvm-token.sh`). With Stourport's API stopped, a proof target booted through Llwyn-y-Groes's broker in 36 s and was deleted. Capture stays on one home host |

## Service names

Guests reach host-level services by name. Each host's dnsmasq answers each name with an address from `/etc/cloudcore/services.conf`, one `name=self` or `name=<IPv4>` line per service. `self` is the host's own bridge gateway, and a missing line or file means `self`. After editing the file, re-run `sudo bash api/setup-network.sh <octet>`, which restarts only dnsmasq. Never restart `cloudcore-bridge.service`: its stop step deletes the bridge.

| Name | Service |
|---|---|
| `repo` | package repo and artifact cache (:8090) |
| `artifacts` | read-only NFS export of the artifact cache |
| `logs` | Loki (:3100) |
| `grafana` | Grafana (:3000) |
| `capture` | examples, LLM deployment registration and student submissions (:8083). One home host for both: it's a record, kept in one database |
| `broker` | lab-VM broker (:8083), always the guest's own host. Every host's broker accepts the same lab-VM token (`api/import-labvm-token.sh`) |
| `sentinel` | Sentinel (:8900) |

Create the file readable by everyone. On a host whose root has a strict umask, a plain `sudo tee` leaves it root-only:

```bash
sudo install -d -m 755 /etc/cloudcore
printf 'repo=self\nartifacts=self\nlogs=192.168.100.1\n' | sudo install -m 644 /dev/stdin /etc/cloudcore/services.conf
```

The peer copies the repo from another host with `python3 api/sync-package-repo.py pull --from http://<host>:8090`; the host it copies from needs a fresh `sync-package-repo.py checksums`. Both run daily as `repo_sync` scheduler jobs, `index` mode on the host being copied and `pull` mode on the peer.

## Backups and moving a service (S6)

Each host sends a nightly backup to the other: `host_backup` scheduler job, `api/backup-host.py`.

**What's in a backup:**
- **The CloudCore database.** It holds the capture record: examples, LLM deployments, student tokens and submissions.
- **Every example's OpenTofu state.**
- **Sentinel's database and models,** if Sentinel runs on that host.

**How it's made and kept:**
- **Consistent copies:** databases are copied with SQLite's online backup and integrity-checked. `MANIFEST.json` lists every file's sha256.
- **Retention:** each host keeps 7 days, and unchanged files are hard-linked.
- **Where:** they land in `~/cloudcore-backups/<sending host>/<date>/` on the other host.
- **Confinement:** the sending host's key can only write there. Its `authorized_keys` entry runs it through `rrsync`, with `restrict`.

**Set up, once per direction,** on the host being backed up:

```bash
bash api/setup-backup-key.sh | ssh <user>@<other host> bash     # asks for the password once
python3 api/backup-host.py --to <user>@<other host>              # first backup, by hand
```

Then add a "Back up this host to another" schedule in the dashboard (daily).

### Run Sentinel on the other host

For example, if Stourport is lost. On the surviving host:

1. **Check the backup:** `python3 api/restore-from-backup.py list`, then `... verify <host>`.
2. **Install Sentinel** if it isn't installed: clone the sentinel repo next to CloudCore's and run its `install.sh`.
3. **Restore its data** with Sentinel stopped: `python3 api/restore-from-backup.py sentinel <host>`. Add `--force` if a database is already there; the old one is kept as `sentinel.db.before-restore`.
4. **Start Sentinel.** It finds every host's Loki from this host's peer list (S4). On each host, point `sentinel=` in `/etc/cloudcore/services.conf` at this host, then re-run `setup-network.sh`.

### Make another host capture's home

1. **Merge the record in:** `python3 api/restore-from-backup.py capture <host> --dry-run`, then without `--dry-run`. Rows have UUID keys, so this host's own rows are untouched. Student tokens come across as hashes, so students keep theirs.
2. **Point the name at it:** on every host, set `capture=` in `services.conf` to the new home (`self` there), then re-run `setup-network.sh`. The capture token must also be the same on both hosts (as for the broker in S5) before guests built elsewhere can post.

### Build state

`tfstate/<template>.tfstate` in a backup is the state of a build made on that host. To manage that build from the other host, copy the file into `examples/<template>/terraform.tfstate` there. Its resources must be reachable from that host's API (for example, peer-placed instances).

