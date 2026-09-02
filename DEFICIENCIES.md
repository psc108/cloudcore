# Codebase Deficiency Review

**Date:** 2026-09-01  
**Scope:** `provider/` — CloudCore Terraform provider (~2,800 lines of Go)  
**Reviewer:** Claude Code automated review

---

## 1. Correctness — Bugs That Break Real Workflows

### 1.1 Update methods write plan, not API result (INCOMPLETE from IMPROVEMENTS.md §1.2)

**Files:** `vpc.go:176`, `instance.go:232`, `load_balancer.go:210`

All three `Update` implementations call `resp.State.Set(ctx, &plan)` using the *plan* struct rather than the *result* returned by the API. Any field the API normalises or computes — `status`, `dns_name`, `private_ip`, `public_ip` — goes stale immediately after an update. The `Create` methods handle this correctly; `Update` must match that pattern.

### 1.2 SecurityGroup Update loses CreatedAt / Name / VPCID (INCOMPLETE from IMPROVEMENTS.md §1.4)

**File:** `security_group.go:315–331`

`SecurityGroupResource.Update` writes `Status`, `IngressRules`, and `EgressRules` from the API result but takes `Name` and `VPCID` from the plan. `CreatedAt` is preserved manually. Any future API-side normalisation of `Name` will silently diverge from state.

### 1.3 NFS Server share loop exits on first error, leaving inconsistent state (INCOMPLETE from IMPROVEMENTS.md §2.1)

**File:** `nfs_server.go:299–312`

The add-share and delete-share loops both `return` on first failure without executing the re-read GET at lines 314–329. Terraform state then reflects neither the old configuration nor the new one. The re-read must execute unconditionally — move it to a `defer` or restructure so it always runs before diagnostic evaluation.

---

## 2. Missing Test Coverage

**Severity: High — no automated validation of any provider behaviour**

There are no Go tests anywhere under `provider/`. Specifically:

- No **unit tests** for client error handling (`client.go`), struct mapping, or rule parsing.
- No **acceptance tests** using the Terraform Plugin Framework test harness (`resource.UnitTest` / `resource.Test`). These are the standard mechanism for verifying create/read/update/delete/import cycles against a real or mocked API.
- No **data source tests** for the id-or-name lookup logic in `datasources.go`.

The existing `tests/` directory contains Python/Ansible API tests — unrelated to the provider. Any refactoring or new resource addition currently has zero regression protection.

**Minimum viable fix:** Acceptance tests for VPC and Instance resources covering Create, Read, Update (check computed fields sync), Delete, and ImportState.

---

## 3. Schema Documentation Gaps

**File:** All resource and data source schemas

The majority of schema attributes have no `Description` field, or have only a token description. This matters because the Plugin Framework generates provider documentation from these descriptions.

Examples of missing/thin descriptions:
- `vpc.go`: `cidr_block`, `dns_support`, `status`, `tags` — no descriptions
- `instance.go`: `image_id`, `flavor_id`, `subnet_id`, `private_ip`, `public_ip` — no descriptions
- `security_group.go`: nested rule attributes — no descriptions
- `nfs_server.go`: `capacity_gb`, `protocol`, share attributes — no descriptions

Every attribute should have a description explaining its purpose and, for computed fields, whether it is API-assigned and always overwritten.

---

## 4. No Retry Logic or Transient Error Handling

**File:** `client.go`

The HTTP client has no retry mechanism. Any transient API failure — network timeout, 5xx response, connection reset — immediately returns an error and fails the Terraform operation. For infrastructure APIs that occasionally return 429 or 503 during load, this causes unnecessary plan/apply failures.

Add exponential backoff with jitter for `5xx` and `429` responses, capped at a configurable maximum (e.g. 3 retries, 30s total). The `WithTimeout` option already exists on the client; a `WithRetries` option would be consistent.

---

## 5. No Configurable Timeout in Provider Block

**File:** `client.go:38`, `provider.go`

The HTTP timeout is hardcoded at 30 seconds. It is overridable via `WithTimeout()` at client construction but the provider schema exposes no `timeout` attribute to HCL. Users with slow API endpoints or large payloads (e.g. NFS servers with many shares) cannot tune this without forking the provider.

Add a `timeout` attribute to the provider schema (integer, seconds, optional, default 30) and pass it through to `client.New`.

---

## 6. No TLS Enforcement on API Endpoint

