#!/usr/bin/env bash
# Standalone host verification -- shell equivalent of
# `bootstrap.py --check-only`, for environments where you'd rather not
# invoke Python (e.g. a quick manual sanity check over SSH, or a Nagios/
# monitoring plugin). Exits non-zero if any check fails, printing a
# PASS/FAIL line per check so failures are easy to spot in CI logs.
set -uo pipefail

FAILED=0

check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "PASS  ${name}"
    else
        echo "FAIL  ${name}"
        FAILED=1
    fi
}

echo "== Host validation: $(hostname) =="

check "libvirtd active"        systemctl is-active --quiet libvirtd
check "openvswitch active"     systemctl is-active --quiet openvswitch
check "ovn-controller active"  systemctl is-active --quiet ovn-controller
check "cockpit active"         systemctl is-active --quiet cockpit.socket
check "virsh responsive"       virsh list --all
check "br-int exists"          ovs-vsctl br-exists br-int
check "br-ex exists"           ovs-vsctl br-exists br-ex
check "ceph reachable"         ceph health
check "IOMMU active (dmesg)"   bash -c "dmesg | grep -qiE '(DMAR|IOMMU).*enabled'"
check "vfio-pci module loaded" lsmod | grep -q vfio_pci

if [[ "${FAILED}" -eq 0 ]]; then
    echo "== All checks passed =="
else
    echo "== One or more checks FAILED =="
fi
exit "${FAILED}"
