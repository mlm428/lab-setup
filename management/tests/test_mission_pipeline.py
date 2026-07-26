"""
End-to-end pipeline tests against the real generated mission files
(mission_alpha.yaml: 40 VMs, mission_bravo.yaml: 4 VMs) and the real
config/hosts.yaml -- exercising every piece of core/ and services/ that
doesn't require fastapi/pydantic/libvirt/ovsdbapp/rados/rbd.
"""
from __future__ import annotations

import random
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.macs import resolve_mission_macs, validate_no_duplicate_macs
from core.placement import reserve_gpu_devices, validate_placement
from core.state import MissionStore
from core.xml_render import render_domain_xml
from services.cluster_config import load_deployment_config
from services.missions import load_host_inventory, load_mission_yaml

MISSION_DEFS = Path(__file__).resolve().parents[1] / "mission_defs"


class TestMissionAlphaPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mission = load_mission_yaml(MISSION_DEFS / "mission_alpha.yaml")
        cls.hosts = load_host_inventory()
        cls.deployment_cfg = load_deployment_config()

    def test_forty_vms_240_nics(self):
        self.assertEqual(len(self.mission.vms), 40)
        self.assertEqual(self.mission.total_nics(), 240)

    def test_seven_gpu_vms(self):
        self.assertEqual(len(self.mission.gpu_vm_names()), 7)

    def test_placement_fits_capacity(self):
        report = validate_placement(self.mission, self.hosts)
        self.assertTrue(report.ok, report.as_dict())

    def test_mac_resolution_no_collisions(self):
        prefix, macs = resolve_mission_macs(self.mission, set(), rng=random.Random(42))
        all_macs = [m for vm_macs in macs.values() for m in vm_macs]
        self.assertEqual(len(all_macs), 240)
        self.assertEqual(validate_no_duplicate_macs(all_macs), [])

    def test_two_concurrent_deployments_isolated_but_substring_matchable(self):
        prefix1, macs1 = resolve_mission_macs(self.mission, set(), rng=random.Random(1))
        prefix2, macs2 = resolve_mission_macs(self.mission, {prefix1}, rng=random.Random(2))
        self.assertNotEqual(prefix1, prefix2)
        for vm_name in self.mission.vms:
            suffixes1 = [m.split(":", 3)[3:] for m in macs1[vm_name]]
            suffixes2 = [m.split(":", 3)[3:] for m in macs2[vm_name]]
            self.assertEqual(suffixes1, suffixes2, f"{vm_name}: suffixes must match across deployments")
            self.assertNotEqual(macs1[vm_name], macs2[vm_name], f"{vm_name}: full MACs must differ across deployments")

    def test_every_vm_renders_valid_xml_with_expected_devices(self):
        prefix, macs = resolve_mission_macs(self.mission, set(), rng=random.Random(3))
        gpu_pool = {name: list(host.gpu_devices) for name, host in self.hosts.items()}

        for vm_name, vm in self.mission.vms.items():
            host_name = self.mission.placement[vm_name]
            gpu_mdev = None
            if vm.has_gpu:
                available = gpu_pool[host_name]
                idx = next(i for i, d in enumerate(available) if d.profile == vm.gpu_profile)
                gpu_mdev = available.pop(idx).mdev_uuid

            xml_str = render_domain_xml(
                self.mission.name, vm_name, vm, macs[vm_name], self.deployment_cfg.runtime, gpu_mdev_uuid=gpu_mdev,
            )
            root = ET.fromstring(xml_str)
            self.assertEqual(len(root.findall("./devices/interface")), 6, vm_name)
            expected_disks = 1 if vm.type.value == "linked_clone" else 0
            self.assertEqual(len(root.findall("./devices/disk")), expected_disks, vm_name)
            expected_hostdevs = 1 if vm.has_gpu else 0
            self.assertEqual(len(root.findall("./devices/hostdev")), expected_hostdevs, vm_name)

    def test_gpu_vms_exactly_fill_available_profile_slices(self):
        # By construction (see tools/generate_mission.py's ALPHA_ROLES),
        # mission_alpha uses exactly as many of each GPU profile as
        # config/hosts.yaml provides -- confirms the generator and the
        # inventory haven't drifted apart from each other.
        gpu_pool = {name: list(host.gpu_devices) for name, host in self.hosts.items()}
        for vm_name, vm in self.mission.vms.items():
            if not vm.has_gpu:
                continue
            host_name = self.mission.placement[vm_name]
            available = gpu_pool[host_name]
            idx = next((i for i, d in enumerate(available) if d.profile == vm.gpu_profile), None)
            self.assertIsNotNone(idx, f"{vm_name}: no matching profile left on {host_name}")
            available.pop(idx)


class TestMissionBravoPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mission = load_mission_yaml(MISSION_DEFS / "mission_bravo.yaml")
        cls.hosts = load_host_inventory()

    def test_four_vms_two_linked_two_pxe(self):
        self.assertEqual(len(self.mission.vms), 4)
        linked = [v for v in self.mission.vms.values() if v.type.value == "linked_clone"]
        pxe = [v for v in self.mission.vms.values() if v.type.value == "pxe"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(pxe), 2)

    def test_placement_fits_capacity(self):
        report = validate_placement(self.mission, self.hosts)
        self.assertTrue(report.ok, report.as_dict())

    def test_mac_resolution_no_collisions(self):
        prefix, macs = resolve_mission_macs(self.mission, set(), rng=random.Random(1))
        all_macs = [m for vm_macs in macs.values() for m in vm_macs]
        self.assertEqual(validate_no_duplicate_macs(all_macs), [])


class TestTemplateYaml(unittest.TestCase):
    """template.yaml is a reference/example, not deployed -- but it should still parse and validate cleanly, since a stale example helps no one."""

    def test_template_parses_and_validates(self):
        mission = load_mission_yaml(MISSION_DEFS / "template.yaml")
        hosts = load_host_inventory()
        report = validate_placement(mission, hosts)
        self.assertTrue(report.ok, report.as_dict())


class TestConcurrentRegistrationViaStore(unittest.TestCase):
    def test_store_level_atomic_registration_across_two_full_missions(self):
        store = MissionStore()
        mission = load_mission_yaml(MISSION_DEFS / "mission_bravo.yaml")
        hosts = load_host_inventory()
        status1 = store.register_deployment(mission.name, mission, hosts, resolve_mission_macs, reserve_gpu_devices)
        status2 = store.register_deployment(mission.name, mission, hosts, resolve_mission_macs, reserve_gpu_devices)
        self.assertNotEqual(status1.mac_prefix, status2.mac_prefix)
        self.assertEqual(store.active_mac_prefixes(), {status1.mac_prefix, status2.mac_prefix})
        # mission_bravo has one GPU VM (render01); two concurrent
        # deployments must be handed two DIFFERENT physical mdev slices,
        # not the same one.
        self.assertNotEqual(status1.gpu_allocations["render01"], status2.gpu_allocations["render01"])
        self.assertEqual(store.active_gpu_allocations(), {status1.gpu_allocations["render01"], status2.gpu_allocations["render01"]})


if __name__ == "__main__":
    unittest.main()
