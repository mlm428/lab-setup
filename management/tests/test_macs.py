"""
Unit tests for core/macs.py. Notably includes a regression test for the
collision bug this project actually hit while generating the 40-VM
proof-of-concept mission (see macs.py's module docstring).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.macs import assign_all_macs, compute_vm_indices, generate_mac, validate_no_duplicate_macs


class TestComputeVmIndices(unittest.TestCase):
    def test_indices_are_unique_and_order_independent(self):
        names_a = ["zebra", "apple", "mango"]
        names_b = ["mango", "zebra", "apple"]  # same set, different order
        idx_a = compute_vm_indices(names_a)
        idx_b = compute_vm_indices(names_b)
        self.assertEqual(idx_a, idx_b)
        self.assertEqual(sorted(idx_a.values()), [0, 1, 2])

    def test_rejects_too_many_vms(self):
        names = [f"vm{i}" for i in range(300)]
        with self.assertRaises(ValueError):
            compute_vm_indices(names)


class TestGenerateMac(unittest.TestCase):
    def test_format(self):
        mac = generate_mac("Mission-Alpha", 3, 5)
        self.assertRegex(mac, r"^52:54:00:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")
        self.assertTrue(mac.endswith(":05"))

    def test_deterministic_across_calls(self):
        self.assertEqual(
            generate_mac("Mission-Alpha", 3, 5),
            generate_mac("Mission-Alpha", 3, 5),
        )

    def test_different_missions_differ(self):
        self.assertNotEqual(
            generate_mac("Mission-Alpha", 3, 5),
            generate_mac("Mission-Bravo", 3, 5),
        )

    def test_rejects_out_of_range_vm_index(self):
        with self.assertRaises(ValueError):
            generate_mac("Mission-Alpha", 256, 0)


class TestAssignAllMacsNoCollisions(unittest.TestCase):
    def test_40_vm_mission_no_duplicate_macs(self):
        """
        Regression test: an earlier version of core/macs.py hashed the VM
        name into a single byte, which produced duplicate MAC sets once
        applied to the 40-VM proof-of-concept mission (birthday-paradox
        collision at n=40 over a 256-value space). This reproduces that
        exact scenario (40 VMs, 6 interfaces each) and asserts zero
        duplicates.
        """
        mission_vms = {f"vm{i:02d}": ["control", "storage", "operations", "sensor", "external", "management"] for i in range(40)}
        assigned = assign_all_macs("Mission-Alpha", mission_vms)
        all_macs = [m for macs in assigned.values() for m in macs]
        self.assertEqual(len(all_macs), 240)
        self.assertEqual(validate_no_duplicate_macs(all_macs), [])

    def test_reproducible_across_repeated_calls(self):
        mission_vms = {f"vm{i:02d}": ["control", "storage"] for i in range(10)}
        first = assign_all_macs("Mission-Alpha", mission_vms)
        second = assign_all_macs("Mission-Alpha", mission_vms)
        self.assertEqual(first, second)

    def test_explicit_override_is_honored(self):
        mission_vms = {"pxe01": ["control", "storage"]}
        overrides = {"pxe01": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]}
        assigned = assign_all_macs("Mission-Alpha", mission_vms, overrides)
        self.assertEqual(assigned["pxe01"], overrides["pxe01"])

    def test_override_length_mismatch_raises(self):
        mission_vms = {"pxe01": ["control", "storage"]}
        overrides = {"pxe01": ["aa:bb:cc:dd:ee:01"]}  # only 1, needs 2
        with self.assertRaises(ValueError):
            assign_all_macs("Mission-Alpha", mission_vms, overrides)

    def test_only_overridden_vm_is_pinned_others_generated(self):
        mission_vms = {
            "pxe01": ["control", "storage"],
            "pxe02": ["control", "storage"],
        }
        overrides = {"pxe01": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]}
        assigned = assign_all_macs("Mission-Alpha", mission_vms, overrides)
        self.assertEqual(assigned["pxe01"], overrides["pxe01"])
        self.assertTrue(assigned["pxe02"][0].startswith("52:54:00:"))


if __name__ == "__main__":
    unittest.main()
