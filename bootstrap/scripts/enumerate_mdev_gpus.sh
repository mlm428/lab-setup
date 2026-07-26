#!/usr/bin/env bash
# Enumerates mediated devices (mdevs) already created on this host --
# i.e. NVIDIA H100 MIG instances or NVIDIA L4 vGPU instances -- so their
# UUIDs and profile/type names can be copied into config/hosts.yaml's
# `gpu_devices:` list.
#
# IMPORTANT: this script only DISCOVERS mdevs that already exist. Creating
# them is a manual, driver-specific, one-time host step this automation
# does not perform:
#   - H100 MIG:  `nvidia-smi mig -i <gpu> -cgi <profile> -C` (repeat per slice)
#   - L4 vGPU:   configured via the NVIDIA vGPU Manager / license server,
#                per NVIDIA's vGPU deployment guide for your driver branch
#
# Usage: ./enumerate_mdev_gpus.sh
set -uo pipefail

echo "== Physical GPUs (for reference, to know what to partition) =="
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,uuid,pci.bus_id --format=csv,noheader
else
    echo "nvidia-smi not found -- is the NVIDIA driver installed? (see config/cluster.yaml's packages.gpu)"
fi

echo
echo "== Existing mediated devices (already-created MIG/vGPU slices) =="
if [[ -d /sys/bus/mdev/devices ]] && [[ -n "$(ls -A /sys/bus/mdev/devices 2>/dev/null)" ]]; then
    printf "%-38s %-24s %s\n" "MDEV_UUID" "TYPE" "PARENT_DEVICE"
    for dev in /sys/bus/mdev/devices/*/; do
        uuid="$(basename "$dev")"
        mdev_type="$(cat "${dev}mdev_type/name" 2>/dev/null || basename "$(readlink -f "${dev}mdev_type" 2>/dev/null)" 2>/dev/null || echo unknown)"
        parent="$(basename "$(readlink -f "${dev}../" 2>/dev/null)" 2>/dev/null || echo unknown)"
        printf "%-38s %-24s %s\n" "$uuid" "$mdev_type" "$parent"
    done
    echo
    echo "Copy each UUID + a matching 'profile:' name into this host's entry"
    echo "under config/hosts.yaml's gpu_devices: list. The 'profile:' name in"
    echo "config/hosts.yaml is a value YOU choose (e.g. 'H100-MIG-3g.40gb' or"
    echo "'L4-vGPU-4Q') -- pick something that matches what mission definitions"
    echo "will request under a VM's gpu: profile: field, and stay consistent"
    echo "with it across hosts offering the same slice type."
else
    echo "No mediated devices found. Create MIG/vGPU slices first (see this"
    echo "script's header comment), then re-run."
fi
