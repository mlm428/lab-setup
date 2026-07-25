"""
Unit tests for core/types.py -- the framework-independent domain model.
Runs with just the standard library.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import HostSpec, MissionSpec, MissionState, VMSpec, VMType


def make_vm(**overrides):
    defaults = dict(
        name="vm1",
        type=VMType.LINKED_CLONE,
        cpu=4,
        memory_mb=8192,
        interfaces=["control", "storage"],
        image="golden.qcow2",
    )
    defaults.update(overrides)
    return VMSpec(**defaults)


class TestVMSpec(unittest.TestCase):
    def test_linked_clone_requires_image(self):
        with self.assertRaises(ValueError):
            make_vm(image=None)

    def test_pxe_forbids_image(self):
        with self.assertRaises(ValueError):
            make_vm(type=VMType.PXE, image="golden.qcow2")

    def test_pxe_without_image_ok(self):
        vm = make_vm(type=VMType.PXE, image=None)
        self.assertEqual(vm.type, VMType.PXE)

    def test_rejects_nonpositive_cpu(self):
        with self.assertRaises(ValueError):
            make_vm(cpu=0)

    def test_rejects_nonpositive_memory(self):
        with self.assertRaises(ValueError):
            make_vm(memory_mb=0)

    def test_requires_at_least_one_interface(self):
        with self.assertRaises(ValueError):
            make_vm(interfaces=[])

    def test_macs_length_must_match_interfaces(self):
        with self.assertRaises(ValueError):
            make_vm(interfaces=["a", "b", "c"], macs=["52:54:00:00:00:01"])

    def test_macs_length_match_ok(self):
        vm = make_vm(interfaces=["a", "b"], macs=["52:54:00:00:00:01", "52:54:00:00:00:02"])
        self.assertEqual(len(vm.macs), 2)


class TestMissionSpec(unittest.TestCase):
    def _base_kwargs(self):
        return dict(
            name="Mission-Test",
            networks={"control": 100, "storage": 110},
            vms={"vm1": make_vm(interfaces=["control", "storage"])},
            placement={"vm1": "compute01"},
        )

    def test_valid_mission_constructs(self):
        mission = MissionSpec(**self._base_kwargs())
        self.assertEqual(mission.total_nics(), 2)
        self.assertEqual(mission.gpu_vm_names(), [])

    def test_missing_placement_rejected(self):
        kwargs = self._base_kwargs()
        kwargs["placement"] = {}
        with self.assertRaises(ValueError):
            MissionSpec(**kwargs)

    def test_placement_for_unknown_vm_rejected(self):
        kwargs = self._base_kwargs()
        kwargs["placement"] = {"vm1": "compute01", "ghost": "compute02"}
        with self.assertRaises(ValueError):
            MissionSpec(**kwargs)

    def test_vm_referencing_unknown_network_rejected(self):
        kwargs = self._base_kwargs()
        kwargs["vms"] = {"vm1": make_vm(interfaces=["control", "does-not-exist"])}
        with self.assertRaises(ValueError):
            MissionSpec(**kwargs)

    def test_gpu_vm_names(self):
        kwargs = self._base_kwargs()
        kwargs["vms"] = {
            "vm1": make_vm(interfaces=["control", "storage"], gpu=False),
            "vm2": make_vm(name="vm2", interfaces=["control", "storage"], gpu=True, type=VMType.PXE, image=None),
        }
        kwargs["placement"] = {"vm1": "compute01", "vm2": "compute02"}
        mission = MissionSpec(**kwargs)
        self.assertEqual(mission.gpu_vm_names(), ["vm2"])


class TestMissionStatusStateMachine(unittest.TestCase):
    def test_fail_transitions_to_error_and_records_message(self):
        from core.types import MissionStatus

        status = MissionStatus(mission_id="abc", name="Mission-Test")
        status.fail("define_vms", RuntimeError("boom"))
        self.assertEqual(status.state, MissionState.ERROR)
        self.assertEqual(status.error, "boom")
        self.assertEqual(status.steps[-1].status, "error")


if __name__ == "__main__":
    unittest.main()
