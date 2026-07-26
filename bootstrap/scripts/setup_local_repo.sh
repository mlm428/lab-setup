#!/usr/bin/env bash
# Run this in your AIRGAPPED environment (on the rescue/setup node, or on
# each target host individually -- see this script's --serve option to
# decide which) to turn a directory of RPMs fetched by
# fetch_offline_packages.sh into a dnf repo bootstrap.py's packages.py
# module can install from with zero internet access.
#
# Two supported topologies:
#   1. --serve: build the repo on the rescue node and serve it over HTTP
#      on the management network -- target hosts point their dnf config
#      at http://<rescue-node>:8080/ (see the printed .repo snippet at
#      the end). Preferred when hosts share a network with the rescue
#      node, since you only need to transfer the RPMs once.
#   2. (default, no --serve): build a local, host-side repo directly from
#      an RPM directory already present on THIS machine (e.g. copied over
#      by rescue/orchestrate.py alongside bootstrap/ + config/). Use this
#      when a host has no route to the rescue node's HTTP server.
#
# Usage:
#   ./setup_local_repo.sh /path/to/offline-packages [--serve [port]]
set -euo pipefail

RPM_DIR="${1:?Usage: $0 /path/to/offline-packages [--serve [port]]}"
SERVE=false
PORT=8080
if [[ "${2:-}" == "--serve" ]]; then
    SERVE=true
    PORT="${3:-8080}"
fi

if ! command -v createrepo_c >/dev/null 2>&1; then
    echo "createrepo_c not found. If it's not already on this offline machine, it"
    echo "must itself be fetched via fetch_offline_packages.sh (add 'createrepo_c'"
    echo "to the temporary package list on the connected machine, or dnf download"
    echo "it directly) and installed with 'rpm -ivh' before this script can run."
    exit 1
fi

echo "== Importing GPG keys =="
if [[ -d "$RPM_DIR/gpg-keys" ]]; then
    for key in "$RPM_DIR"/gpg-keys/*; do
        [ -f "$key" ] && rpm --import "$key" 2>/dev/null || true
    done
fi

echo "== Building repo metadata in $RPM_DIR =="
createrepo_c "$RPM_DIR"

REPO_FILE=/etc/yum.repos.d/mission-cluster-offline.repo

if [[ "$SERVE" == true ]]; then
    echo "== Serving $RPM_DIR over HTTP on port $PORT =="
    echo "(runs in the foreground -- Ctrl+C to stop, or run this under systemd-run/tmux for a persistent server)"
    THIS_HOST="$(hostname -f 2>/dev/null || hostname)"
    cat <<EOF

On EACH target host, create $REPO_FILE with:

    [mission-cluster-offline]
    name=Mission Cluster Offline Repo
    baseurl=http://${THIS_HOST}:${PORT}/
    enabled=1
    gpgcheck=1

Then run bootstrap.py normally -- packages.py's dnf install will use this
repo with no internet access required.
EOF
    cd "$RPM_DIR" && python3 -m http.server "$PORT"
else
    echo "== Writing local repo file: $REPO_FILE =="
    cat > "$REPO_FILE" <<EOF
[mission-cluster-offline]
name=Mission Cluster Offline Repo
baseurl=file://$(cd "$RPM_DIR" && pwd)
enabled=1
gpgcheck=1
EOF
    echo "Done. dnf on this host will now use $RPM_DIR with no internet access."
    echo "Run bootstrap.py normally -- packages.py's dnf install requires no code changes for this."
fi
