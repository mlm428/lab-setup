"""
Unit tests for core/macs.py -- the per-deployment MAC prefix + per-
interface suffix resolution scheme.
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.macs import random_prefix, random_suffix, resolve_mission_macs, validate_no_duplicate_macs
from core.types import InterfaceSpec, MissionSpec, VMSpec, VMType


def ifaces(*names_and_suffixes):
    return {name: InterfaceSpec(network=name, mac_suffix=suffix) for name, suffix in names_and_suffixes}


class TestRandomPrefix(unittest.TestCase):
    def test_format(self):
        prefix = random_prefix(set(), rng=random.Random(1))
        self.assertRegex(prefix, r"^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")

    def test_locally_administered_unicast_bit_pattern(self):
        for seed in range(50):
            prefix = random_prefix(set(), rng=random.Random(seed))
            first_octet = int(prefix.split(":")[0], 16)
            self.assertEqual(first_octet & 0b10, 0b10, "locally-administered bit must be set")
            self.assertEqual(first_octet & 0b01, 0, "multicast bit must be clear (must be a unicast address)")

    def test_avoids_existing_prefixes(self):
        rng = random.Random(7)
        first = random_prefix(set(), rng=rng)
        second = random_prefix({first}, rng=rng)
        self.assertNotEqual(first, second)

    def test_raises_when_space_exhausted(self):
        # Force every possible output to already be "in use" by using a
        # tiny max_attempts and a set containing whatever the rng would
        # produce -- simpler: just assert it eventually raises given an
        # absurdly small attempt budget with a guaranteed-colliding set.
        rng = random.Random(3)
        used = set()
        # Prime `used` with a handful of prefixes this rng will produce,
        # then force max_attempts=1 so on a repeat draw it must raise.
        for _ in range(5):
            used.add(random_prefix(set(), rng=random.Random(3)))
        with self.assertRaises(RuntimeError):
            random_prefix(used, rng=random.Random(3), max_attempts=1)


class TestRandomSuffix(unittest.TestCase):
    def test_format(self):
        suffix = random_suffix(set(), rng=random.Random(1))
        self.assertRegex(suffix, r"^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")

    def test_avoids_existing_suffixes(self):
        rng = random.Random(9)
        first = random_suffix(set(), rng=rng)
        second = random_suffix({first}, rng=rng)
        self.assertNotEqual(first, second)


def make_mission(name="Mission-Test", vms=None, networks=None, placement=None):
    vms = vms or {
        "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces(("control", "10:00:00"), ("storage", None))),
    }
    return MissionSpec(
        name=name,
        networks=networks or {"control": 100, "storage": 110},
        vms=vms,
        placement=placement or {n: "compute01" for n in vms},
    )


class TestResolveMissionMacs(unittest.TestCase):
    def test_configured_suffix_used_verbatim(self):
        mission = make_mission()
        prefix, macs = resolve_mission_macs(mission, set(), rng=random.Random(1))
        self.assertTrue(macs["vm1"][0].endswith(":10:00:00"))
        self.assertTrue(macs["vm1"][0].startswith(prefix))

    def test_unconfigured_suffix_gets_random_value(self):
        mission = make_mission()
        prefix, macs = resolve_mission_macs(mission, set(), rng=random.Random(1))
        # vm1's 2nd interface ("storage") has mac_suffix=None -> random
        self.assertNotEqual(macs["vm1"][1].split(":", 3)[3:], ["10", "00", "01"])
        self.assertTrue(macs["vm1"][1].startswith(prefix))

    def test_same_prefix_applied_to_every_vm_in_deployment(self):
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces(("control", "10:00:00"))),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces(("control", "10:01:00"))),
        }
        mission = make_mission(vms=vms)
        prefix, macs = resolve_mission_macs(mission, set(), rng=random.Random(2))
        self.assertTrue(macs["vm1"][0].startswith(prefix))
        self.assertTrue(macs["vm2"][0].startswith(prefix))

    def test_two_deployments_get_different_prefixes_but_identical_suffixes(self):
        mission = make_mission()
        prefix1, macs1 = resolve_mission_macs(mission, set(), rng=random.Random(1))
        prefix2, macs2 = resolve_mission_macs(mission, {prefix1}, rng=random.Random(2))
        self.assertNotEqual(prefix1, prefix2)
        suffix1 = macs1["vm1"][0].split(":", 3)[3:]
        suffix2 = macs2["vm1"][0].split(":", 3)[3:]
        self.assertEqual(suffix1, suffix2, "configured suffix must be identical across deployments")
        self.assertNotEqual(macs1["vm1"][0], macs2["vm1"][0], "full MAC must differ (different prefix)")

    def test_duplicate_configured_suffix_across_vms_raises(self):
        vms = {
            "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces(("control", "10:00:00"))),
            "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces(("storage", "10:00:00"))),
        }
        mission = make_mission(vms=vms, networks={"control": 100, "storage": 110})
        with self.assertRaises(ValueError):
            resolve_mission_macs(mission, set(), rng=random.Random(1))

    def test_no_duplicate_full_macs_across_large_mission(self):
        vms = {
            f"vm{i:02d}": VMSpec(
                name=f"vm{i:02d}", type=VMType.PXE, cpu=1, memory_mb=1024,
                interfaces=ifaces(*[(net, f"{i:02x}:{j:02x}:00") for j, net in enumerate(["control", "storage"])]),
            )
            for i in range(40)
        }
        mission = make_mission(vms=vms)
        prefix, macs = resolve_mission_macs(mission, set(), rng=random.Random(5))
        all_macs = [m for vm_macs in macs.values() for m in vm_macs]
        self.assertEqual(len(all_macs), 80)
        self.assertEqual(validate_no_duplicate_macs(all_macs), [])


class TestValidateNoDuplicateMacs(unittest.TestCase):
    def test_finds_duplicates(self):
        result = validate_no_duplicate_macs(["a", "b", "a", "c", "c", "c"])
        self.assertEqual(set(result), {"a", "c"})

    def test_empty_when_all_unique(self):
        self.assertEqual(validate_no_duplicate_macs(["a", "b", "c"]), [])


if __name__ == "__main__":
    unittest.main()
