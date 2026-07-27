"""
Post-deploy validation: operationalizes the acceptance-criteria tables
from both source documents (VM count, NIC count/MACs per VM, disk
presence/absence, GPU hostdev presence, CPU/memory match, OVN switch
count). Used by workers/deploy.py's final step and by
tools/validate_deployment.py for a stand-alone re-check against a live
mission.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from clients import libvirt_client, ovn_client
from core.types import MissionSpec, VMType
from core.xml_render import switch_name


@dataclass
class ValidationIssue:
    """One acceptance-criteria mismatch found for a specific VM."""
    vm_name: str
    check: str
    detail: str


@dataclass
class MissionValidationReport:
    """Aggregate result of validate_mission_deployment."""
    issues: list[ValidationIssue] = field(default_factory=list)
    vm_count_expected: int = 0
    vm_count_found: int = 0
    nic_count_expected: int = 0

    @property
    def ok(self) -> bool:
        """True if every VM was found with no issues and the count matches expectations."""
        return not self.issues and self.vm_count_expected == self.vm_count_found

    def as_dict(self) -> dict:
        """JSON-friendly representation, used in API/status output and logs."""
        return {
            "ok": self.ok,
            "vm_count_expected": self.vm_count_expected,
            "vm_count_found": self.vm_count_found,
            "nic_count_expected": self.nic_count_expected,
            "issues": [
                {"vm": i.vm_name, "check": i.check, "detail": i.detail}
                for i in self.issues
            ],
        }


def validate_mission_deployment(
    mission: MissionSpec,
    host_addresses_by_vm: dict[str, str],
    assigned_macs: dict[str, list[str]],
    gpu_mdev_by_vm: dict[str, str],
    ssh_user: str = "root",
) -> MissionValidationReport:
    """
    Re-inspect every VM's actual libvirt domain XML on its placed host and
    compare it against the mission spec.

    Args:
        mission: The mission being validated.
        host_addresses_by_vm: {vm_name: host_address} actually used at deploy time.
        assigned_macs: {vm_name: [mac, ...]} actually used at deploy time.
        gpu_mdev_by_vm: {vm_name: mdev_uuid} for every VM that has a GPU.
        ssh_user: Non-root SSH user for the libvirt connection (see
            config/hosts.yaml's management_ssh_user).

    Returns:
        A MissionValidationReport listing every mismatch found (empty
        `issues` and vm_count_expected == vm_count_found means the
        deployment fully matches its spec).
    """
    report = MissionValidationReport()
    report.vm_count_expected = len(mission.vms)
    report.nic_count_expected = mission.total_nics()

    found = 0
    for vm_name, vm in mission.vms.items():
        host = host_addresses_by_vm.get(vm_name)
        if host is None:
            report.issues.append(ValidationIssue(vm_name, "placement", "no host recorded for this VM"))
            continue

        conn = libvirt_client.connect(host, ssh_user=ssh_user)
        try:
            names = libvirt_client.list_domain_names(conn)
            if vm_name not in names:
                report.issues.append(ValidationIssue(vm_name, "exists", f"not found among domains on {host}"))
                continue
            found += 1

            xml_desc = libvirt_client.domain_xml_desc(conn, vm_name)
            _validate_single_vm_xml(
                report, vm_name, vm, xml_desc,
                expected_macs=assigned_macs.get(vm_name, []),
                expected_mdev_uuid=gpu_mdev_by_vm.get(vm_name),
            )
        finally:
            conn.close()

    report.vm_count_found = found
    return report


def _validate_single_vm_xml(
    report: MissionValidationReport,
    vm_name: str,
    vm,
    xml_desc: str,
    expected_macs: list[str],
    expected_mdev_uuid: str | None,
) -> None:
    """Compare one VM's actual domain XML against its spec, appending any mismatches to `report`."""
    root = ET.fromstring(xml_desc)

    interfaces = root.findall("./devices/interface")
    if len(interfaces) != len(vm.interfaces):
        report.issues.append(ValidationIssue(
            vm_name, "nic_count", f"expected {len(vm.interfaces)}, found {len(interfaces)}",
        ))
    found_macs = [i.find("mac").get("address") for i in interfaces if i.find("mac") is not None]
    if expected_macs and sorted(found_macs) != sorted(expected_macs):
        report.issues.append(ValidationIssue(vm_name, "mac_addresses", f"expected {expected_macs}, found {found_macs}"))

    disks = root.findall("./devices/disk")
    if vm.type == VMType.LINKED_CLONE and len(disks) < 1:
        report.issues.append(ValidationIssue(vm_name, "disk_presence", "expected a disk for a linked_clone VM, found none"))
    if vm.type == VMType.PXE and len(disks) != 0:
        report.issues.append(ValidationIssue(vm_name, "disk_presence", f"expected no disk for a PXE VM, found {len(disks)}"))

    hostdevs = root.findall("./devices/hostdev")
    if vm.has_gpu:
        if len(hostdevs) != 1:
            report.issues.append(ValidationIssue(vm_name, "gpu_hostdev", f"expected 1 mdev hostdev, found {len(hostdevs)}"))
        else:
            found_uuid = hostdevs[0].find("./source/address")
            found_uuid = found_uuid.get("uuid") if found_uuid is not None else None
            if found_uuid != expected_mdev_uuid:
                report.issues.append(ValidationIssue(vm_name, "gpu_mdev_uuid", f"expected {expected_mdev_uuid}, found {found_uuid}"))
    elif len(hostdevs) != 0:
        report.issues.append(ValidationIssue(vm_name, "gpu_hostdev", f"expected 0 hostdevs, found {len(hostdevs)}"))

    vcpu_el = root.find("./vcpu")
    if vcpu_el is not None and int(vcpu_el.text) != vm.cpu:
        report.issues.append(ValidationIssue(vm_name, "cpu", f"expected {vm.cpu}, found {vcpu_el.text}"))

    mem_el = root.find("./memory")
    if mem_el is not None and int(mem_el.text) != vm.memory_mb:
        report.issues.append(ValidationIssue(vm_name, "memory", f"expected {vm.memory_mb} MiB, found {mem_el.text}"))


def validate_networks(api, mission_id: str, mission: MissionSpec) -> list[str]:
    """
    Check that every mission network's OVN logical switch actually exists.

    Args:
        api: A connected ovsdbapp OVN Northbound API object.
        mission_id: This deployment's unique id (see
            services/networking.py:provision_networks's docstring for why
            switch names are scoped by this, not just mission.name).
        mission: The mission (deployment) being validated.

    Returns:
        Names of any mission networks whose OVN logical switch is missing
        (empty list == all present, per the "Networks" acceptance criterion).
    """
    existing = set(ovn_client.list_logical_switches(api))
    missing = []
    for net_name in mission.networks:
        expected = switch_name(mission.name, mission_id, net_name)
        if expected not in existing:
            missing.append(net_name)
    return missing
