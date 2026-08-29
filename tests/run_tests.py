#!/usr/bin/env python3
"""
CloudCore Test Harness Runner

Usage:
    python3 tests/run_tests.py                        # all tests
    python3 tests/run_tests.py --skip-vm              # skip VM + scenario tests
    python3 tests/run_tests.py --skip-scenarios       # unit suites + VM, no scenarios
    python3 tests/run_tests.py --skip-vm --skip-scenarios  # API-only, fastest

Adding a new suite:
    1. Create tests/suites/test_<feature>.py with class Test<Feature>
    2. Import and add to SUITES below

Adding a new scenario:
    1. Create tests/scenarios/scenario_<name>.py with class Scenario<Name>
    2. Import and add to SCENARIOS below
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.lib.framework import (
    API_BASE, API_TOKEN, RESULTS_DIR,
    BOLD, RED, GREEN, YELLOW, _c,
    run_suite, write_results,
)

# ---------------------------------------------------------------------------
# Suite registry
# ---------------------------------------------------------------------------
from tests.suites.test_auth           import TestAuth
from tests.suites.test_vpcs           import TestVPCs
from tests.suites.test_instances      import TestInstances
from tests.suites.test_load_balancers import TestLoadBalancers
from tests.suites.test_dns            import TestDNS
from tests.suites.test_images         import TestImages
from tests.suites.test_ssh_key        import TestSSHKey
from tests.suites.test_help           import TestHelp
from tests.suites.test_vm             import TestVM

SUITES = [
    TestAuth,
    TestVPCs,
    TestInstances,
    TestLoadBalancers,
    TestDNS,
    TestImages,
    TestSSHKey,
    TestHelp,
    TestVM,
]

# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------
from tests.scenarios.scenario_lb_with_instances  import ScenarioLBWithInstances
from tests.scenarios.scenario_inter_instance_ssh import ScenarioInterInstanceSSH
from tests.scenarios.scenario_dns_lifecycle      import ScenarioDNSLifecycle

SCENARIOS = [
    ScenarioDNSLifecycle,        # non-VM tests run first, VM tests last
    ScenarioLBWithInstances,
    ScenarioInterInstanceSSH,
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="CloudCore integration tests")
    parser.add_argument("--skip-vm",        action="store_true",
                        help="Skip all @vm_test tests (suites and scenarios)")
    parser.add_argument("--skip-scenarios", action="store_true",
                        help="Skip scenario tests entirely")
    args = parser.parse_args()

    # Verify API reachable
    try:
        r = urllib.request.Request(
            API_BASE + "/v1/vpcs", method="GET",
            headers={"Authorization": f"Bearer {API_TOKEN}"})
        urllib.request.urlopen(r)
    except Exception:
        print(_c(RED + BOLD, f"\nERROR: Cannot reach API at {API_BASE}"))
        print("Start the API:  systemctl --user start cloudcore-api\n")
        sys.exit(2)

    print(_c(BOLD, f"\nCloudCore Test Harness  —  "
                   f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
    if args.skip_vm:
        print(_c(YELLOW, "VM tests    : SKIPPED (--skip-vm)"))
    if args.skip_scenarios:
        print(_c(YELLOW, "Scenarios   : SKIPPED (--skip-scenarios)"))

    # ── Suites ────────────────────────────────────────────────────────────────
    print(_c(BOLD, "\n── Unit Suites ──────────────────────────────────────────"))
    for suite in SUITES:
        run_suite(suite, skip_vm=args.skip_vm)

    # ── Scenarios ─────────────────────────────────────────────────────────────
    if not args.skip_scenarios:
        print(_c(BOLD, "\n── Integration Scenarios ────────────────────────────────"))
        for scenario in SCENARIOS:
            run_suite(scenario, skip_vm=args.skip_vm)

    # ── Summary ───────────────────────────────────────────────────────────────
    passed, failed, skipped = write_results(args.skip_vm)
    total = passed + failed + skipped

    print(f"\n{'━'*54}")
    print(f"  {_c(GREEN,  f'PASSED  {passed:>3}')}"
          f"  {_c(RED,    f'FAILED  {failed:>3}')}"
          f"  {_c(YELLOW, f'SKIPPED {skipped:>3}')}"
          f"  of {total} tests")
    print(f"  Results → {RESULTS_DIR}/")
    print(f"{'━'*54}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
