#!/usr/bin/env bash
# Run apply → validate → destroy for each example directory.
# Requires CLOUDCORE_API_URL and CLOUDCORE_API_TOKEN to be exported.
set -euo pipefail

EXAMPLES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../examples" && pwd)"
API_URL="${CLOUDCORE_API_URL:-}"
API_TOKEN="${CLOUDCORE_API_TOKEN:-}"

# Unique 6-char suffix for this run — prevents 409 conflicts from prior orphans.
RUN_ID="t$(date +%s | tail -c 6)"
TOFU_VARS=(-var "suffix=${RUN_ID}")

PASS=()
FAIL=()

# Delete all API resources whose name matches a pattern, across a given endpoint.
# Usage: api_purge_by_name <endpoint> <name_contains>
api_purge_by_name() {
  local endpoint="$1"
  local pattern="$2"
  if [[ -z "$API_URL" || -z "$API_TOKEN" ]]; then return; fi
  local ids
  ids=$(curl -sf "${API_URL}${endpoint}" -H "Authorization: Bearer ${API_TOKEN}" \
        | python3 -c "
import json,sys
for r in json.load(sys.stdin):
  if '${pattern}' in r.get('name',''):
    print(r['id'])
" 2>/dev/null || true)
  for id in $ids; do
    echo "  Deleting orphan ${endpoint}/${id} (name matches '${pattern}')"
    curl -sf -X DELETE "${API_URL}${endpoint}/${id}" -H "Authorization: Bearer ${API_TOKEN}" || true
  done
}

# Destroy any partial state left in an example directory, then purge API orphans.
cleanup_example() {
  local name="$1"
  local dir="$EXAMPLES_DIR/$name"
  local state="$dir/terraform.tfstate"
  local has_resources=false

  if [[ -f "$state" ]]; then
    python3 -c "
import json,sys
d=json.load(open('$state'))
sys.exit(0 if d.get('resources') else 1)
" 2>/dev/null && has_resources=true || true
  fi

  if [[ "$has_resources" == "true" ]]; then
    echo "Partial state in $name — running destroy..."
    cd "$dir"
    tofu init -no-color 2>&1
    if ! tofu destroy -no-color -auto-approve 2>&1; then
      echo "ERROR: destroy of partial $name state failed — clean up manually before retrying." >&2
      return 1
    fi
    echo "Partial state in $name cleared."
  fi

  # Belt-and-braces: remove any API resources that carry this run's suffix.
  # This catches resources that were created but not tracked in state.
  local prefix="example-dev-${RUN_ID}"
  api_purge_by_name /v1/security-groups  "${prefix}"
  api_purge_by_name /v1/load-balancers   "${prefix}"
  api_purge_by_name /v1/instances        "${prefix}"
  api_purge_by_name /v1/vpcs             "${prefix}"
  api_purge_by_name /v1/nfs-servers      "${prefix}"
  api_purge_by_name /v1/dns/zones        "${prefix}"
}

run_example() {
  local name="$1"
  local dir="$EXAMPLES_DIR/$name"

  echo ""
  echo "══════════════════════════════════════════════════"
  echo "  EXAMPLE: $name"
  echo "══════════════════════════════════════════════════"

  cd "$dir"

  echo "--- init ---"
  tofu init -no-color 2>&1

  echo "--- plan (suffix=${RUN_ID}) ---"
  tofu plan -no-color "${TOFU_VARS[@]}" -out=tfplan 2>&1

  echo "--- apply ---"
  if tofu apply -no-color -auto-approve tfplan 2>&1; then
    echo "--- apply SUCCEEDED ---"
    echo "--- destroy ---"
    if tofu destroy -no-color -auto-approve "${TOFU_VARS[@]}" 2>&1; then
      echo "--- destroy SUCCEEDED ---"
      PASS+=("$name")
    else
      echo "--- destroy FAILED ---" >&2
      FAIL+=("$name (destroy failed)")
      # Purge orphans so subsequent examples aren't blocked
      cleanup_example "$name" || true
    fi
  else
    echo "--- apply FAILED ---" >&2
    FAIL+=("$name (apply failed)")
    cleanup_example "$name" || true
  fi

  rm -f tfplan
}

# ── Pre-run cleanup: destroy any state from previous partial runs ────────────
# (Does not purge by RUN_ID since this is a fresh run with a new suffix.)
echo "=== Pre-run cleanup (state only) ==="
for ex in vpc-only compute-basic load-balanced-web network-lb full-stack dns-with-compute nfs-shared-storage; do
  state="$EXAMPLES_DIR/$ex/terraform.tfstate"
  if [[ -f "$state" ]] && python3 -c "
import json,sys
d=json.load(open('$state'))
sys.exit(0 if d.get('resources') else 1)
" 2>/dev/null; then
    echo "Partial state in $ex — running destroy..."
    cd "$EXAMPLES_DIR/$ex"
    tofu init -no-color 2>&1
    if ! tofu destroy -no-color -auto-approve 2>&1; then
      echo "ERROR: could not destroy partial state in '$ex'. Clean up manually then re-run." >&2
      exit 1
    fi
    echo "  cleared."
  fi
done
echo ""
echo "Run ID: ${RUN_ID}  (all resources will be named *-${RUN_ID})"

# ── Run examples in dependency order ────────────────────────────────────────
EXAMPLES=(
  vpc-only
  compute-basic
  load-balanced-web
  network-lb
  full-stack
  dns-with-compute
  nfs-shared-storage
)

for ex in "${EXAMPLES[@]}"; do
  run_example "$ex" || true   # don't abort the loop on failure
done

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo "  RESULTS"
echo "══════════════════════════════════════════════════"
for p in "${PASS[@]:-}"; do [[ -n "$p" ]] && echo "  PASS  $p"; done
for f in "${FAIL[@]:-}"; do [[ -n "$f" ]] && echo "  FAIL  $f"; done
echo ""
[[ ${#FAIL[@]} -eq 0 ]] && echo "All examples passed." || echo "${#FAIL[@]} example(s) failed."
