from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.placement import greedy_place, validate_placement
from core.types import HostSpec, MissionSpec, VMSpec, VMType


def make_mission(vms: dict, placement: dict, networks=None):
    return MissionSpec(
        name="Mission-Test",
        networks=networks or {"control": 100},
        vms=vms,
        placement=placement,
    )


class TestValidatePlacement(unittest.TestCase):
    def test_within_capacity_passes(self):
        hosts = {"h1": HostSpec(name="h1", cpus=16, memory_mb=32768, gpus=1)}
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=8, memory_mb=16384, interfaces=["control"])
        mission = make_mission({"vm1": vm}, {"vm1": "h1"})
        report = validate_placement(mission, hosts)
        self.assertTrue(report.ok)

    def test_cpu_oversubscription_detected(self):
        hosts = {"h1": HostSpec(name="h1", cpus=4, memory_mb=32768, gpus=0)}
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=8, memory_mb=16384, interfaces=["control"])
        mission = make_mission({"vm1": vm}, {"vm1": "h1"})
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "cpu")

    def test_memory_oversubscription_detected(self):
        hosts = {"h1": HostSpec(name="h1", cpus=16, memory_mb=8192, gpus=0)}
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=16384, interfaces=["control"])
        mission = make_mission({"vm1": vm}, {"vm1": "h1"})
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "memory_mb")

    def test_gpu_oversubscription_detected(self):
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, gpus=1)}
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"], gpu=True),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"], gpu=True),
        }
        mission = make_mission(vms, {"vm1": "h1", "vm2": "h1"})
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "gpus")

    def test_unknown_host_detected(self):
        hosts = {"h1": HostSpec(name="h1", cpus=16, memory_mb=32768, gpus=0)}
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"])
        mission = make_mission({"vm1": vm}, {"vm1": "h-does-not-exist"})
        report = validate_placement(mission, hosts)
        self.assertFalse(report.ok)
        self.assertEqual(report.violations[0].resource, "unknown_host")

    def test_multiple_vms_sum_correctly_on_shared_host(self):
        hosts = {"h1": HostSpec(name="h1", cpus=10, memory_mb=32768, gpus=0)}
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"]),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"]),
        }
        mission = make_mission(vms, {"vm1": "h1", "vm2": "h1"})
        self.assertTrue(validate_placement(mission, hosts).ok)  # 8 <= 10

        vms["vm3"] = VMSpec(name="vm3", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=["control"])
        mission2 = make_mission(vms, {"vm1": "h1", "vm2": "h1", "vm3": "h1"})
        self.assertFalse(validate_placement(mission2, hosts).ok)  # 12 > 10


class TestGreedyPlace(unittest.TestCase):
    def test_places_all_vms_within_capacity(self):
        hosts = {
            "h1": HostSpec(name="h1", cpus=16, memory_mb=32768, gpus=1),
            "h2": HostSpec(name="h2", cpus=16, memory_mb=32768, gpus=0),
        }
        vm_names = ["gpu-vm", "plain-vm"]
        requirements = {
            "gpu-vm": {"cpu": 4, "memory_mb": 8192, "gpu": True},
            "plain-vm": {"cpu": 4, "memory_mb": 8192, "gpu": False},
        }
        placement = greedy_place(vm_names, requirements, hosts)
        self.assertEqual(placement["gpu-vm"], "h1")  # only h1 has a GPU
        self.assertIn(placement["plain-vm"], ["h1", "h2"])

    def test_raises_when_infeasible(self):
        hosts = {"h1": HostSpec(name="h1", cpus=2, memory_mb=4096, gpus=0)}
        vm_names = ["big-vm"]
        requirements = {"big-vm": {"cpu": 8, "memory_mb": 4096, "gpu": False}}
        with self.assertRaises(RuntimeError):
            greedy_place(vm_names, requirements, hosts)

    def test_spreads_load_across_hosts(self):
        hosts = {
            "h1": HostSpec(name="h1", cpus=8, memory_mb=16384, gpus=0),
            "h2": HostSpec(name="h2", cpus=8, memory_mb=16384, gpus=0),
        }
        vm_names = [f"vm{i}" for i in range(4)]
        requirements = {name: {"cpu": 4, "memory_mb": 4096, "gpu": False} for name in vm_names}
        placement = greedy_place(vm_names, requirements, hosts)
        counts = {"h1": 0, "h2": 0}
        for host in placement.values():
            counts[host] += 1
        self.assertEqual(counts["h1"], 2)
        self.assertEqual(counts["h2"], 2)


if __name__ == "__main__":
    unittest.main()
