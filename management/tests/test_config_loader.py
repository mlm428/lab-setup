"""Unit tests for core/config_loader.py -- loading the consolidated top-level config/ directory."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import config_loader

REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class TestConfigLoader(unittest.TestCase):
    def test_config_dir_points_at_repo_root_config(self):
        self.assertEqual(config_loader.CONFIG_DIR, REPO_CONFIG_DIR)
        self.assertTrue(config_loader.CONFIG_DIR.is_dir())

    def test_load_hosts_config_has_expected_top_level_keys(self):
        data = config_loader.load_hosts_config()
        self.assertIn("hosts", data)
        self.assertIn("ovn_central", data)
        self.assertIn("ceph_monitors", data)

    def test_load_hosts_config_gpu_devices_present(self):
        data = config_loader.load_hosts_config()
        compute01 = data["hosts"]["compute01"]
        self.assertGreater(len(compute01["gpu_devices"]), 0)
        for dev in compute01["gpu_devices"]:
            self.assertIn("profile", dev)
            self.assertIn("mdev_uuid", dev)

    def test_load_cluster_config_has_packages(self):
        data = config_loader.load_cluster_config()
        self.assertIn("packages", data)
        self.assertIn("services", data)

    def test_load_storage_config_has_separate_runtime_and_golden(self):
        data = config_loader.load_storage_config()
        self.assertIn("runtime", data)
        self.assertIn("golden", data)
        # The key requirement under test: runtime and golden are
        # independently configured pools/sources, not one shared section.
        self.assertNotEqual(data["runtime"].get("ceph", {}).get("pool"), data["golden"]["images"])

    def test_load_networks_config(self):
        data = config_loader.load_networks_config()
        self.assertIn("networks", data)
        self.assertIsInstance(data["networks"]["control"], int)

    def test_only_one_copy_of_config_exists(self):
        # Regression guard for the duplication the operator flagged
        # (bootstrap/config/ and management/inventory/ used to both exist
        # with hand-synced copies of this same data).
        repo_root = REPO_CONFIG_DIR.parent
        self.assertFalse((repo_root / "bootstrap" / "config").exists())
        self.assertFalse((repo_root / "management" / "inventory").exists())
        self.assertFalse((repo_root / "management" / "placement").exists())


if __name__ == "__main__":
    unittest.main()
