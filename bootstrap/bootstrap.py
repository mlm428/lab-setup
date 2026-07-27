#!/usr/bin/env python3
"""
bootstrap.py -- Deliverable A: Platform Bootstrap Automation.

Turns a fresh RHEL 9 server into a ready compute node for the mission
management service: installs KVM/libvirt/OVS/OVN/Ceph-client/Cockpit,
configures OVS bridges and joins the OVN fabric, wires up the storage
backend (Ceph RBD or local qcow2 fallback), prepares GPU passthrough
(IOMMU/VFIO), and runs a final validation pass.

Designed to be run once per host (e.g. from a kickstart %post, an Ansible
task, or by hand over SSH) and to be safely re-run: every module checks
current state before mutating anything.

Usage:
    sudo ./bootstrap.py --host compute01 [--config-dir ../config] [--dry-run]
    sudo ./bootstrap.py --host compute01 --check-only
    sudo ./bootstrap.py --host compute01 --skip-gpu

Exit codes:
    0  success (or --check-only passed)
    1  a bootstrap step failed
    2  final validation failed
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from modules import cockpit, gpu, libvirt_checks, networking, packages, storage, validation
from modules.util import RunContext, log


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap a RHEL host for the mission compute cluster.")
    parser.add_argument("--host", required=True, help="Host name as it appears in hosts.yaml")
    parser.add_argument("--config-dir", default=str(Path(__file__).parent.parent / "config"), help="Directory containing hosts.yaml, cluster.yaml, storage.yaml (defaults to the repo's top-level config/, the single source of truth shared with management/)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without changing the host")
    parser.add_argument("--check-only", action="store_true", help="Only run validation checks, do not change anything")
    parser.add_argument("--skip-gpu", action="store_true", help="Skip IOMMU/VFIO/GPU discovery steps")
    parser.add_argument("--report-file", default=None, help="Write the JSON audit/validation report to this path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_dir = Path(args.config_dir)

    hosts_cfg = load_yaml(config_dir / "hosts.yaml")
    cluster_cfg = load_yaml(config_dir / "cluster.yaml")
    storage_cfg = load_yaml(config_dir / "storage.yaml")

    if args.host not in hosts_cfg["hosts"]:
        log.error("host %s not found in %s", args.host, config_dir / "hosts.yaml")
        return 1
    host_entry = hosts_cfg["hosts"][args.host]
    storage_backend = host_entry.get("storage_backend", "ceph_rbd")

    ctx = RunContext(dry_run=args.dry_run)
    log.info("bootstrap: starting for host=%s dry_run=%s storage_backend=%s", args.host, args.dry_run, storage_backend)

    if args.check_only:
        report = validation.validate_host(ctx, cluster_cfg, storage_backend)
        _write_report(args.report_file, {"audit": ctx.audit, "validation": report.as_dict()})
        return 0 if report.ok else 2

    try:
        packages.install_base_packages(ctx, cluster_cfg)
        libvirt_checks.ensure_libvirtd_running(ctx)
        libvirt_checks.ensure_default_pool(ctx)

        networking.setup_ovs_bridges(ctx, cluster_cfg)
        networking.join_ovn_fabric(ctx, hosts_cfg)

        storage.configure_storage(ctx, storage_cfg, storage_backend)

        if not args.skip_gpu and len(host_entry.get("gpu_devices", [])) > 0:
            reboot_required = gpu.ensure_iommu_kernel_args(ctx, cluster_cfg)
            gpu.load_vfio_modules(ctx, cluster_cfg)
            discovered = gpu.discover_gpus(ctx)
            log.info("bootstrap: discovered physical GPU adapters (partition into MIG/vGPU slices, then run scripts/enumerate_mdev_gpus.sh and record UUIDs in config/hosts.yaml): %s", discovered)
            if reboot_required:
                log.warning(
                    "bootstrap: *** REBOOT REQUIRED *** IOMMU kernel args were just added to "
                    "/etc/default/grub -- they will not take effect until this host reboots. "
                    "Final validation below will likely report iommu_active as not confirmed "
                    "until you reboot and re-run bootstrap.py (or --check-only) to confirm."
                )
                ctx.record("reboot_required", "ok", "IOMMU kernel args added; reboot before re-running")
        else:
            log.info("bootstrap: skipping GPU setup (skip_gpu=%s, gpu_devices=%s)", args.skip_gpu, host_entry.get("gpu_devices", []))

        cockpit.enable_cockpit(ctx)

    except Exception as exc:  # noqa: BLE001 - top-level orchestrator: report and abort
        log.error("bootstrap: FAILED: %s", exc)
        _write_report(args.report_file, {"audit": ctx.audit, "error": str(exc)})
        return 1

    report = validation.validate_host(ctx, cluster_cfg, storage_backend)
    _write_report(args.report_file, {"audit": ctx.audit, "validation": report.as_dict()})

    if report.ok:
        log.info("bootstrap: host %s is ready", args.host)
        return 0
    else:
        log.error("bootstrap: host %s failed final validation", args.host)
        return 2


def _write_report(path: str | None, data: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    log.info("bootstrap: wrote report to %s", path)


if __name__ == "__main__":
    sys.exit(main())
