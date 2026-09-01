# Provider Improvements — Phased Implementation Plan

## Phase 1 — Correctness (breaks real workflows)

### 1.1 — 404 on Read → RemoveResource (all resources)
**Problem:** If a resource is deleted out-of-band (e.g. via the UI or API), the next
`tofu plan` errors instead of detecting drift and offering to recreate.  
**Fix:** In every resource `Read`, detect `API error 404` from the client and call
`resp.State.RemoveResource(ctx)` instead of adding a diagnostic error.  
**Files:** `client.go` (add typed 404 detection), all 7 resource `Read` methods.

### 1.2 — Update writes plan values, not API result (vpc, instance, load_balancer)
**Problem:** All three `Update` methods call `resp.State.Set(ctx, &plan)` using the
plan struct, ignoring the `result` returned by the API. Any field the API normalises
or computes (status, dns_name, private_ip, public_ip) goes stale after an update.  
**Fix:** After a successful PUT, map `result` fields back onto `plan` before writing
state — matching the pattern already used in `Create`.  
**Files:** `vpc.go`, `instance.go`, `load_balancer.go`.

### 1.3 — DNS resources missing `id` attribute
**Problem:** `dns_zone.go` and `dns_record.go` have no `id` field in their schema or
model. The Plugin Framework expects every resource to expose `id` in state; without
it `terraform show`, `terraform state list`, and some plan operations misbehave.  
**Fix:** Add `id` as a `Computed` + `UseStateForUnknown` string attribute. For
`dns_zone` set `id = name`. For `dns_record` set `id = zone/name/type`.  
**Files:** `dns_zone.go`, `dns_record.go`.

### 1.4 — security_group Update loses CreatedAt / Name / VPCID
**Problem:** `SecurityGroupResource.Update` only writes `Status`, `IngressRules`,
`EgressRules` back to state. `CreatedAt` becomes null and `Name`/`VPCID` are taken
from plan (correct today, but fragile).  
**Fix:** Map all fields from `result` back onto `plan` after the PUT, same as Create.  
**Files:** `security_group.go`.

---

## Phase 2 — Reliability (causes silent data loss or hard errors under normal use)

### 2.1 — nfs_server Update: partial failure leaves state inconsistent
**Problem:** The share add/remove loop returns early on first error without re-reading
the server. State ends up describing neither the old nor the new configuration.  
**Fix:** Move the re-read GET to always execute (defer or restructure so it runs on
both success and error paths), then let the diagnostic decide whether to fail.  
**Files:** `nfs_server.go`.

### 2.2 — instance.go missing RequiresReplace on vpc_id
**Problem:** Changing `vpc_id` on an existing instance triggers an in-place update
rather than a destroy+recreate. The API will accept the PUT but the VM is already
bound to its original VPC.  
**Fix:** Add `stringplanmodifier.RequiresReplace()` to the `vpc_id` attribute.  
**Files:** `instance.go`.

### 2.3 — Data sources silently return first item when neither id nor name given
**Problem:** If a caller omits both `id` and `name`, the VPC/instance/LB data sources
return the first item in the list with no warning. Should be a validation error.  
**Fix:** Add an `AtLeastOneOf` validator or an explicit check that errors when both
are null/empty.  
**Files:** `datasources.go`.

---

## Phase 3 — Missing coverage (gaps vs. the Ansible collection)

### 3.1 — Missing data sources: security_group, nfs_server, dns_zone, dns_record
**Problem:** The three existing data sources cover only vpc/instance/lb. Modules and
cross-stack configurations cannot look up security groups, NFS servers, or DNS zones
by name without hardcoding IDs.  
**Fix:** Add four new data sources following the existing id-or-name lookup pattern.  
**Files:** `datasources.go` (extend), `provider.go` (register).

---

## Phase 4 — Hygiene and future-proofing

### 4.1 — client.go: hardcoded 30s timeout, no 404 type
**Problem:** Timeout is not configurable and 404 is detected by string matching in
each resource. Should be a typed sentinel so resources can switch on it cleanly.  
**Fix:** Export a `NotFoundError` type from `client.go`; use `errors.As` in resources.
Add a `WithTimeout` functional option to `client.New`.  
**Status: COMPLETE** — `NotFoundError` exported (phase 1), `WithTimeout` option added (phase 4).  
**Files:** `client.go`, all resource `Read` methods (update 404 check).

### 4.2 — Dependency updates
**Problem:** `terraform-plugin-framework v1.11.0` is ~2 versions behind; 
`golang.org/x/net v0.23.0` has known CVEs fixed in v0.33+.  
**Fix:** `go get github.com/hashicorp/terraform-plugin-framework@latest && go mod tidy`.  
**Status: COMPLETE** — framework v1.11.0 → v1.19.0, x/net v0.23.0 → v0.58.0, Go toolchain 1.22 → 1.25.0.  
**Files:** `go.mod`, `go.sum`.

---

## Implementation Order

| Phase | Item | Effort | Risk if skipped |
|-------|------|--------|-----------------|
| 1 | 1.1 — 404 → RemoveResource | Medium | Hard errors on every drift detection |
| 1 | 1.2 — Update writes API result | Low | Stale computed fields after update |
| 1 | 1.3 — DNS id attribute | Low | Broken state list / show |
| 1 | 1.4 — SG Update loses fields | Low | Null CreatedAt after update |
| 2 | 2.1 — NFS partial failure | Low | Inconsistent state on share errors |
| 2 | 2.2 — instance vpc_id replace | Trivial | Silent no-op on vpc_id change |
| 2 | 2.3 — Data source validation | Low | Silent wrong resource returned |
| 3 | 3.1 — Missing data sources | Medium | Cross-stack lookups impossible |
| 4 | 4.1 — client NotFoundError type | Low | String-match fragility |
| 4 | 4.2 — Dependency updates | Low | CVE exposure |
