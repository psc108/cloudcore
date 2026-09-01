# CloudCore Provider — Implementation Plan

Merged from `DEFICIENCIES.md` (automated review) and provider gap analysis
(AWS provider parity review). Items already completed are noted but not
re-listed as work items.

## Already Complete (not re-planned)

| Item | Source |
|------|--------|
| 404 → RemoveResource on all reads | IMPROVEMENTS §1.1 |
| Update writes API result (vpc, instance, lb, sg) | IMPROVEMENTS §1.2 / §1.4 |
| DNS `id` attribute | IMPROVEMENTS §1.3 |
| NFS partial failure — re-read always runs | IMPROVEMENTS §2.1 |
| `instance.vpc_id` RequiresReplace | IMPROVEMENTS §2.2 |
| Data source validation (both null) | IMPROVEMENTS §2.3 |
| All 7 data sources present | IMPROVEMENTS §3.1 |
| `NotFoundError` type, `WithTimeout` option | IMPROVEMENTS §4.1 |
| Dependency upgrades (framework v1.19, x/net, grpc) | IMPROVEMENTS §4.2 |

---

## Phase 1 — Correctness and Safety ✓ COMPLETE

These are bugs or gaps that cause silent wrong behaviour or security risk
under normal use. Low-to-trivial effort, high consequence if deferred.

### 1.1 — `nfs_server.name` must force replacement

**Source:** Gap analysis  
**Problem:** `name` is baked into the NFS VM's cloud-init hostname at boot.
A PUT that renames it in the API leaves the running VM with the wrong
hostname. The provider silently accepts the change.  
**Fix:** Add `stringplanmodifier.RequiresReplace()` to the `name` attribute
in `nfs_server.go`.  
**File:** `provider/internal/resources/nfs_server.go`

### 1.2 — TLS enforcement on `api_url`

**Source:** DEFICIENCIES §6  
**Problem:** If a caller passes `http://` the bearer token is sent in
plaintext. No warning or error is produced.  
**Fix:** In `provider.go` Configure, check the scheme of `apiURL`. If it is
not `https` and not `127.0.0.1`/`localhost` (dev exemption), add a
`resp.Diagnostics.AddWarning` so the user is explicitly informed.  
**File:** `provider/internal/provider/provider.go`

### 1.3 — Input validators on known-enum fields

**Source:** Gap analysis  
**Problem:** `flavor`, load balancer `type`, and security group rule
`protocol` accept any string and only fail at apply time with an opaque API
error. AWS provider validates these at plan time.  
**Fix:** Add `stringvalidator.OneOf(...)` validators to:
- `instance.flavor` — `standard.nano`, `standard.small`, `standard.medium`, `standard.large`
- `nfs_server.flavor` — same set
- `load_balancer.type` — `network`, `application`
- `security_group` rule `protocol` — `tcp`, `udp`, `icmp`, `-1`  

**Files:** `instance.go`, `nfs_server.go`, `load_balancer.go`, `security_group.go`

---

## Phase 2 — UX Parity with AWS Provider ✓ COMPLETE

These are the features that make the provider feel like a first-class
infrastructure tool rather than a thin API wrapper.

### 2.1 — `timeouts` block + create polling on instance and NFS server

**Source:** DEFICIENCIES §9 (timeouts), Gap analysis (polling)  
**Problem:** Both `cloudcore_instance` and `cloudcore_nfs_server` return
`202 Accepted` from the API. The provider immediately writes
`status = "pending"` to state and returns. Users cannot chain resources that
depend on a running instance without external polling (the Ansible examples
use `until:` loops for this). The AWS provider polls EC2 until
`running` within a configurable timeout.  
**Fix:**
- Import `github.com/hashicorp/terraform-plugin-framework-timeouts/resource/timeouts`
- Add a `timeouts` block attribute to both resource schemas (create default
  `10m`, delete default `5m`)
- After the POST in Create, poll `GET /v1/instances/{id}` (or
  `/v1/nfs-servers/{id}`) every 10s until `status == "running"` or the
  timeout expires, using the context deadline
- On timeout, add a diagnostic error with the last known status  

**Files:** `instance.go`, `nfs_server.go`, `go.mod` (new dependency)

### 2.2 — `ssh_port` and `ssh_user` as computed outputs on instance

**Source:** Gap analysis  
**Problem:** The API returns `ssh_port` and `ssh_user` in every instance
response but the provider model drops them. These are useful for
cross-resource chaining — e.g. building an Ansible inventory from provider
outputs, or passing the port to a `null_resource` provisioner.  
**Fix:** Add `ssh_port` (Int64, Computed) and `ssh_user` (String, Computed)
to the instance schema and model. Map them from the API result in Create,
Read, Update, and ImportState.  
**File:** `provider/internal/resources/instance.go`

### 2.3 — Configurable `request_timeout` in provider block

**Source:** DEFICIENCIES §5  
**Problem:** The 30s HTTP timeout is not exposed to HCL. Users with slow
endpoints or large NFS payloads cannot tune it.  
**Fix:** Add an optional `request_timeout` integer attribute (seconds,
default 30) to the provider schema. Pass it to `client.New` via
`client.WithTimeout`.  
**Files:** `provider/internal/provider/provider.go`

