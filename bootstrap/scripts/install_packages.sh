#!/usr/bin/env bash
# Low-level, manual counterpart to modules/packages.py -- useful for a
# kickstart %post section, a break-glass SSH session, or just eyeballing
# exactly what gets installed without reading Python. bootstrap.py does NOT
# call this script; it does the equivalent via subprocess itself. Keep the
# package list here in sync with bootstrap/config/cluster.yaml if you edit
# either by hand.
set -euo pipefail

echo "==> Installing virtualization, networking, storage, and management packages"

dnf install -y \
    qemu-kvm libvirt-daemon libvirt-daemon-driver-qemu libvirt-client \
    virt-install virt-viewer python3-libvirt \
    openvswitch openvswitch-ovn-host NetworkManager-ovs \
    ceph-common python3-rados python3-rbd \
    cockpit cockpit-machines \
    openscap-scanner scap-security-guide

echo "==> Enabling services"
for svc in libvirtd openvswitch ovn-controller cockpit.socket; do
    systemctl enable --now "${svc}"
done

echo "==> Done. Verify with: virsh list --all ; ovs-vsctl show ; systemctl status cockpit"
