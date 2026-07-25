"""
End-to-end pipeline test against the actual generated proof-of-concept
mission (management/mission_defs/mission_alpha.yaml +
management/inventory/hosts.yaml), exercising every piece of core/ and
services/missions.py together the way workers/deploy.py does, minus the
real libvirt/OVN/Ceph calls (which need clients/* against real
infrastructure -- see README's transparency section).

This is the closest thing to an integration test this build sandbox can
run: it proves the full chain -- YAML -> MissionSpec -> placement
validation -> MAC assignment -> libvirt XML for all 40 VMs -- produces
exactly what the design docs' acceptance-criteria table calls for.
"""
from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.macs import assign_all_macs, validate_no_duplicate_macs
from core.placement import validate_placement
from core.xml_render import StorageContext, render_domain_xml
from services.missions import load_host_inventory, load_mission_yaml

MANAGEMENT_DIR = Path(__file__).resolve().parents[1]
MISSION_PATH = MANAGEMENT_DIR / "mission_defs" / "mission_alpha.yaml"

STORAGE = StorageContext(
    backend="ceph_rbd",
    ceph_secret_uuid="5e4b8c1a-9f2d-4e3a-8b7a-1c2d3e4f5a6b",
    ceph_monitors=[{"name": "ceph-mon1", "port": 6789}, {"name": "ceph-mon2", "port": 6789}],
)


@unittest.skipUnless(MISSION_PATH.exists(), "mission_alpha.yaml not generated -- run tools/generate_mission.py first")
class TestMissionAlphaPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mission = load_mission_yaml(MISSION_PATH)
        cls.hosts = load_host_inventory()

    def test_mission_has_40_vms_and_240_nics(self):
        self.assertEqual(len(self.mission.vms), 40)
        self.assertEqual(self.mission.total_nics(), 240)

    def test_exactly_7_gpu_vms(self):
        self.assertEqual(len(self.mission.gpu_vm_names()), 7)

    def test_exactly_10_linked_clone_and_30_pxe(self):
        linked = [v for v in self.mission.vms.values() if v.type.value == "linked_clone"]
        pxe = [v for v in self.mission.vms.values() if v.type.value == "pxe"]
        self.assertEqual(len(linked), 10)
        self.assertEqual(len(pxe), 30)

    def test_every_vm_has_all_6_networks(self):
        for vm_name, vm in self.mission.vms.items():
            self.assertEqual(len(vm.interfaces), 6, f"{vm_name} does not have 6 interfaces")

    def test_placement_is_feasible_against_host_inventory(self):
        report = validate_placement(self.mission, self.hosts)
        self.assertTrue(report.ok, report.as_dict())

    def test_gpu_vms_only_placed_on_gpu_capable_hosts(self):
        for vm_name in self.mission.gpu_vm_names():
            host = self.hosts[self.mission.placement[vm_name]]
            self.assertGreater(host.gpus, 0, f"{vm_name} placed on GPU-less host {host.name}")

    def test_all_240_macs_unique(self):
        interfaces_by_vm = {name: vm.interfaces for name, vm in self.mission.vms.items()}
        overrides = {name: vm.macs for name, vm in self.mission.vms.items() if vm.macs}
        assigned = assign_all_macs(self.mission.name, interfaces_by_vm, overrides)
        all_macs = [m for macs in assigned.values() for m in macs]
        self.assertEqual(len(all_macs), 240)
        self.assertEqual(validate_no_duplicate_macs(all_macs), [])

    def test_every_vm_carries_explicit_operator_supplied_macs(self):
        """
        Per operator confirmation: guest software in these VMs has MACs
        hardcoded into its licensing/configuration, so every VM's MAC
        addresses must come from the mission file itself, not from
        core/macs.py's fallback generator. Assert the mission's own
        `macs:` section is what's actually used -- not merely present,
        but that assign_all_macs resolves to exactly those values with no
        VM falling through to generation.
        """
        for vm_name, vm in self.mission.vms.items():
            self.assertIsNotNone(vm.macs, f"{vm_name} has no explicit macs in mission_alpha.yaml")
            self.assertEqual(len(vm.macs), len(vm.interfaces), vm_name)

        interfaces_by_vm = {name: vm.interfaces for name, vm in self.mission.vms.items()}
        overrides = {name: vm.macs for name, vm in self.mission.vms.items() if vm.macs}
        self.assertEqual(len(overrides), len(self.mission.vms), "every VM should be in the override set")

        assigned = assign_all_macs(self.mission.name, interfaces_by_vm, overrides)
        for vm_name, vm in self.mission.vms.items():
            self.assertEqual(assigned[vm_name], vm.macs, f"{vm_name}: resolved MACs must equal the file's explicit values verbatim")

    def test_renders_valid_xml_for_every_vm_with_correct_structure(self):
        interfaces_by_vm = {name: vm.interfaces for name, vm in self.mission.vms.items()}
        overrides = {name: vm.macs for name, vm in self.mission.vms.items() if vm.macs}
        assigned_macs = assign_all_macs(self.mission.name, interfaces_by_vm, overrides)

        # Mirrors the per-host GPU allocator in workers/deploy.py.
        gpu_pool = {name: list(host.gpu_pci_addresses) for name, host in self.hosts.items()}

        for vm_name, vm in self.mission.vms.items():
            macs = assigned_macs[vm_name]
            gpu_pci = None
            if vm.gpu:
                host_name = self.mission.placement[vm_name]
                gpu_pci = [gpu_pool[host_name].pop(0)]

            xml_str = render_domain_xml(self.mission.name, vm_name, vm, macs, STORAGE, gpu_pci_addresses=gpu_pci)
            root = ET.fromstring(xml_str)  # raises if not well-formed

            self.assertEqual(len(root.findall("./devices/interface")), 6, vm_name)
            disks = root.findall("./devices/disk")
            if vm.type.value == "linked_clone":
                self.assertEqual(len(disks), 1, vm_name)
            else:
                self.assertEqual(len(disks), 0, vm_name)

            hostdevs = root.findall("./devices/hostdev")
            self.assertEqual(len(hostdevs), 1 if vm.gpu else 0, vm_name)
            self.assertEqual(int(root.find("./vcpu").text), vm.cpu, vm_name)
            self.assertEqual(int(root.find("./memory").text), vm.memory_mb, vm_name)

    def test_per_host_capacity_matches_inventory_after_full_allocation(self):
        totals = {h: {"cpu": 0, "mem": 0, "gpu": 0} for h in self.hosts}
        for vm_name, vm in self.mission.vms.items():
            host = self.mission.placement[vm_name]
            totals[host]["cpu"] += vm.cpu
            totals[host]["mem"] += vm.memory_mb
            if vm.gpu:
                totals[host]["gpu"] += 1
        for host_name, host in self.hosts.items():
            self.assertLessEqual(totals[host_name]["cpu"], host.cpus)
            self.assertLessEqual(totals[host_name]["mem"], host.memory_mb)
            self.assertLessEqual(totals[host_name]["gpu"], host.gpus)


if __name__ == "__main__":
    unittest.main()
