"""
Final host validation: run every "did this actually work" check the design
doc calls for, and return a structured pass/fail report so bootstrap.py can
abort with a clear error (per the doc: "Any failure should abort with a
clear error").
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import RunContext, run, service_is_active, log
from . import gpu as gpu_mod


@dataclass
class CheckResult:
    """One named pass/fail check result.

    Attributes:
        name: Short machine-readable check name (e.g. "libvirtd_active").
        passed: Whether the check succeeded.
        detail: Free-form context -- an error message on failure, or
            supporting info (bridge name, Ceph health string, etc.) on success.
    """
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationReport:
    """Aggregate result of validate_host: every individual CheckResult, plus an overall pass/fail."""
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only if every check in this report passed."""
        return all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        """Append one check result to this report."""
        self.checks.append(CheckResult(name, passed, detail))

    def as_dict(self) -> dict:
        """JSON-friendly representation, used for logging and for any caller that wants a structured summary."""
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


def validate_host(ctx: RunContext, cluster_cfg: dict, storage_backend: str) -> ValidationReport:
    """
    Run every post-bootstrap validation check for one host: required
    systemd services active, virsh responsive, every configured OVS
    bridge present, Ceph cluster healthy (if storage_backend=="ceph_rbd"),
    and IOMMU enabled (required for GPU mdev/PCI passthrough).

    Args:
        ctx: Run context (honors --dry-run -- Ceph health is treated as
            passing in a dry run, since no real cluster may exist yet).
        cluster_cfg: Parsed config/cluster.yaml (for the ovs_bridges list).
        storage_backend: This host's storage backend ("ceph_rbd" or
            "local_qcow2") -- the Ceph health check only runs for ceph_rbd.

    Returns:
        A ValidationReport with one CheckResult per check performed.
        Every check's outcome is also logged immediately (PASS/FAIL) as
        it's evaluated, in addition to being returned in the report.
    """
    report = ValidationReport()

    report.add("libvirtd_active", service_is_active(ctx, "libvirtd"))
    report.add("openvswitch_active", service_is_active(ctx, "openvswitch"))
    report.add("ovn_controller_active", service_is_active(ctx, "ovn-controller"))
    report.add("cockpit_active", service_is_active(ctx, "cockpit.socket"))

    virsh_check = run(ctx, ["virsh", "list", "--all"], check=False)
    report.add("virsh_responsive", virsh_check.returncode == 0, virsh_check.stderr.strip())

    for role, bridge in cluster_cfg["ovs_bridges"].items():
        br_check = run(ctx, ["ovs-vsctl", "br-exists", bridge], check=False)
        report.add(f"ovs_bridge_{role}", br_check.returncode == 0, bridge)

    if storage_backend == "ceph_rbd":
        health = run(ctx, ["ceph", "health"], check=False)
        report.add(
            "ceph_health_ok",
            "HEALTH_OK" in health.stdout or ctx.dry_run,
            health.stdout.strip(),
        )

    iommu_ok = gpu_mod.check_iommu_enabled(ctx)
    report.add("iommu_active", iommu_ok, "check dmesg for DMAR/IOMMU enabled")

    for c in report.checks:
        level = log.info if c.passed else log.error
        level("validate: %-24s %s %s", c.name, "PASS" if c.passed else "FAIL", c.detail)

    return report
