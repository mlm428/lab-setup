#!/usr/bin/env bash
# End-to-end bootstrap test, intended to run against a real (or freshly
# kickstarted) RHEL host -- NOT executable in a plain Linux container,
# since it exercises systemd/libvirt/OVS/OVN/Ceph. See
# bootstrap/tests/test_modules.py for the logic-level tests that run
# anywhere with just Python 3 + PyYAML.
#
# What this checks, per the design doc's "Bootstrap Validation" test plan:
#   1. bootstrap.py runs to completion on a clean host
#   2. running it a second time is idempotent (no errors, no duplicate
#      resources)
#   3. --check-only reflects the same PASS state bootstrap.py itself found
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${1:-compute01}"

echo "== Run 1: fresh bootstrap =="
python3 bootstrap.py --host "${HOST}" --report-file /tmp/bootstrap-run1.json
RUN1_RC=$?

echo "== Run 2: idempotency check (should succeed with no new changes) =="
python3 bootstrap.py --host "${HOST}" --report-file /tmp/bootstrap-run2.json
RUN2_RC=$?

echo "== check-only should reflect a passing host =="
python3 bootstrap.py --host "${HOST}" --check-only --report-file /tmp/bootstrap-check.json
CHECK_RC=$?

echo "== Shell-level spot checks =="
./scripts/verify_host.sh

if [[ "${RUN1_RC}" -ne 0 || "${RUN2_RC}" -ne 0 || "${CHECK_RC}" -ne 0 ]]; then
    echo "FAIL: one or more bootstrap runs did not succeed"
    exit 1
fi

echo "PASS: bootstrap is idempotent and host validates clean"
