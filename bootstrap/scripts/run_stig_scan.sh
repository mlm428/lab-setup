#!/usr/bin/env bash
# Runs the RHEL 9 DISA STIG OpenSCAP profile against this host and writes an
# HTML + XML results report. Maps to both source docs' "STIG and Security
# Compliance" test: "Run OpenSCAP scan on RHEL hosts and VMs... verify no
# critical/high vulnerabilities present... passing score >= 95%."
#
# Requires: openscap-scanner, scap-security-guide (installed by
# bootstrap.py / install_packages.sh).
#
# Usage: ./run_stig_scan.sh [output-dir]
set -euo pipefail

OUT_DIR="${1:-/var/log/stig-scan}"
mkdir -p "${OUT_DIR}"
TS="$(date +%Y%m%d-%H%M%S)"
DATASTREAM="$(rpm -ql scap-security-guide 2>/dev/null | grep 'ssg-rhel9-ds.xml$' | head -n1 || true)"
PROFILE="xccdf_org.ssgproject.content_profile_stig"

if [[ -z "${DATASTREAM}" ]]; then
    echo "ERROR: could not locate ssg-rhel9-ds.xml -- is scap-security-guide installed?" >&2
    exit 1
fi

echo "==> Scanning against profile ${PROFILE}"
echo "==> Datastream: ${DATASTREAM}"

oscap xccdf eval \
    --profile "${PROFILE}" \
    --results "${OUT_DIR}/results-${TS}.xml" \
    --report "${OUT_DIR}/report-${TS}.html" \
    "${DATASTREAM}" || SCAN_RC=$?

echo "==> Report written to ${OUT_DIR}/report-${TS}.html"

# oscap exits 2 when some rules fail (not a script error) -- surface a
# simple pass rate so this can be gated on in CI without parsing XML.
if [[ -f "${OUT_DIR}/results-${TS}.xml" ]]; then
    PASS=$(grep -c 'result>pass<' "${OUT_DIR}/results-${TS}.xml" || true)
    FAIL=$(grep -c 'result>fail<' "${OUT_DIR}/results-${TS}.xml" || true)
    TOTAL=$((PASS + FAIL))
    if [[ "${TOTAL}" -gt 0 ]]; then
        PCT=$(( 100 * PASS / TOTAL ))
        echo "==> ${PASS}/${TOTAL} rules passed (${PCT}%)"
        if [[ "${PCT}" -lt 95 ]]; then
            echo "==> BELOW the 95% acceptance threshold from the design doc" >&2
            exit 2
        fi
    fi
fi

exit "${SCAN_RC:-0}"
