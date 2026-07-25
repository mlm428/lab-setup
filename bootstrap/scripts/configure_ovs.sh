#!/usr/bin/env bash
# Manual/reference counterpart to modules/networking.py. Creates the OVS
# integration + external bridges and joins the host to the OVN fabric.
#
# Usage: ./configure_ovs.sh <ovn-southbound-address:port> [external-uplink-nic]
# Example: ./configure_ovs.sh compute01.cluster.local:6642 eth1
set -euo pipefail

SB_ADDR="${1:?usage: $0 <ovn-sb-host:port> [uplink-nic]}"
UPLINK_NIC="${2:-}"

echo "==> Creating OVS bridges (br-int, br-ex)"
for br in br-int br-ex; do
    if ovs-vsctl br-exists "${br}"; then
        echo "    ${br} already exists, skipping"
    else
        ovs-vsctl add-br "${br}"
        echo "    created ${br}"
    fi
done

if [[ -n "${UPLINK_NIC}" ]]; then
    echo "==> Attaching uplink NIC ${UPLINK_NIC} to br-ex"
    nmcli connection add type ovs-port conn-name "ovs-port-${UPLINK_NIC}" \
        ifname "${UPLINK_NIC}" master br-ex || echo "    (already attached?)"
fi

echo "==> Joining OVN fabric via southbound DB at tcp:${SB_ADDR}"
ovs-vsctl set open . external-ids:ovn-remote="tcp:${SB_ADDR}"
ovs-vsctl set open . external-ids:ovn-encap-type=geneve
systemctl enable --now ovn-controller

echo "==> Done. Verify with: ovs-vsctl show ; ovn-sbctl show (from a controller node)"
