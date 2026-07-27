"""Unit tests for services/compute.py and services/networking.py -- the thin glue between core/ (pure logic) and clients/ (real libvirt/OVN calls), mocked at the clients boundary."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import InterfaceSpec, MissionSpec, VMSpec, VMType
from core.xml_render import StorageContext
from services import compute, networking


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


class TestComputeService(unittest.TestCase):
    def test_define_and_start_vm_renders_and_calls_libvirt_client(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        storage = StorageContext(backend="local_qcow2")

        with mock.patch.object(compute, "libvirt_client") as m_client:
            m_conn = mock.MagicMock()
            m_client.connect.return_value = m_conn
            compute.define_and_start_vm(mission, "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], "h1.cluster.local", storage, gpu_mdev_uuid=None)

        m_client.connect.assert_called_once_with("h1.cluster.local", ssh_user="root")
        m_client.define_and_start.assert_called_once()
        domain_xml_arg = m_client.define_and_start.call_args[0][1]
        self.assertIn("<name>vm1</name>", domain_xml_arg)
        m_conn.close.assert_called_once()

    def test_define_and_start_vm_closes_connection_even_on_failure(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=2, memory_mb=4096, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        storage = StorageContext(backend="local_qcow2")

        with mock.patch.object(compute, "libvirt_client") as m_client:
            m_conn = mock.MagicMock()
            m_client.connect.return_value = m_conn
            m_client.define_and_start.side_effect = RuntimeError("libvirt error")
            with self.assertRaises(RuntimeError):
                compute.define_and_start_vm(mission, "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], "h1.cluster.local", storage, gpu_mdev_uuid=None)

        m_conn.close.assert_called_once()

    def test_destroy_vm_calls_libvirt_client_and_closes(self):
        with mock.patch.object(compute, "libvirt_client") as m_client:
            m_conn = mock.MagicMock()
            m_client.connect.return_value = m_conn
            compute.destroy_vm("vm1", "h1.cluster.local")
        m_client.destroy_and_undefine.assert_called_once_with(m_conn, "vm1")
        m_conn.close.assert_called_once()

    def test_list_running_vm_names_aggregates_across_hosts(self):
        with mock.patch.object(compute, "libvirt_client") as m_client:
            m_client.connect.return_value = mock.MagicMock()
            m_client.list_domain_names.side_effect = [["vm1", "vm2"], ["vm3"]]
            names = compute.list_running_vm_names(["h1", "h2"])
        self.assertEqual(names, ["vm1", "vm2", "vm3"])


class TestNetworkingService(unittest.TestCase):
    def test_provision_networks_creates_switch_per_network_and_router(self):
        mission = MissionSpec(
            name="M", networks={"control": 100, "storage": 110},
            vms={"vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))},
            placement={"vm1": "h1"},
        )
        with mock.patch.object(networking, "ovn_client") as m_ovn:
            switches = networking.provision_networks(mock.MagicMock(), "dep-1", mission)

        self.assertEqual(m_ovn.ensure_logical_switch.call_count, 2)
        m_ovn.ensure_logical_router.assert_called_once_with(mock.ANY, "M-dep-1-router")
        self.assertEqual(switches, {"control": "M-dep-1-control", "storage": "M-dep-1-storage"})

    def test_add_vm_ports_uses_resolved_macs_in_interface_order(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control", "storage"))
        mission = MissionSpec(name="M", networks={"control": 100, "storage": 110}, vms={"vm1": vm}, placement={"vm1": "h1"})
        resolved_macs = {"vm1": ["aa:00", "aa:01"]}

        with mock.patch.object(networking, "ovn_client") as m_ovn:
            networking.add_vm_ports(mock.MagicMock(), "dep-1", mission, resolved_macs)

        calls = m_ovn.add_vm_port.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[1:], ("M-dep-1-control", "M-dep-1-vm1-control", "aa:00"))
        self.assertEqual(calls[1].args[1:], ("M-dep-1-storage", "M-dep-1-vm1-storage", "aa:01"))

    def test_two_deployments_of_same_mission_get_distinct_switches(self):
        # Regression test for the exact bug this review caught.
        mission = MissionSpec(
            name="M", networks={"control": 100},
            vms={"vm1": VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))},
            placement={"vm1": "h1"},
        )
        with mock.patch.object(networking, "ovn_client") as m_ovn:
            switches1 = networking.provision_networks(mock.MagicMock(), "dep-1", mission)
            switches2 = networking.provision_networks(mock.MagicMock(), "dep-2", mission)

        self.assertNotEqual(switches1["control"], switches2["control"])

    def test_teardown_removes_ports_switches_and_router(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})

        with mock.patch.object(networking, "ovn_client") as m_ovn:
            networking.teardown_mission_networking(mock.MagicMock(), "dep-1", mission)

        m_ovn.remove_vm_port.assert_called_once_with(mock.ANY, "M-dep-1-vm1-control")
        m_ovn.remove_logical_switch.assert_called_once_with(mock.ANY, "M-dep-1-control")
        m_ovn.remove_logical_router.assert_called_once_with(mock.ANY, "M-dep-1-router")


if __name__ == "__main__":
    unittest.main()
