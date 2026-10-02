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
