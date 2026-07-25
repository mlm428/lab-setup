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
    vm_name: str
    check: str
    detail: str


@dataclass
class MissionValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)
    vm_count_expected: int = 0
    vm_count_found: int = 0
    nic_count_expected: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues and self.vm_count_expected == self.vm_count_found

    def add(self, vm_name: str, check: str, detail: str) -> None:
        self.issues.append(ValidationIssue(vm_name, check, detail))

    def as_dict(self) -> dict:
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
    gpu_pci_by_vm: dict[str, list[str]],
) -> MissionValidationReport:
    report = MissionValidationReport()
    report.vm_count_expected = len(mission.vms)
    report.nic_count_expected = mission.total_nics()

    found = 0
    for vm_name, vm in mission.vms.items():
        host = host_addresses_by_vm.get(vm_name)
        if host is None:
            report.add(vm_name, "placement", "no host recorded for this VM")
            continue

        conn = libvirt_client.connect(host)
        try:
            names = libvirt_client.list_domain_names(conn)
            if vm_name not in names:
                report.add(vm_name, "exists", f"not found among domains on {host}")
                continue
            found += 1

            xml_desc = libvirt_client.domain_xml_desc(conn, vm_name)
            _validate_single_vm_xml(
                report, vm_name, vm, xml_desc,
                expected_macs=assigned_macs.get(vm_name, []),
                expected_gpu_count=len(gpu_pci_by_vm.get(vm_name, [])),
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
    expected_gpu_count: int,
) -> None:
    root = ET.fromstring(xml_desc)

    interfaces = root.findall("./devices/interface")
    if len(interfaces) != len(vm.interfaces):
        report.add(
            vm_name, "nic_count",
            f"expected {len(vm.interfaces)}, found {len(interfaces)}",
        )
    found_macs = [i.find("mac").get("address") for i in interfaces if i.find("mac") is not None]
    if expected_macs and sorted(found_macs) != sorted(expected_macs):
        report.add(vm_name, "mac_addresses", f"expected {expected_macs}, found {found_macs}")

    disks = root.findall("./devices/disk")
    if vm.type == VMType.LINKED_CLONE and len(disks) < 1:
        report.add(vm_name, "disk_presence", "expected a disk for a linked_clone VM, found none")
    if vm.type == VMType.PXE and len(disks) != 0:
        report.add(vm_name, "disk_presence", f"expected no disk for a PXE VM, found {len(disks)}")

    hostdevs = root.findall("./devices/hostdev")
    if vm.gpu and len(hostdevs) != expected_gpu_count:
        report.add(vm_name, "gpu_hostdev", f"expected {expected_gpu_count} hostdev(s), found {len(hostdevs)}")
    if not vm.gpu and len(hostdevs) != 0:
        report.add(vm_name, "gpu_hostdev", f"expected 0 hostdevs, found {len(hostdevs)}")

    vcpu_el = root.find("./vcpu")
    if vcpu_el is not None and int(vcpu_el.text) != vm.cpu:
        report.add(vm_name, "cpu", f"expected {vm.cpu}, found {vcpu_el.text}")

    mem_el = root.find("./memory")
    if mem_el is not None and int(mem_el.text) != vm.memory_mb:
        report.add(vm_name, "memory", f"expected {vm.memory_mb} MiB, found {mem_el.text}")


def validate_networks(api, mission: MissionSpec) -> list[str]:
    """Returns any mission network names whose OVN logical switch is
    missing (empty list == all present, per the 'Networks' acceptance
    criterion)."""
    existing = set(ovn_client.list_logical_switches(api))
    missing = []
    for net_name in mission.networks:
        expected = switch_name(mission.name, net_name)
        if expected not in existing:
            missing.append(net_name)
    return missing