**File:** `client.go:56–62`

The client accepts any URL scheme for `api_url`. If a caller passes `http://` rather than `https://`, the bearer token is transmitted in plaintext. There is no check that the scheme is `https` before making authenticated requests.

Add a validation step in `client.New` (or in the provider configuration) that rejects non-HTTPS URLs, or at minimum logs a prominent warning.

---

## 7. No Pagination Support in Data Sources

**File:** `datasources.go:102–106, 224–229, 343–350` (and all other list fetches)

All seven data sources fetch the full resource list in a single API call and scan it in memory. If the API returns paginated results with a page size limit, or if the account has thousands of resources, this silently returns a partial list and may return the wrong resource or no resource at all.

Unless the API is known to return all results in one response unconditionally, add pagination loop support (follow `next` link or increment `page` parameter) before the lookup.

---

## 8. Code Duplication — Struct Pairs and Data Source Read Pattern

**Severity: Maintainability / technical debt**

### 8.1 Dual struct pairs repeated seven times

Every resource file defines two structs: a `*ResourceModel` for Terraform state and a `*APIModel` for JSON serialisation. The mapping between them is hand-written in each file. With seven resources this is ~200 lines of repetitive field-assignment code. Any new field requires changes in four places (schema, Terraform model, API model, mapping function).

Consider shared generic mapper utilities or embedding patterns to reduce this surface.

### 8.2 Data source Read is copy-pasted

The fetch-list → filter-by-id-or-name → set-state pattern in `datasources.go` is repeated identically seven times with only struct names changing (lines 104–116, 227–239, 343–355, etc.). This is a candidate for a generic helper function once the struct duplication above is addressed.

### 8.3 Nested rule/share handling duplicated

`security_group.go` and `nfs_server.go` each implement custom `FromAPI`/`ToAPI` conversion functions for their nested objects (rules and shares respectively). The pattern is identical. A shared generic nested-object mapper would halve this code.

---

## 9. Missing Resource Features

Items not in IMPROVEMENTS.md that represent meaningful capability gaps:

| Gap | Impact |
|-----|--------|
| No rate-limit handling (HTTP 429) | Hard failure under API throttling |
| Load balancer target groups not modelled | LB resources are incomplete without backends |
| DNS record not separately importable by zone+name+type | Import requires knowing internal ID |
| No `timeouts` block support on resources | Users cannot override per-resource timeouts |
| Tags not enforced/propagated | Nothing ensures `Environment`/`Owner` tags are present |

---

## 10. Outstanding IMPROVEMENTS.md Items

For completeness, items from IMPROVEMENTS.md with current status:

| Item | Status |
|------|--------|
| 1.1 — 404 → RemoveResource | Complete ✓ |
| 1.2 — Update writes API result | **Incomplete** |
| 1.3 — DNS id attribute (dns_record) | Complete ✓ |
| 1.3 — DNS id attribute (dns_zone uses name, not computed id) | Partial |
| 1.4 — SG Update loses fields | **Incomplete** |
| 2.1 — NFS partial failure | **Incomplete** |
| 2.2 — instance vpc_id RequiresReplace | Complete ✓ |
| 2.3 — Data source validation | Complete ✓ |
| 3.1 — Missing data sources | Complete ✓ |
| 4.1 — NotFoundError type + WithTimeout | Complete ✓ |
| 4.2 — Dependency updates | Complete ✓ |

---

## Priority Order

| # | Item | Effort | Consequence if deferred |
|---|------|--------|------------------------|
| 1 | Fix Update methods (§1.1, §1.2) | Low | Stale computed fields after every update |
| 2 | Fix NFS partial failure (§1.3) | Low | Inconsistent state on share errors |
| 3 | Add acceptance tests (§2) | Medium | No regression safety as codebase grows |
| 4 | Add TLS enforcement (§6) | Trivial | Token exposure risk on misconfiguration |
| 5 | Add schema descriptions (§3) | Medium | No generated documentation |
| 6 | Add retry logic (§4) | Low | Brittle under transient API failures |
| 7 | Add configurable timeout (§5) | Trivial | Hardcoded limit affects all users |
| 8 | Add pagination (§7) | Medium | Silent wrong results on large accounts |
| 9 | Reduce code duplication (§8) | Medium | Maintenance burden grows with each resource |
| 10 | Missing resource features (§9) | High | LB and DNS coverage incomplete |
