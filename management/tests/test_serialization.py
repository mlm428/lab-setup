"""Unit tests for core/serialization.py -- MissionSpec/MissionStatus <-> JSON round-tripping for persistence."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.serialization import (
    mission_spec_from_jsonable,
    mission_spec_to_jsonable,
    mission_status_from_jsonable,
    mission_status_to_jsonable,
)
from core.types import InterfaceSpec, MissionSpec, MissionState, MissionStatus, StepLogEntry, VMSpec, VMType


def make_mission():
    vms = {
        "db01": VMSpec(
            name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192,
            interfaces={"control": InterfaceSpec("control", "10:00:00"), "storage": InterfaceSpec("storage", None)},
            image="rhel9-db-golden", image_revision="Alpha",
        ),
        "render01": VMSpec(
            name="render01", type=VMType.PXE, cpu=8, memory_mb=32768,
            interfaces={"control": InterfaceSpec("control", "20:00:00")},
            gpu_profile="H100-MIG-3g.40gb",
        ),
    }
    return MissionSpec(
        name="Mission-Test", networks={"control": 100, "storage": 110},
        vms=vms, placement={"db01": "h1", "render01": "h2"}, file_version="1.2.3",
    )


class TestMissionSpecRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_all_fields(self):
        original = make_mission()
        restored = mission_spec_from_jsonable(mission_spec_to_jsonable(original))

        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.networks, original.networks)
        self.assertEqual(restored.placement, original.placement)
        self.assertEqual(restored.file_version, original.file_version)
        self.assertEqual(set(restored.vms.keys()), set(original.vms.keys()))
        self.assertEqual(restored.vms["db01"].image, "rhel9-db-golden")
        self.assertEqual(restored.vms["db01"].image_revision, "Alpha")
        self.assertEqual(restored.vms["render01"].gpu_profile, "H100-MIG-3g.40gb")

    def test_none_mac_suffix_preserved(self):
        original = make_mission()
        restored = mission_spec_from_jsonable(mission_spec_to_jsonable(original))
        self.assertIsNone(restored.vms["db01"].interfaces["storage"].mac_suffix)
        self.assertEqual(restored.vms["db01"].interfaces["control"].mac_suffix, "10:00:00")

    def test_interface_order_preserved(self):
        original = make_mission()
        restored = mission_spec_from_jsonable(mission_spec_to_jsonable(original))
        self.assertEqual(list(restored.vms["db01"].interfaces.keys()), list(original.vms["db01"].interfaces.keys()))

    def test_output_is_actually_json_serializable(self):
        original = make_mission()
        data = mission_spec_to_jsonable(original)
        round_tripped = json.loads(json.dumps(data))  # confirm no non-JSON types (enums, dataclasses) slipped through
        restored = mission_spec_from_jsonable(round_tripped)
        self.assertEqual(restored.name, original.name)

    def test_no_gpu_no_image_vm_round_trips(self):
        vm = VMSpec(name="worker01", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces={"control": InterfaceSpec("control", None)})
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"worker01": vm}, placement={"worker01": "h1"})
        restored = mission_spec_from_jsonable(mission_spec_to_jsonable(mission))
        self.assertIsNone(restored.vms["worker01"].gpu_profile)
        self.assertIsNone(restored.vms["worker01"].image)


class TestMissionStatusRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_all_fields(self):
        mission = make_mission()
        original = MissionStatus(
            mission_id="abc-123", name="Mission-Test", state=MissionState.RUNNING,
            steps=[StepLogEntry("networks", "ok", "2 switches"), StepLogEntry("deploy", "error", "boom")],
            error="boom", spec=mission, mac_prefix="aa:bb:cc",
            resolved_macs={"db01": ["aa:bb:cc:10:00:00", "aa:bb:cc:7d:22:11"]},
            gpu_allocations={"render01": "uuid-1234"},
        )
        restored = mission_status_from_jsonable(mission_status_to_jsonable(original))

        self.assertEqual(restored.mission_id, "abc-123")
        self.assertEqual(restored.name, "Mission-Test")
        self.assertEqual(restored.state, MissionState.RUNNING)
        self.assertEqual(len(restored.steps), 2)
        self.assertEqual(restored.steps[0].step, "networks")
        self.assertEqual(restored.error, "boom")
        self.assertEqual(restored.mac_prefix, "aa:bb:cc")
        self.assertEqual(restored.resolved_macs, original.resolved_macs)
        self.assertEqual(restored.gpu_allocations, original.gpu_allocations)
        self.assertEqual(restored.spec.name, "Mission-Test")
        self.assertEqual(restored.spec.vms["db01"].image_revision, "Alpha")

    def test_no_spec_round_trips_as_none(self):
        original = MissionStatus(mission_id="abc", name="M", state=MissionState.PENDING)
        restored = mission_status_from_jsonable(mission_status_to_jsonable(original))
        self.assertIsNone(restored.spec)

    def test_output_is_actually_json_serializable(self):
        mission = make_mission()
        original = MissionStatus(mission_id="abc", name="Mission-Test", state=MissionState.ROLLED_BACK, spec=mission)
        data = mission_status_to_jsonable(original)
        round_tripped = json.loads(json.dumps(data))
        restored = mission_status_from_jsonable(round_tripped)
        self.assertEqual(restored.state, MissionState.ROLLED_BACK)


if __name__ == "__main__":
    unittest.main()
