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
| S5 | **Broker and capture local to each host:** the coordinator asks its own host | Claude |
| S6 | **Sentinel and builds movable:** database backup to the other host, a runbook, builds from either host | Both |
| S7 | **Prove it:** build and run llm-chat on Llwyn-y-Groes with Stourport's services stopped | Both |

The full-VM switch (F5) waits for S1, S2 and S5, so the coordinator doesn't get more tied to Stourport.

Methodology unchanged: build and verify live, log findings as F-NNN, tear down after.

## Progress

| # | Done | Result |
|---|---|---|
| S1 | 2026-10-02 | Peer on current code; F-201 closed there; lab network verified (F-207) |
| S2 | 2026-10-02 | Six names, `repo`, `logs`, `grafana`, `capture`, `artifacts`, `sentinel` (`.cloudcore.internal`), replace 207 references in 69 files; each host's dnsmasq answers them from `/etc/cloudcore/services.conf` (F-208) |
| S3 | 2026-10-03 | Peer holds a checksum-verified copy of the repo (203 files, 212.5 GB) via `api/sync-package-repo.py`; `repo_sync` jobs keep it in step daily; peer guests use their own host's repo and NFS export |

## Service names

Guests reach host-level services by name. Each host's dnsmasq answers each name with an address from `/etc/cloudcore/services.conf`, one `name=self` or `name=<IPv4>` line per service. `self` is the host's own bridge gateway, and a missing line or file means `self`. After editing the file, re-run `sudo bash api/setup-network.sh <octet>`, which restarts only dnsmasq. Never restart `cloudcore-bridge.service`: its stop step deletes the bridge.

| Name | Service |
|---|---|
| `repo` | package repo and artifact cache (:8090) |
| `artifacts` | read-only NFS export of the artifact cache |
| `logs` | Loki (:3100) |
| `grafana` | Grafana (:3000) |
| `capture` | examples capture and lab-VM broker (:8083) |
| `sentinel` | Sentinel (:8900) |

Create the file readable by everyone. On a host whose root has a strict umask, a plain `sudo tee` leaves it root-only:

```bash
sudo install -d -m 755 /etc/cloudcore
printf 'repo=self\nartifacts=self\nlogs=192.168.100.1\n' | sudo install -m 644 /dev/stdin /etc/cloudcore/services.conf
```

The peer copies the repo from another host with `python3 api/sync-package-repo.py pull --from http://<host>:8090`; the host it copies from needs a fresh `sync-package-repo.py checksums`. Both run daily as `repo_sync` scheduler jobs, `index` mode on the host being copied and `pull` mode on the peer.
