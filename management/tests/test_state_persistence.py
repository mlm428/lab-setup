"""
Unit tests for core/state.py's SQLite persistence -- the "MissionStore(db_path=...)"
path specifically, using real temp-file SQLite databases (sqlite3 is
stdlib, so this needs no extra dependency and works fully offline). The
default (db_path=None, in-memory only) behavior is already covered by
test_state.py; this file is about durability across a MissionStore being
discarded and a new one constructed against the same file, simulating a
process restart.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.macs import resolve_mission_macs
from core.placement import reserve_gpu_devices
from core.state import MissionStore
from core.types import GpuDeviceSpec, HostSpec, InterfaceSpec, MissionSpec, MissionState, VMSpec, VMType


def make_mission(name="Mission-Test"):
    vm = VMSpec(
        name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192,
        interfaces={"control": InterfaceSpec("control", "10:00:00")},
        image="rhel9-db-golden", image_revision="Alpha",
    )
    return MissionSpec(name=name, networks={"control": 100}, vms={"db01": vm}, placement={"db01": "h1"})


class TestSqlitePersistence(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "missions.db"
        self.addCleanup(self._tmpdir.cleanup)

    def test_no_db_path_creates_no_file(self):
        MissionStore()  # default -- in-memory only
        self.assertFalse(self.db_path.exists())

    def test_db_path_creates_file(self):
        MissionStore(db_path=self.db_path)
        self.assertTrue(self.db_path.exists())

    def test_mission_survives_a_simulated_restart(self):
        store1 = MissionStore(db_path=self.db_path)
        mission = make_mission()
        status = store1.create(mission.name, mission)
        store1.update_state(status.mission_id, MissionState.RUNNING)
        store1.log_step(status.mission_id, "networks", "ok", "2 switches")

        # Simulate a process restart: discard store1, construct a fresh
        # MissionStore against the SAME file.
        store2 = MissionStore(db_path=self.db_path)
        restored = store2.get(status.mission_id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.state, MissionState.RUNNING)
        self.assertEqual(restored.name, "Mission-Test")
        self.assertEqual(len(restored.steps), 1)
        self.assertEqual(restored.steps[0].step, "networks")
        self.assertEqual(restored.spec.vms["db01"].image_revision, "Alpha")

    def test_mac_prefix_and_gpu_allocations_survive_restart(self):
        store1 = MissionStore(db_path=self.db_path)
        mission = make_mission()
        mission.vms["db01"] = VMSpec(
            name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192,
            interfaces={"control": InterfaceSpec("control", "10:00:00")},
            image="rhel9-db-golden", gpu_profile="X",
        )
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="uuid-1")])}
        status = store1.register_deployment(mission.name, mission, hosts, resolve_mission_macs, reserve_gpu_devices)

        store2 = MissionStore(db_path=self.db_path)
        self.assertEqual(store2.active_mac_prefixes(), {status.mac_prefix})
        self.assertEqual(store2.active_gpu_allocations(), {"uuid-1"})

    def test_removed_mission_does_not_reappear_after_restart(self):
        store1 = MissionStore(db_path=self.db_path)
        mission = make_mission()
        status = store1.create(mission.name, mission)
        store1.remove(status.mission_id)

        store2 = MissionStore(db_path=self.db_path)
        self.assertIsNone(store2.get(status.mission_id))
        self.assertEqual(store2.list(), [])

    def test_multiple_missions_all_survive_restart(self):
        store1 = MissionStore(db_path=self.db_path)
        s1 = store1.create("Mission-A", make_mission("Mission-A"))
        s2 = store1.create("Mission-B", make_mission("Mission-B"))
        store1.update_state(s2.mission_id, MissionState.ERROR)

        store2 = MissionStore(db_path=self.db_path)
        self.assertEqual(len(store2.list()), 2)
        self.assertEqual(store2.get(s1.mission_id).state, MissionState.PENDING)
        self.assertEqual(store2.get(s2.mission_id).state, MissionState.ERROR)

    def test_adopt_persists_too(self):
        store1 = MissionStore(db_path=self.db_path)
        from core.types import MissionStatus
        status = MissionStatus(mission_id="reconciled-1", name="Mission-Reconciled", state=MissionState.RUNNING)
        store1.adopt(status)

        store2 = MissionStore(db_path=self.db_path)
        self.assertIsNotNone(store2.get("reconciled-1"))
        self.assertEqual(store2.get("reconciled-1").name, "Mission-Reconciled")

    def test_directory_created_if_missing(self):
        nested_path = Path(self._tmpdir.name) / "nested" / "subdir" / "missions.db"
        MissionStore(db_path=nested_path)
        self.assertTrue(nested_path.exists())


if __name__ == "__main__":
    unittest.main()
