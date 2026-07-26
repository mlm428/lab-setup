"""
Package installation and service enablement for RHEL compute hosts.

Maps to the design doc's "Install Packages" bootstrap task: install
qemu-kvm/libvirt/OVS/OVN/Ceph-client/Cockpit, then enable+start the
associated systemd services.
"""
from __future__ import annotations

import subprocess
from typing import Iterable

from .util import RunContext, run, log


def _flatten(package_groups: dict) -> list[str]:
    """Flatten config/cluster.yaml's grouped `packages:` mapping (virtualization/networking/storage/etc.) into one flat package name list."""
    pkgs: list[str] = []
    for group in package_groups.values():
        pkgs.extend(group)
    return pkgs


def installed_packages() -> set[str]:
    """Query rpm for currently installed package names (idempotency check)."""
    try:
        result = subprocess.run(
            ["rpm", "-qa", "--qf", "%{NAME}\n"], capture_output=True, text=True
        )
        return set(result.stdout.splitlines())
    except FileNotFoundError:
        # Non-RPM host (e.g. this build sandbox) -- treat as "nothing
        # installed" so callers see a clear diff rather than crashing.
        return set()


def install_base_packages(ctx: RunContext, cluster_cfg: dict) -> None:
    """
    Install every package listed in cluster.yaml's `packages` section, then
    enable+start the services listed under `services`. Idempotent: only
    installs what isn't already present, and `systemctl enable --now` is a
    no-op if the service is already enabled/running.

    In an airgapped environment, `dnf install` here works exactly as it
    does normally -- see ../scripts/fetch_offline_packages.sh and
    ../scripts/setup_local_repo.sh for getting these same packages onto a
    host with no internet access, no code changes needed here.

    Args:
        ctx: Run context (honors --dry-run).
        cluster_cfg: Parsed config/cluster.yaml (for `packages` and `services`).

    Returns:
        None.
    """
    all_pkgs = _flatten(cluster_cfg["packages"])
    already = installed_packages()
    missing = [p for p in all_pkgs if p not in already]

    if not missing:
        log.info("packages: all %d required packages already installed", len(all_pkgs))
        ctx.record("install_packages", "skipped", "all packages already present")
    else:
        log.info("packages: installing %d missing package(s): %s", len(missing), ", ".join(missing))
        run(ctx, ["dnf", "install", "-y"] + missing)
        ctx.record("install_packages", "ok", f"installed {len(missing)} package(s)")

    for service in cluster_cfg["services"]:
        run(ctx, ["systemctl", "enable", "--now", service])
        ctx.record("enable_service", "ok", service)

    log.info("packages: base package/service setup complete")
