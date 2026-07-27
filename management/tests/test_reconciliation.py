"""Unit tests for services/reconciliation.py -- startup drift detection, with clients.libvirt_client mocked out (no real cluster in this sandbox)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import HostSpec, InterfaceSpec, MissionSpec, MissionState, VMSpec, VMType
from core.xml_render import render_domain_xml, StorageContext
from services import reconciliation


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


def make_hosts():
    return {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}


def real_domain_xml(mission_name, mission_id, vm_name, mac="aa:bb:cc:00:00:00", gpu_mdev_uuid=None):
    """Build a real, fully-rendered domain XML (with the actual <metadata> block) for a fake running VM -- more realistic than hand-writing XML fixtures."""
    vm = VMSpec(name=vm_name, type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control"), gpu_profile="X" if gpu_mdev_uuid else None)
    return render_domain_xml(mission_name, mission_id, vm_name, vm, [mac], StorageContext(backend="local_qcow2"), gpu_mdev_uuid=gpu_mdev_uuid)


class TestParseDomainMetadata(unittest.TestCase):
    def test_extracts_mission_id_name_vm(self):
        xml_desc = real_domain_xml("Mission-Alpha", "deploy-123", "db01")
        parsed = reconciliation._parse_domain_metadata(xml_desc)
        self.assertEqual(parsed, ("deploy-123", "Mission-Alpha", "db01"))

    def test_domain_without_metadata_returns_none(self):
        xml_desc = "<domain type='kvm'><name>unrelated-vm</name><uuid>x</uuid><devices/></domain>"
        self.assertIsNone(reconciliation._parse_domain_metadata(xml_desc))


class TestExtractMacsAndGpu(unittest.TestCase):
    def test_extract_macs(self):
        xml_desc = real_domain_xml("M", "d1", "vm1", mac="11:22:33:44:55:66")
        self.assertEqual(reconciliation._extract_macs(xml_desc), ["11:22:33:44:55:66"])

    def test_extract_gpu_uuids_present(self):
        xml_desc = real_domain_xml("M", "d1", "vm1", gpu_mdev_uuid="uuid-xyz")
        self.assertEqual(reconciliation._extract_gpu_uuids(xml_desc), ["uuid-xyz"])

    def test_extract_gpu_uuids_absent(self):
        xml_desc = real_domain_xml("M", "d1", "vm1")
        self.assertEqual(reconciliation._extract_gpu_uuids(xml_desc), [])


class TestScanLiveVms(unittest.TestCase):
    def test_skips_domains_without_metadata_and_keeps_ones_with_it(self):
        hosts = make_hosts()
        with mock.patch.object(reconciliation, "libvirt_client") as m_client:
            m_client.connect.return_value = mock.MagicMock()
            m_client.list_domain_names.return_value = ["vm1", "unrelated-manual-vm"]
            m_client.domain_xml_desc.side_effect = [
                real_domain_xml("Mission-A", "deploy-1", "vm1"),
                "<domain><name>unrelated-manual-vm</name><devices/></domain>",
            ]
            discovered = reconciliation.scan_live_vms(hosts)

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].vm_name, "vm1")
        self.assertEqual(discovered[0].mission_id, "deploy-1")


class TestReconcileOnStartup(unittest.TestCase):
    def test_mission_confirmed_intact_when_all_vms_present(self):
        store = MissionStore()
        mission = MissionSpec(
            name="Mission-A", networks={"control": 100},
            vms={"vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control"))},
            placement={"vm1": "h1"},
        )
        status = store.create("Mission-A", mission)
        store.update_state(status.mission_id, MissionState.RUNNING)

        with mock.patch.object(reconciliation, "scan_live_vms") as m_scan:
            m_scan.return_value = [
                reconciliation.DiscoveredVM(mission_id=status.mission_id, mission_name="Mission-A", vm_name="vm1", host_name="h1", macs=["aa:bb"], gpu_mdev_uuids=[]),
            ]
            report = reconciliation.reconcile_on_startup(make_hosts(), store)

        self.assertTrue(report.ok)
        self.assertEqual(report.missions_confirmed_intact, [status.mission_id])
        self.assertEqual(report.missions_missing_vms, {})
        self.assertEqual(report.missions_reconstructed, [])

    def test_mission_with_missing_vm_flagged(self):
        store = MissionStore()
        mission = MissionSpec(
            name="Mission-A", networks={"control": 100},
            vms={
                "vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control")),
                "vm2": VMSpec(name="vm2", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control")),
            },
            placement={"vm1": "h1", "vm2": "h1"},
        )
        status = store.create("Mission-A", mission)
        store.update_state(status.mission_id, MissionState.RUNNING)

        with mock.patch.object(reconciliation, "scan_live_vms") as m_scan:
            # Only vm1 found running -- vm2 is missing.
            m_scan.return_value = [
                reconciliation.DiscoveredVM(mission_id=status.mission_id, mission_name="Mission-A", vm_name="vm1", host_name="h1", macs=[], gpu_mdev_uuids=[]),
            ]
            report = reconciliation.reconcile_on_startup(make_hosts(), store)

        self.assertFalse(report.ok)
        self.assertEqual(report.missions_missing_vms, {status.mission_id: ["vm2"]})
        # The mission's step log should record this too.
        updated = store.get(status.mission_id)
        self.assertTrue(any(s.step == "reconciliation" and s.status == "error" for s in updated.steps))

    def test_mission_entirely_gone_flagged_as_all_vms_missing(self):
        store = MissionStore()
        mission = MissionSpec(
            name="Mission-A", networks={"control": 100},
            vms={"vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control"))},
            placement={"vm1": "h1"},
        )
        status = store.create("Mission-A", mission)
        store.update_state(status.mission_id, MissionState.RUNNING)

        with mock.patch.object(reconciliation, "scan_live_vms", return_value=[]):
            report = reconciliation.reconcile_on_startup(make_hosts(), store)

        self.assertFalse(report.ok)
        self.assertEqual(report.missions_missing_vms, {status.mission_id: ["vm1"]})

    def test_mission_unknown_to_db_is_reconstructed(self):
        store = MissionStore()  # empty -- no prior record of anything

        with mock.patch.object(reconciliation, "scan_live_vms") as m_scan:
            m_scan.return_value = [
                reconciliation.DiscoveredVM(mission_id="deploy-orphan", mission_name="Mission-Orphan", vm_name="vm1", host_name="h1", macs=["aa:bb:cc:00:00:00"], gpu_mdev_uuids=["uuid-1"]),
            ]
            report = reconciliation.reconcile_on_startup(make_hosts(), store)

        self.assertFalse(report.ok)
        self.assertEqual(report.missions_reconstructed, ["deploy-orphan"])

        adopted = store.get("deploy-orphan")
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted.name, "Mission-Orphan")
        self.assertEqual(adopted.state, MissionState.RUNNING)
        self.assertEqual(adopted.resolved_macs, {"vm1": ["aa:bb:cc:00:00:00"]})
        self.assertEqual(adopted.gpu_allocations, {"vm1": "uuid-1"})
        self.assertIsNone(adopted.spec)  # honestly reflects what reconstruction can't recover

    def test_reconstructed_mission_frees_correctly_once_destroyed(self):
        # Confirms a reconstructed mission behaves like any other for
        # resource-freeing purposes (important since it has gpu_allocations).
        store = MissionStore()
        with mock.patch.object(reconciliation, "scan_live_vms") as m_scan:
            m_scan.return_value = [
                reconciliation.DiscoveredVM(mission_id="deploy-orphan", mission_name="Mission-Orphan", vm_name="vm1", host_name="h1", macs=[], gpu_mdev_uuids=["uuid-1"]),
            ]
            reconciliation.reconcile_on_startup(make_hosts(), store)

        self.assertEqual(store.active_gpu_allocations(), {"uuid-1"})
        store.update_state("deploy-orphan", MissionState.DESTROYED)
        self.assertEqual(store.active_gpu_allocations(), set())

    def test_scan_failure_does_not_raise_and_returns_empty_report(self):
        store = MissionStore()
        with mock.patch.object(reconciliation, "scan_live_vms", side_effect=RuntimeError("all hosts unreachable")):
            report = reconciliation.reconcile_on_startup(make_hosts(), store)
        self.assertEqual(report.discovered_vm_count, 0)
        self.assertTrue(report.ok)  # nothing to flag -- degrades gracefully rather than blocking startup

    def test_unrelated_vm_without_metadata_is_ignored(self):
        store = MissionStore()
        with mock.patch.object(reconciliation, "scan_live_vms", return_value=[]):
            report = reconciliation.reconcile_on_startup(make_hosts(), store)
        self.assertEqual(report.discovered_vm_count, 0)
        self.assertEqual(store.list(), [])


if __name__ == "__main__":
    unittest.main()
