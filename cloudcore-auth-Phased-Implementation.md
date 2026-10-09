# CloudCore: central authorization (layer 1) and human logins (layer 2) — Phased Implementation

> **Roadmap (2026-10-09):** open items are now tracked in `cloudcore-Roadmap.md` (CC-10 to CC-18). This document is kept as the record of how the work was done.

**Status:** layer 1 built on the hub 2026-10-02 (A1–A4, A6); the peer pending (A5). **Owner:** Paul Scott.

## Context

F-201 found security-group routes with no authentication, reachable from the home LAN and lab VMs, and a default master token (`dev-token`) that unlocked instance and VPC control on the network-facing peer listener. The holes are fixed, but the cause remains:
- **No central check:** each blueprint checks tokens its own way (copied `_auth()` functions, `require_auth`, per-route checks), and a route with no check is open by default.
- **Five kinds of token, each handled differently:** master, peer, examples capture, lab-VM broker, per-student client tokens. There's no common expiry, use record or revocation.
- **No audit trail** of who created or deleted what, which matters now that lab VMs create VMs.
- **No people in the model:** the dashboard is protected only by being localhost-only; llm-chat students are identified mostly by IP.

Direct request (2026-10-02): "given the security concerns … should we think about creating a login/user security facility around cloudcore". Agreed order:
1. F2 (lab network);
2. **layer 1**, before the full-VM plan's F6, since student machines need a student identity;
3. F3–F8;
4. **layer 2** when needed.

## Layer 1 — central, default-deny authorization (recommended now)

| # | Stage | Status |
|---|---|---|
| A1 | **One check for every request.** An app-wide `before_request` that refuses any route that hasn't declared its scope (a `@scope("…")` decorator or a registry), so a new route is closed until someone decides who may call it. Remove the copied per-blueprint `_auth()` functions. Keep the per-bind gates (peer and examples listeners) as a second layer. | Done (2026-10-02, F-206): `api/authz.py` gate, after the per-bind gates; 7 public routes, everything unlisted admin-only. The blueprints' own checks are kept as a second layer, not removed. |
| A2 | **One token table.** `api_tokens`: id, name, scope (`admin`, `peer`, `capture`, `labvm`, `student`), SHA-256 hash, created, expires, revoked, last used. Tokens are shown once at creation. Migrate the existing ones: the master token (as `admin`, still loadable from `api.env`), per-peer tokens, the capture and broker tokens, and the per-student client tokens (`llm_client_tokens`). Each scope maps to an explicit set of routes. | Done (F-206): `api_tokens` table (hashed, scoped admin/capture/labvm, expiry, revocation, last used). Peer and student tokens resolve through their existing stores; the api.env tokens stay valid (break-glass). |
| A3 | **Audit log.** Every state-changing request (POST, PUT, DELETE) is recorded: token id and scope, route, target id, result, time, source address. Visible in the dashboard and shipped to Loki, so Sentinel can watch it. | Done (F-206): `audit_log` table plus an `AUDIT` log line for every POST/PUT/PATCH/DELETE, with identity, route, status and source. Readable at `GET /v1/auth/audit`. Dashboard view not yet; not yet shipped to Loki (no host promtail). |
| A4 | **Token management.** Dashboard and CLI: create (shown once), list, revoke, rotate; expiry warnings. | Partly (F-206): `GET/POST /v1/auth/tokens`, `DELETE /v1/auth/tokens/<id>` (create shows the token once). Dashboard UI not yet. |
| A5 | **The peer.** Llwyn-y-Groes runs its own CloudCore; it takes the same code and its own `api.env`. This also brings it up to date with F-201. | Done (2026-10-02, two-host S1): Llwyn-y-Groes runs the same code with its own `api.env`; F-201 closed there (no token and the master token are refused on its 8082). |
| A6 | **Verify.** A test suite that walks every registered route with no token, the wrong scope and the right scope; plus the F-201 checks from the LAN and a lab VM. | Done (F-206): `tests/authz_walk.py` walks all 168 routes (701 requests); refused requests never reach route code. Plus live checks. |

## Layer 1 follow-ups (noted 2026-10-05, not started)

- **A3 dashboard view:** a page listing the audit log (`GET /v1/auth/audit`), filterable by identity, route and status.
- **A3 shipping:** send the `AUDIT` log lines to the host's Loki, so Sentinel can watch them. Each host now runs its own Loki (two-host S4), but the API's log isn't shipped there yet.
- **A4 dashboard view:** list, create (shown once), revoke and rotate named tokens, with expiry warnings.
- **Rotation of the shared guest tokens:** capture and lab-VM broker, done by hand on 2026-10-04. It could be a dashboard or CLI action that rotates both hosts together, using `import-shared-tokens.sh`.

## Layer 2 — human logins (when the dashboard or llm-chat reaches beyond localhost)

**Don't hand-write password and session handling.** Use a proven component in front of CloudCore and the llm-chat page: Authelia, or oauth2-proxy with an identity provider. Either brings logins, sessions and MFA. CloudCore trusts the identity header those components set, but only on a socket only they can reach, and maps users and groups onto layer 1's scopes and roles: admin, instructor, student.

| # | Stage | Status |
|---|---|---|
| H1 | Choose the identity component (Authelia vs oauth2-proxy + IdP) | Not started |
| H2 | Put it in front of the dashboard; CloudCore maps identities to roles | Not started |
| H3 | llm-chat students log in; student VMs, quotas and the corpus attribute to a person | Not started |

## Risks

- **Locking ourselves out:** keep the `api.env` admin token working on 127.0.0.1 throughout, as the break-glass path.
- **Peering during migration:** peers keep their tokens; migrate them first and verify both directions before removing any old check.

Methodology unchanged: build and verify live, log findings as F-NNN.
