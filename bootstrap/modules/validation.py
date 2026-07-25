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
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name, passed, detail))

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


def validate_host(ctx: RunContext, cluster_cfg: dict, storage_backend: str) -> ValidationReport:
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
