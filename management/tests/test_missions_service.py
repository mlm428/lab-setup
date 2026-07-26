"""Unit tests for services/missions.py -- mission dict/YAML parsing and the register_mission validation+MAC-resolution flow."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import GpuDeviceSpec, HostSpec
from services import missions as missions_mod


def base_mission_dict():
    return {
        "mission": "Mission-Test",
        "file_version": "2.0",
        "networks": {"control": 100, "storage": 110},
        "vms": {
            "db01": {
                "type": "linked_clone",
                "image": "rhel9-db-golden",
                "image_revision": "Alpha",
                "cpu": 4,
                "memory": 8192,
                "interfaces": {"control": {"mac_suffix": "10:00:00"}, "storage": {}},
            },
            "render01": {
                "type": "pxe",
                "cpu": 8,
                "memory": 16384,
                "gpu": {"profile": "H100-MIG-3g.40gb"},
                "interfaces": {"control": {"mac_suffix": "20:00:00"}, "storage": None},
            },
        },
        "placement": {"db01": "h1", "render01": "h1"},
    }


class TestMissionSpecFromDict(unittest.TestCase):
    def test_parses_basic_fields(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertEqual(mission.name, "Mission-Test")
        self.assertEqual(mission.file_version, "2.0")
        self.assertEqual(len(mission.vms), 2)

    def test_configured_mac_suffix_parsed(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertEqual(mission.vms["db01"].interfaces["control"].mac_suffix, "10:00:00")

    def test_empty_dict_interface_means_no_configured_suffix(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertIsNone(mission.vms["db01"].interfaces["storage"].mac_suffix)

    def test_null_interface_means_no_configured_suffix(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertIsNone(mission.vms["render01"].interfaces["storage"].mac_suffix)

    def test_gpu_profile_dict_form_parsed(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertEqual(mission.vms["render01"].gpu_profile, "H100-MIG-3g.40gb")

    def test_gpu_profile_bare_string_form_also_accepted(self):
        data = base_mission_dict()
        data["vms"]["render01"]["gpu"] = "H100-MIG-3g.40gb"
        mission = missions_mod.mission_spec_from_dict(data)
        self.assertEqual(mission.vms["render01"].gpu_profile, "H100-MIG-3g.40gb")

    def test_no_gpu_key_means_no_gpu(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertFalse(mission.vms["db01"].has_gpu)

    def test_image_revision_parsed(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        self.assertEqual(mission.vms["db01"].image_revision, "Alpha")


class TestRegisterMission(unittest.TestCase):
    def setUp(self):
        self.hosts = {
            "h1": HostSpec(
                name="h1", cpus=64, memory_mb=262144,
                gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1")],
            )
        }
        # Isolate from the process-wide singleton so tests don't leak state into each other.
        self.store_patch = unittest.mock.patch.object(missions_mod, "store", MissionStore())
        self.mock_store = self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def test_register_valid_mission_succeeds(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        status = missions_mod.register_mission(mission, self.hosts)
        self.assertIsNotNone(status.mac_prefix)
        self.assertTrue(status.resolved_macs["db01"][0].endswith(":10:00:00"))

    def test_register_infeasible_placement_raises_and_registers_nothing(self):
        data = base_mission_dict()
        data["vms"]["db01"]["cpu"] = 999
        mission = missions_mod.mission_spec_from_dict(data)
        with self.assertRaises(missions_mod.MissionValidationError):
            missions_mod.register_mission(mission, self.hosts)
        self.assertEqual(missions_mod.list_missions(), [])

    def test_register_duplicate_suffix_across_vms_raises(self):
        data = base_mission_dict()
        data["vms"]["render01"]["interfaces"]["control"]["mac_suffix"] = "10:00:00"  # collides with db01's
        mission = missions_mod.mission_spec_from_dict(data)
        with self.assertRaises(ValueError):
            missions_mod.register_mission(mission, self.hosts)

    def test_get_and_list_after_registration(self):
        mission = missions_mod.mission_spec_from_dict(base_mission_dict())
        status = missions_mod.register_mission(mission, self.hosts)
        self.assertEqual(missions_mod.get_mission(status.mission_id).mission_id, status.mission_id)
        self.assertEqual(len(missions_mod.list_missions()), 1)

    def test_two_missions_competing_for_the_only_gpu_slice_second_one_fails_clearly(self):
        # Regression test: validate_placement alone only checks a
        # mission's own VMs against total host capacity -- it has no
        # knowledge of GPU slices already claimed by OTHER active
        # deployments. Without register_mission's atomic cross-deployment
        # GPU reservation, this second registration would have wrongly
        # succeeded (both requests independently look feasible against
        # the single available slice), and the collision would only have
        # surfaced later as an opaque libvirt/mdev error at actual VM
        # definition time.
        single_gpu_hosts = {
            "h1": HostSpec(name="h1", cpus=64, memory_mb=262144, gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="only-uuid")])
        }
        mission_a = missions_mod.mission_spec_from_dict(base_mission_dict())
        mission_b = missions_mod.mission_spec_from_dict(base_mission_dict())

        status_a = missions_mod.register_mission(mission_a, single_gpu_hosts)
        self.assertEqual(status_a.gpu_allocations, {"render01": "only-uuid"})

        with self.assertRaises(RuntimeError):
            missions_mod.register_mission(mission_b, single_gpu_hosts)
        # Mission A's registration must be entirely unaffected.
        self.assertEqual(len(missions_mod.list_missions()), 1)


import unittest.mock  # noqa: E402 - used by setUp above


if __name__ == "__main__":
    unittest.main()
