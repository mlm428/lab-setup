"""
Unit tests for core/types.py -- the framework-independent domain model.
Runs with just the standard library.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import GpuDeviceSpec, HostSpec, InterfaceSpec, MissionSpec, MissionState, VMSpec, VMType


def ifaces(*names_and_suffixes):
    """Helper: ifaces(('control','10:00:00'), ('storage', None)) -> ordered interfaces dict."""
    return {name: InterfaceSpec(network=name, mac_suffix=suffix) for name, suffix in names_and_suffixes}


def make_vm(**overrides):
    defaults = dict(
        name="vm1",
        type=VMType.LINKED_CLONE,
        cpu=4,
        memory_mb=8192,
        interfaces=ifaces(("control", "10:00:00"), ("storage", "10:00:01")),
        image="golden.qcow2",
    )
    defaults.update(overrides)
    return VMSpec(**defaults)


class TestInterfaceSpec(unittest.TestCase):
    def test_valid_suffix_ok(self):
        InterfaceSpec(network="control", mac_suffix="10:00:00")

    def test_none_suffix_ok(self):
        InterfaceSpec(network="control", mac_suffix=None)

    def test_wrong_octet_count_rejected(self):
        with self.assertRaises(ValueError):
            InterfaceSpec(network="control", mac_suffix="10:00")

    def test_non_hex_octet_rejected(self):
        with self.assertRaises(ValueError):
            InterfaceSpec(network="control", mac_suffix="zz:00:00")

    def test_wrong_octet_length_rejected(self):
        with self.assertRaises(ValueError):
            InterfaceSpec(network="control", mac_suffix="1:00:00")


class TestVMSpec(unittest.TestCase):
    def test_linked_clone_requires_image(self):
        with self.assertRaises(ValueError):
            make_vm(image=None)

    def test_pxe_forbids_image(self):
        with self.assertRaises(ValueError):
            make_vm(type=VMType.PXE, image="golden.qcow2")

    def test_pxe_forbids_image_revision(self):
        with self.assertRaises(ValueError):
            make_vm(type=VMType.PXE, image=None, image_revision="Alpha")

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
            make_vm(interfaces={})

    def test_duplicate_suffix_within_one_vm_rejected(self):
        with self.assertRaises(ValueError):
            make_vm(interfaces=ifaces(("control", "10:00:00"), ("storage", "10:00:00")))

    def test_distinct_suffixes_ok(self):
        vm = make_vm(interfaces=ifaces(("control", "10:00:00"), ("storage", "10:00:01")))
        self.assertEqual(len(vm.interfaces), 2)

    def test_none_suffixes_do_not_collide_with_each_other(self):
        # Multiple None suffixes on one VM are fine at this layer -- they
        # get distinct random suffixes later, in core/macs.py.
        vm = make_vm(interfaces=ifaces(("control", None), ("storage", None)))
        self.assertEqual(len(vm.interfaces), 2)

    def test_has_gpu_false_by_default(self):
        vm = make_vm()
        self.assertFalse(vm.has_gpu)

    def test_has_gpu_true_with_profile(self):
        vm = make_vm(gpu_profile="H100-MIG-3g.40gb")
        self.assertTrue(vm.has_gpu)

    def test_interface_names_preserves_order(self):
        vm = make_vm(interfaces=ifaces(("storage", "10:00:00"), ("control", "10:00:01")))
        self.assertEqual(vm.interface_names(), ["storage", "control"])


class TestMissionSpec(unittest.TestCase):
    def _base_kwargs(self):
        return dict(
            name="Mission-Test",
            networks={"control": 100, "storage": 110},
            vms={"vm1": make_vm(interfaces=ifaces(("control", "10:00:00"), ("storage", "10:00:01")))},
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
        kwargs["vms"] = {"vm1": make_vm(interfaces=ifaces(("control", "10:00:00"), ("does-not-exist", "10:00:01")))}
        with self.assertRaises(ValueError):
            MissionSpec(**kwargs)

    def test_gpu_vm_names(self):
        kwargs = self._base_kwargs()
        kwargs["vms"] = {
            "vm1": make_vm(interfaces=ifaces(("control", "10:00:00"), ("storage", "10:00:01"))),
            "vm2": make_vm(
                name="vm2", interfaces=ifaces(("control", "20:00:00"), ("storage", "20:00:01")),
                gpu_profile="H100-MIG-3g.40gb", type=VMType.PXE, image=None,
            ),
        }
        kwargs["placement"] = {"vm1": "compute01", "vm2": "compute02"}
        mission = MissionSpec(**kwargs)
        self.assertEqual(mission.gpu_vm_names(), ["vm2"])

    def test_file_version_optional_and_retained(self):
        kwargs = self._base_kwargs()
        kwargs["file_version"] = "1.2.3"
        mission = MissionSpec(**kwargs)
        self.assertEqual(mission.file_version, "1.2.3")


class TestHostSpec(unittest.TestCase):
    def test_available_profiles_counts_by_name(self):
        host = HostSpec(
            name="h1", cpus=64, memory_mb=262144,
            gpu_devices=[
                GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1"),
                GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u2"),
                GpuDeviceSpec(profile="L4-vGPU-4Q", mdev_uuid="u3"),
            ],
        )
        self.assertEqual(host.available_profiles(), {"H100-MIG-3g.40gb": 2, "L4-vGPU-4Q": 1})

    def test_available_profiles_empty_when_no_gpus(self):
        host = HostSpec(name="h1", cpus=64, memory_mb=262144)
        self.assertEqual(host.available_profiles(), {})


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
