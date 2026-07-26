"""Unit tests for core/state.py -- MissionStore, especially atomic MAC prefix + GPU device allocation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import InterfaceSpec, MissionSpec, MissionState, VMSpec, VMType


def make_mission(name="Mission-Test"):
    vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces={"control": InterfaceSpec("control", "10:00:00")})
    return MissionSpec(name=name, networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})


def fake_resolver(assigned_prefix):
    """Build a mac_resolve_fn that always returns a fixed prefix, ignoring existing_prefixes -- for tests that want deterministic control."""
    def _resolve(mission, existing_prefixes):
        return assigned_prefix, {"vm1": [f"{assigned_prefix}:10:00:00"]}
    return _resolve


def no_op_gpu_resolver(mission, hosts, already_reserved):
    """A gpu_resolve_fn that reserves nothing -- for tests with no GPU VMs."""
    return {}


class TestMissionStoreBasics(unittest.TestCase):
    def test_create_and_get(self):
        store = MissionStore()
        status = store.create("Mission-Test", make_mission())
        self.assertEqual(store.get(status.mission_id).name, "Mission-Test")

    def test_get_missing_returns_none(self):
        store = MissionStore()
        self.assertIsNone(store.get("nonexistent"))

    def test_list_returns_all(self):
        store = MissionStore()
        store.create("A", make_mission("A"))
        store.create("B", make_mission("B"))
        self.assertEqual(len(store.list()), 2)

    def test_update_state(self):
        store = MissionStore()
        status = store.create("Mission-Test", make_mission())
        store.update_state(status.mission_id, MissionState.RUNNING)
        self.assertEqual(store.get(status.mission_id).state, MissionState.RUNNING)

    def test_log_step(self):
        store = MissionStore()
        status = store.create("Mission-Test", make_mission())
        store.log_step(status.mission_id, "networks", "ok", "detail")
        self.assertEqual(store.get(status.mission_id).steps[0].step, "networks")

    def test_fail_records_error(self):
        store = MissionStore()
        status = store.create("Mission-Test", make_mission())
        store.fail(status.mission_id, "deploy", RuntimeError("boom"))
        updated = store.get(status.mission_id)
        self.assertEqual(updated.state, MissionState.ERROR)
        self.assertEqual(updated.error, "boom")

    def test_remove(self):
        store = MissionStore()
        status = store.create("Mission-Test", make_mission())
        store.remove(status.mission_id)
        self.assertIsNone(store.get(status.mission_id))


class TestRegisterDeployment(unittest.TestCase):
    def test_populates_prefix_and_macs(self):
        store = MissionStore()
        status = store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), no_op_gpu_resolver)
        self.assertEqual(status.mac_prefix, "aa:bb:cc")
        self.assertEqual(status.resolved_macs["vm1"], ["aa:bb:cc:10:00:00"])

    def test_active_mac_prefixes_reflects_registered_deployments(self):
        store = MissionStore()
        store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), no_op_gpu_resolver)
        self.assertEqual(store.active_mac_prefixes(), {"aa:bb:cc"})

    def test_destroyed_mission_frees_its_prefix(self):
        store = MissionStore()
        status = store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), no_op_gpu_resolver)
        self.assertEqual(store.active_mac_prefixes(), {"aa:bb:cc"})
        store.update_state(status.mission_id, MissionState.DESTROYED)
        self.assertEqual(store.active_mac_prefixes(), set())

    def test_mac_resolver_sees_prefixes_from_prior_active_deployments(self):
        store = MissionStore()
        store.register_deployment("Mission-A", make_mission("Mission-A"), {}, fake_resolver("aa:bb:cc"), no_op_gpu_resolver)

        seen_existing = []

        def capturing_resolver(mission, existing_prefixes):
            seen_existing.append(set(existing_prefixes))
            return "dd:ee:ff", {"vm1": ["dd:ee:ff:10:00:00"]}

        store.register_deployment("Mission-B", make_mission("Mission-B"), {}, capturing_resolver, no_op_gpu_resolver)
        self.assertEqual(seen_existing[0], {"aa:bb:cc"})

    def test_mac_resolver_raising_registers_nothing(self):
        store = MissionStore()

        def failing_resolver(mission, existing_prefixes):
            raise ValueError("duplicate suffix")

        with self.assertRaises(ValueError):
            store.register_deployment("Mission-Test", make_mission(), {}, failing_resolver, no_op_gpu_resolver)
        self.assertEqual(store.list(), [])

    def test_populates_gpu_allocations(self):
        store = MissionStore()

        def gpu_resolver(mission, hosts, already_reserved):
            return {"vm1": "uuid-1234"}

        status = store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), gpu_resolver)
        self.assertEqual(status.gpu_allocations, {"vm1": "uuid-1234"})

    def test_active_gpu_allocations_reflects_registered_deployments(self):
        store = MissionStore()

        def gpu_resolver(mission, hosts, already_reserved):
            return {"vm1": "uuid-1234"}

        store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), gpu_resolver)
        self.assertEqual(store.active_gpu_allocations(), {"uuid-1234"})

    def test_destroyed_mission_frees_its_gpu_allocations(self):
        store = MissionStore()

        def gpu_resolver(mission, hosts, already_reserved):
            return {"vm1": "uuid-1234"}

        status = store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), gpu_resolver)
        self.assertEqual(store.active_gpu_allocations(), {"uuid-1234"})
        store.update_state(status.mission_id, MissionState.DESTROYED)
        self.assertEqual(store.active_gpu_allocations(), set())

    def test_gpu_resolver_sees_allocations_from_prior_active_deployments(self):
        store = MissionStore()

        def gpu_resolver_a(mission, hosts, already_reserved):
            return {"vm1": "uuid-1234"}

        store.register_deployment("Mission-A", make_mission("Mission-A"), {}, fake_resolver("aa:bb:cc"), gpu_resolver_a)

        seen_existing = []

        def capturing_gpu_resolver(mission, hosts, already_reserved):
            seen_existing.append(set(already_reserved))
            return {"vm1": "uuid-5678"}

        store.register_deployment("Mission-B", make_mission("Mission-B"), {}, fake_resolver("dd:ee:ff"), capturing_gpu_resolver)
        self.assertEqual(seen_existing[0], {"uuid-1234"})

    def test_gpu_resolver_raising_registers_nothing_including_mac_prefix(self):
        store = MissionStore()

        def failing_gpu_resolver(mission, hosts, already_reserved):
            raise RuntimeError("no free GPU slice")

        with self.assertRaises(RuntimeError):
            store.register_deployment("Mission-Test", make_mission(), {}, fake_resolver("aa:bb:cc"), failing_gpu_resolver)
        self.assertEqual(store.list(), [])
        # The MAC prefix must not be "half-reserved" either -- the whole
        # registration is atomic, not just the GPU half of it.
        self.assertEqual(store.active_mac_prefixes(), set())


if __name__ == "__main__":
    unittest.main()
