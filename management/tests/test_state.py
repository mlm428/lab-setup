from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import MissionState


class TestMissionStore(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()

    def test_create_and_get(self):
        status = self.store.create("Mission-Alpha")
        fetched = self.store.get(status.mission_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "Mission-Alpha")
        self.assertEqual(fetched.state, MissionState.PENDING)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_list_returns_all_created(self):
        self.store.create("Mission-A")
        self.store.create("Mission-B")
        names = sorted(s.name for s in self.store.list())
        self.assertEqual(names, ["Mission-A", "Mission-B"])

    def test_update_state(self):
        status = self.store.create("Mission-Alpha")
        self.store.update_state(status.mission_id, MissionState.RUNNING)
        self.assertEqual(self.store.get(status.mission_id).state, MissionState.RUNNING)

    def test_log_step_appends(self):
        status = self.store.create("Mission-Alpha")
        self.store.log_step(status.mission_id, "networks", "ok", "6 switches")
        steps = self.store.get(status.mission_id).steps
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].step, "networks")

    def test_fail_sets_error_state(self):
        status = self.store.create("Mission-Alpha")
        self.store.fail(status.mission_id, "define_vms", RuntimeError("boom"))
        updated = self.store.get(status.mission_id)
        self.assertEqual(updated.state, MissionState.ERROR)
        self.assertEqual(updated.error, "boom")

    def test_remove(self):
        status = self.store.create("Mission-Alpha")
        self.store.remove(status.mission_id)
        self.assertIsNone(self.store.get(status.mission_id))

    def test_spec_round_trips(self):
        from core.types import MissionSpec, VMSpec, VMType

        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=["control"])
        spec = MissionSpec(name="Mission-Alpha", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        status = self.store.create("Mission-Alpha", spec=spec)
        self.assertIs(self.store.get(status.mission_id).spec, spec)


if __name__ == "__main__":
    unittest.main()
