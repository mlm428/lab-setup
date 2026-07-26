#!/usr/bin/env bash
# Run this on an INTERNET-CONNECTED machine (matching the target hosts'
# RHEL major/minor version and architecture) to download every package
# this project needs -- plus their full dependency chains -- into a
# single directory you can transfer into your airgapped environment
# (USB drive, one-way transfer station, etc.). Then run
# setup_local_repo.sh on the airgapped side to turn that directory into a
# dnf repo bootstrap.py's package installation step can use with zero
# internet access.
#
# This does NOT need to run on a cluster host -- any RHEL/CentOS/Rocky
# machine with `dnf` and matching major version + arch works (a
# subscription-manager-registered RHEL VM, a Red Hat UBI container, etc.).
#
# Usage:
#   ./fetch_offline_packages.sh [output_dir]
#     output_dir defaults to ./offline-packages
set -euo pipefail

OUT_DIR="${1:-./offline-packages}"
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../config" && pwd)"

if ! command -v dnf >/dev/null 2>&1; then
    echo "ERROR: dnf not found. Run this on a RHEL/CentOS/Rocky machine matching your cluster hosts' version+arch." >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

echo "== Reading package list from config/cluster.yaml =="
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" 2>/dev/null; then
    PACKAGES="$(python3 - "$CONFIG_DIR/cluster.yaml" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)
pkgs = []
for group in data.get("packages", {}).values():
    pkgs.extend(group)
print(" ".join(sorted(set(pkgs))))
PYEOF
)"
else
    echo "python3-yaml not available here -- falling back to a hardcoded package list."
    echo "(Prefer running this with PyYAML installed so it always matches config/cluster.yaml exactly.)"
    PACKAGES="qemu-kvm libvirt-daemon libvirt-daemon-driver-qemu libvirt-client virt-install virt-viewer python3-libvirt openvswitch openvswitch-ovn-host NetworkManager-ovs ceph-common python3-rados python3-rbd cockpit cockpit-machines openscap-scanner scap-security-guide"
fi

echo "Packages to fetch: $PACKAGES"
echo
echo "== Downloading packages + full dependency chains =="
# --resolve pulls in dependencies; --alldeps ensures nothing is skipped
# because it looks "already installed" on THIS machine (it may not be on
# the target). destdir is a flat directory of RPMs.
dnf download --resolve --alldeps --destdir="$OUT_DIR" $PACKAGES

echo
echo "== Downloading GPG keys used to sign these packages =="
mkdir -p "$OUT_DIR/gpg-keys"
for keyfile in /etc/pki/rpm-gpg/*; do
    [ -f "$keyfile" ] && cp "$keyfile" "$OUT_DIR/gpg-keys/" 2>/dev/null || true
done

echo
echo "Done. $(ls "$OUT_DIR"/*.rpm 2>/dev/null | wc -l) RPM(s) in $OUT_DIR/"
echo "Transfer $OUT_DIR/ into your airgapped environment, then run"
echo "setup_local_repo.sh there to turn it into a usable dnf repo."
