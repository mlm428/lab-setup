"""Unit tests for services/cluster_health.py -- infrastructure scan logic (mocked at the clients.* boundary)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import HostSpec, MissionState
from services import cluster_health


def make_hosts():
    return {
        "h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local"),
        "h2": HostSpec(name="h2", cpus=64, memory_mb=262144, address="h2.cluster.local"),
    }


class TestCheckHost(unittest.TestCase):
    def test_reachable_and_ok(self):
        host = HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")
        with patch("services.cluster_health.libvirt_client.connect") as m_connect, \
             patch("services.cluster_health.libvirt_client.list_domain_names", return_value=["vm1", "vm2"]):
            m_connect.return_value = MagicMock()
            result = cluster_health.check_host(host)
        self.assertTrue(result["reachable"])
        self.assertTrue(result["libvirt_ok"])
        self.assertEqual(result["vm_count"], 2)

    def test_connect_failure_marks_unreachable(self):
        host = HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")
        with patch("services.cluster_health.libvirt_client.connect", side_effect=RuntimeError("no route to host")):
            result = cluster_health.check_host(host)
        self.assertFalse(result["reachable"])
        self.assertIsNone(result["libvirt_ok"])

    def test_query_failure_after_connect_marks_libvirt_not_ok_but_reachable(self):
        host = HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")
        with patch("services.cluster_health.libvirt_client.connect") as m_connect, \
             patch("services.cluster_health.libvirt_client.list_domain_names", side_effect=RuntimeError("query failed")):
            m_connect.return_value = MagicMock()
            result = cluster_health.check_host(host)
        self.assertTrue(result["reachable"])
        self.assertFalse(result["libvirt_ok"])


class TestCheckOvnAndCeph(unittest.TestCase):
    def test_ovn_reachable(self):
        with patch("services.cluster_health.ovn_client.connect", return_value=MagicMock()), \
             patch("services.cluster_health.ovn_client.list_logical_switches", return_value=["sw1"]):
            ok, detail = cluster_health.check_ovn("tcp:host:6641")
        self.assertTrue(ok)

    def test_ovn_unreachable(self):
        with patch("services.cluster_health.ovn_client.connect", side_effect=RuntimeError("connection refused")):
            ok, detail = cluster_health.check_ovn("tcp:host:6641")
        self.assertFalse(ok)
        self.assertIn("connection refused", detail)

    def test_ceph_reachable(self):
        with patch("services.cluster_health.ceph_client.get_fsid", return_value="fsid-123"):
            ok, detail = cluster_health.check_ceph("/etc/ceph/ceph.conf")
        self.assertTrue(ok)
        self.assertEqual(detail, "fsid-123")

    def test_ceph_unreachable(self):
        with patch("services.cluster_health.ceph_client.get_fsid", side_effect=RuntimeError("timeout")):
            ok, detail = cluster_health.check_ceph("/etc/ceph/ceph.conf")
        self.assertFalse(ok)


class TestScanCluster(unittest.TestCase):
    def setUp(self):
        self.store_patch = patch("services.cluster_health.store", MissionStore())
        self.mock_store = self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def test_all_healthy_rolls_up_ok_true(self):
        with patch("services.cluster_health.check_host", return_value={"host": "h1", "reachable": True, "libvirt_ok": True, "vm_count": 0, "detail": ""}), \
             patch("services.cluster_health.check_ovn", return_value=(True, "")), \
             patch("services.cluster_health.check_ceph", return_value=(True, "fsid-1")):
            result = cluster_health.scan_cluster(make_hosts(), "tcp:host:6641", "/etc/ceph/ceph.conf")
        self.assertTrue(result["ok"])
        self.assertTrue(result["ovn_reachable"])
        self.assertTrue(result["ceph_reachable"])
        self.assertEqual(len(result["hosts"]), 2)

    def test_one_host_down_marks_overall_not_ok(self):
        def fake_check(host, ssh_user="root"):
            if host.name == "h1":
                return {"host": "h1", "reachable": False, "libvirt_ok": None, "vm_count": None, "detail": "down"}
            return {"host": host.name, "reachable": True, "libvirt_ok": True, "vm_count": 0, "detail": ""}

        with patch("services.cluster_health.check_host", side_effect=fake_check), \
             patch("services.cluster_health.check_ovn", return_value=(True, "")), \
             patch("services.cluster_health.check_ceph", return_value=(True, "fsid-1")):
            result = cluster_health.scan_cluster(make_hosts(), "tcp:host:6641", "/etc/ceph/ceph.conf")
        self.assertFalse(result["ok"])
        self.assertIn("h1", result["detail"])

    def test_ovn_down_marks_overall_not_ok(self):
        with patch("services.cluster_health.check_host", return_value={"host": "h1", "reachable": True, "libvirt_ok": True, "vm_count": 0, "detail": ""}), \
             patch("services.cluster_health.check_ovn", return_value=(False, "unreachable")), \
             patch("services.cluster_health.check_ceph", return_value=(True, "fsid-1")):
            result = cluster_health.scan_cluster(make_hosts(), "tcp:host:6641", "/etc/ceph/ceph.conf")
        self.assertFalse(result["ok"])

    def test_ceph_check_skipped_when_conf_path_none(self):
        with patch("services.cluster_health.check_host", return_value={"host": "h1", "reachable": True, "libvirt_ok": True, "vm_count": 0, "detail": ""}), \
             patch("services.cluster_health.check_ovn", return_value=(True, "")), \
             patch("services.cluster_health.check_ceph") as m_ceph:
            result = cluster_health.scan_cluster(make_hosts(), "tcp:host:6641", None)
        m_ceph.assert_not_called()
        self.assertIsNone(result["ceph_reachable"])
        self.assertTrue(result["ok"])

    def test_missions_rolled_up_by_state(self):
        from core.types import InterfaceSpec, MissionSpec, VMSpec, VMType
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces={"control": InterfaceSpec("control", "10:00:00")})
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        status1 = self.mock_store.create("M", mission)
        self.mock_store.update_state(status1.mission_id, MissionState.RUNNING)
        status2 = self.mock_store.create("M", mission)  # stays Pending

        with patch("services.cluster_health.check_host", return_value={"host": "h1", "reachable": True, "libvirt_ok": True, "vm_count": 0, "detail": ""}), \
             patch("services.cluster_health.check_ovn", return_value=(True, "")):
            result = cluster_health.scan_cluster(make_hosts(), "tcp:host:6641", None)

        self.assertEqual(result["missions_total"], 2)
        self.assertEqual(result["missions_by_state"], {"Running": 1, "Pending": 1})


if __name__ == "__main__":
    unittest.main()