---

## Phase 3 — Robustness ✓ COMPLETE

### 3.1 — Retry logic with exponential backoff in client

**Source:** DEFICIENCIES §4  
**Problem:** Any transient 5xx or 429 response immediately fails the
Terraform operation. Infrastructure APIs routinely return these under load.  
**Fix:** In `client.go` `do()`, add a retry loop for `429` and `5xx`
responses: up to 3 attempts, exponential backoff starting at 1s with jitter,
respecting the context deadline. Add a `WithRetries(n int)` option consistent
with `WithTimeout`.  
**File:** `provider/internal/client/client.go`

### 3.2 — Pagination support in data source list fetches

**Source:** DEFICIENCIES §7  
**Problem:** All data source Read methods fetch the full list in one call.
If the API ever returns paginated results, the lookup silently operates on a
partial list and may return the wrong resource.  
**Fix:** Check the API list response for a `next` or `page`/`total_pages`
field. If present, follow pages until exhausted before filtering. If the API
guarantees single-page responses, document that explicitly in a comment so
the assumption is visible.  
**File:** `provider/internal/datasources/datasources.go`

---

## Phase 4 — Schema Polish (documentation and discoverability)

### 4.1 — Descriptions on all schema attributes

**Source:** DEFICIENCIES §3  
**Problem:** Most attributes have no `Description`. The Plugin Framework
generates provider documentation from these. `tofu providers schema -json`
output is also used by IDE plugins and tooling.  
**Fix:** Add a `Description` string to every attribute in every resource and
data source schema. Computed fields should note that they are API-assigned.
Fields with `RequiresReplace` should say so in plain English.  
**Files:** All resource and data source files

### 4.2 — Schema-level `MarkdownDescription` on resources

**Source:** Gap analysis  
**Problem:** The `schema.Schema` struct has a `MarkdownDescription` field
that renders in the Terraform Registry and IDE hover docs. None of our
resources set it.  
**Fix:** Add a one-paragraph `MarkdownDescription` to each resource and data
source schema explaining what it manages and linking to the relevant API
path.  
**Files:** All resource and data source files

---

## Phase 5 — Acceptance Tests (regression safety)

### 5.1 — Acceptance tests for core resources

**Source:** DEFICIENCIES §2  
**Problem:** There are no Go tests anywhere under `provider/`. Any
refactoring or new resource has zero regression protection.  
**Fix:** Add acceptance tests using the Plugin Framework test harness
(`resource.Test`). Minimum coverage:
- `cloudcore_vpc`: Create, Read, Update (tags), Delete, ImportState
- `cloudcore_instance`: Create (verify `status = "running"` after polling),
  Delete, ImportState
- `cloudcore_security_group`: Create with rules, Update rules, Delete
- Data sources: vpc and instance lookup by name and by id

Tests should run against a live API (`CLOUDCORE_API_URL` / `CLOUDCORE_API_TOKEN`
env vars) and be skipped when those are absent, matching the standard
Terraform provider test pattern.  
**Files:** `provider/internal/resources/*_test.go`,
`provider/internal/datasources/datasources_test.go`

---

## Phase 6 — Code Quality (technical debt)

### 6.1 — Reduce struct/mapping duplication

**Source:** DEFICIENCIES §8.1, §8.2, §8.3  
**Problem:** Every resource defines two parallel structs (`*ResourceModel`
and `*APIModel`) with hand-written field mapping repeated in Create, Read,
Update, and ImportState. Seven resources × four methods = ~200 lines of
repetitive assignment. Any new field requires changes in four places.  
**Fix:** Evaluate a shared `mapToState` / `mapFromPlan` pattern or embedding.
The data source Read pattern (fetch → filter → set) is also copy-pasted seven
times and is a candidate for a generic helper once struct handling is
rationalised.  
**Note:** Do this after acceptance tests exist so refactoring has a safety net.  
**Files:** All resource files, `datasources.go`

---

## Implementation Order Summary

| Phase | Item | Effort | Risk if deferred |
|-------|------|--------|-----------------|
| 1 | 1.1 `nfs_server.name` RequiresReplace | Trivial | Silent hostname mismatch |
| 1 | 1.2 TLS warning | Trivial | Token exposure on misconfiguration |
| 1 | 1.3 Enum validators | Low | Opaque apply-time errors |
| 2 | 2.1 Timeouts + polling | Medium | `status = "pending"` forever; no chaining |
| 2 | 2.2 `ssh_port`/`ssh_user` outputs | Low | Cross-resource chaining impossible |
| 2 | 2.3 Configurable request timeout | Trivial | Hardcoded limit affects all users |
| 3 | 3.1 Retry logic | Low | Brittle under transient API failures |
| 3 | 3.2 Pagination | Low | Silent wrong results on large accounts |
| 4 | 4.1 Attribute descriptions | Medium | No generated documentation |
| 4 | 4.2 Resource MarkdownDescription | Low | Poor IDE/registry experience |
| 5 | 5.1 Acceptance tests | Medium | No regression safety |
| 6 | 6.1 Struct/mapping deduplication | Medium | Maintenance burden grows |
