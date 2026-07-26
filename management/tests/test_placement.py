"""Unit tests for core/placement.py -- capacity + GPU-profile-aware placement validation and the reference greedy bin-packer."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.placement import greedy_place, reserve_gpu_devices, validate_placement
from core.types import GpuDeviceSpec, HostSpec, InterfaceSpec, MissionSpec, VMSpec, VMType


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


def make_host(name, cpus=16, memory_mb=65536, gpu_devices=None):
    return HostSpec(name=name, cpus=cpus, memory_mb=memory_mb, gpu_devices=gpu_devices or [])


class TestValidatePlacement(unittest.TestCase):
    def test_fits_within_capacity(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1")}
        report = validate_placement(mission, hosts)
        self.assertTrue(report.ok)

    def test_cpu_oversubscription_detected(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=64, memory_mb=8192, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", cpus=16)}
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "cpu")

    def test_memory_oversubscription_detected(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=999999, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1")}
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "memory_mb")

    def test_unknown_host_detected(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "ghost-host"})
        hosts = {"h1": make_host("h1")}
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "unknown_host")

    def test_gpu_profile_capacity_respected(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1")])}
        report = validate_placement(mission, hosts)
        self.assertTrue(report.ok)

    def test_gpu_profile_oversubscription_detected(self):
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb"),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb"),
        }
        mission = MissionSpec(name="M", networks={"control": 100}, vms=vms, placement={"vm1": "h1", "vm2": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1")])}
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "gpu_profile:H100-MIG-3g.40gb")

    def test_wrong_gpu_profile_not_satisfied_by_different_profile(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="L4-vGPU-4Q")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1")])}
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "gpu_profile:L4-vGPU-4Q")


class TestGreedyPlace(unittest.TestCase):
    def test_places_within_capacity(self):
        hosts = {"h1": make_host("h1", cpus=8), "h2": make_host("h2", cpus=8)}
        reqs = {"vm1": {"cpu": 4, "memory_mb": 1024, "gpu_profile": None}, "vm2": {"cpu": 4, "memory_mb": 1024, "gpu_profile": None}}
        placement = greedy_place(["vm1", "vm2"], reqs, hosts)
        self.assertEqual(set(placement.keys()), {"vm1", "vm2"})
        self.assertIn(placement["vm1"], hosts)

    def test_raises_when_infeasible(self):
        hosts = {"h1": make_host("h1", cpus=8)}
        reqs = {"vm1": {"cpu": 64, "memory_mb": 1024, "gpu_profile": None}}
        with self.assertRaises(RuntimeError):
            greedy_place(["vm1"], reqs, hosts)

    def test_gpu_vm_only_placed_on_host_with_matching_profile(self):
        hosts = {
            "h1": make_host("h1", cpus=64, gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="u1")]),
            "h2": make_host("h2", cpus=64),
        }
        reqs = {"vm1": {"cpu": 4, "memory_mb": 1024, "gpu_profile": "H100-MIG-3g.40gb"}}
        placement = greedy_place(["vm1"], reqs, hosts)
        self.assertEqual(placement["vm1"], "h1")

    def test_two_gpu_vms_exhaust_slices_third_fails(self):
        hosts = {"h1": make_host("h1", cpus=64, gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1"), GpuDeviceSpec(profile="X", mdev_uuid="u2")])}
        reqs = {n: {"cpu": 1, "memory_mb": 1024, "gpu_profile": "X"} for n in ["vm1", "vm2", "vm3"]}
        with self.assertRaises(RuntimeError):
            greedy_place(["vm1", "vm2", "vm3"], reqs, hosts)


class TestReserveGpuDevices(unittest.TestCase):
    def test_reserves_matching_free_device(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1")])}
        result = reserve_gpu_devices(mission, hosts, already_reserved_uuids=set())
        self.assertEqual(result, {"vm1": "u1"})

    def test_non_gpu_vms_absent_from_result(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1")}
        result = reserve_gpu_devices(mission, hosts, already_reserved_uuids=set())
        self.assertEqual(result, {})

    def test_skips_uuids_already_reserved_by_other_active_deployments(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1"), GpuDeviceSpec(profile="X", mdev_uuid="u2")])}
        result = reserve_gpu_devices(mission, hosts, already_reserved_uuids={"u1"})
        self.assertEqual(result, {"vm1": "u2"})

    def test_raises_when_all_matching_devices_already_reserved(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1")])}
        with self.assertRaises(RuntimeError):
            reserve_gpu_devices(mission, hosts, already_reserved_uuids={"u1"})

    def test_two_gpu_vms_in_same_mission_get_distinct_devices(self):
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X"),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X"),
        }
        mission = MissionSpec(name="M", networks={"control": 100}, vms=vms, placement={"vm1": "h1", "vm2": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1"), GpuDeviceSpec(profile="X", mdev_uuid="u2")])}
        result = reserve_gpu_devices(mission, hosts, already_reserved_uuids=set())
        self.assertEqual(set(result.values()), {"u1", "u2"})

    def test_raises_when_second_gpu_vm_in_same_mission_has_no_device_left(self):
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X"),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), gpu_profile="X"),
        }
        mission = MissionSpec(name="M", networks={"control": 100}, vms=vms, placement={"vm1": "h1", "vm2": "h1"})
        hosts = {"h1": make_host("h1", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="u1")])}
        with self.assertRaises(RuntimeError):
            reserve_gpu_devices(mission, hosts, already_reserved_uuids=set())


if __name__ == "__main__":
    unittest.main()
