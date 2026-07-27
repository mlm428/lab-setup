"""Unit tests for core/xml_render.py -- libvirt domain XML generation, including mdev-based GPU passthrough."""
from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import InterfaceSpec, VMSpec, VMType
from core.xml_render import (
    StorageContext,
    deterministic_vm_uuid,
    port_name,
    render_domain_xml,
    router_name,
    router_port_name,
    switch_name,
)


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


CEPH_STORAGE = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime", ceph_client_id="libvirt", ceph_secret_uuid="uuid-1", ceph_monitors=[{"name": "mon1", "port": 6789}])
LOCAL_STORAGE = StorageContext(backend="local_qcow2", local_qcow2_dir="/var/lib/libvirt/images")


class TestNamingHelpers(unittest.TestCase):
    def test_switch_name_scoped_by_mission_and_deployment(self):
        self.assertEqual(switch_name("Mission-Alpha", "dep-1", "control"), "Mission-Alpha-dep-1-control")

    def test_router_name_scoped_by_mission_and_deployment(self):
        self.assertEqual(router_name("Mission-Alpha", "dep-1"), "Mission-Alpha-dep-1-router")

    def test_router_port_name_scoped_by_mission_deployment_and_network(self):
        self.assertEqual(router_port_name("Mission-Alpha", "dep-1", "control"), "Mission-Alpha-dep-1-control-rp")

    def test_port_name_scoped_by_mission_deployment_vm_network(self):
        self.assertEqual(port_name("Mission-Alpha", "dep-1", "db01", "control"), "Mission-Alpha-dep-1-db01-control")

    def test_two_deployments_of_same_mission_name_get_distinct_ovn_names(self):
        # Regression test for the exact bug this review caught: two
        # deployments sharing the same mission.name must NOT collide on
        # any OVN object name, or the second deployment's ls_add(...,
        # may_exist=True) would silently reuse the first deployment's
        # switch instead of creating its own -- breaking the "totally
        # network isolated" requirement.
        self.assertNotEqual(switch_name("Mission-Alpha", "dep-1", "control"), switch_name("Mission-Alpha", "dep-2", "control"))
        self.assertNotEqual(router_name("Mission-Alpha", "dep-1"), router_name("Mission-Alpha", "dep-2"))
        self.assertNotEqual(router_port_name("Mission-Alpha", "dep-1", "control"), router_port_name("Mission-Alpha", "dep-2", "control"))
        self.assertNotEqual(port_name("Mission-Alpha", "dep-1", "db01", "control"), port_name("Mission-Alpha", "dep-2", "db01", "control"))

    def test_deterministic_uuid_stable(self):
        self.assertEqual(deterministic_vm_uuid("M", "vm1"), deterministic_vm_uuid("M", "vm1"))

    def test_deterministic_uuid_differs_by_vm(self):
        self.assertNotEqual(deterministic_vm_uuid("M", "vm1"), deterministic_vm_uuid("M", "vm2"))

    def test_deterministic_uuid_differs_by_mission(self):
        self.assertNotEqual(deterministic_vm_uuid("M1", "vm1"), deterministic_vm_uuid("M2", "vm1"))


class TestRenderDomainXml(unittest.TestCase):
    def test_pxe_vm_has_no_disk(self):
        vm = VMSpec(name="worker01", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"))
        xml_str = render_domain_xml("M", "test-mission-id", "worker01", vm, ["52:54:00:10:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        self.assertEqual(len(root.findall("./devices/disk")), 0)
        self.assertEqual(root.find("./os/boot").get("dev"), "network")

    def test_linked_clone_vm_has_disk_ceph(self):
        vm = VMSpec(name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), image="golden")
        xml_str = render_domain_xml("M", "test-mission-id", "db01", vm, ["52:54:00:10:00:00"], CEPH_STORAGE)
        root = ET.fromstring(xml_str)
        disks = root.findall("./devices/disk")
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].find("source").get("protocol"), "rbd")

    def test_linked_clone_vm_has_disk_local_qcow2(self):
        vm = VMSpec(name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), image="golden")
        xml_str = render_domain_xml("M", "test-mission-id", "db01", vm, ["52:54:00:10:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        disks = root.findall("./devices/disk")
        self.assertEqual(len(disks), 1)
        self.assertIn("db01_clone.qcow2", disks[0].find("source").get("file"))

    def test_nic_count_and_macs_match_interfaces(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control", "storage", "sensor"))
        macs = ["aa:bb:cc:00:00:00", "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
        xml_str = render_domain_xml("M", "test-mission-id", "vm1", vm, macs, LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        interfaces = root.findall("./devices/interface")
        self.assertEqual(len(interfaces), 3)
        found_macs = [i.find("mac").get("address") for i in interfaces]
        self.assertEqual(found_macs, macs)

    def test_wrong_mac_count_raises(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control", "storage"))
        with self.assertRaises(ValueError):
            render_domain_xml("M", "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE)

    def test_no_gpu_vm_has_no_hostdev(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        xml_str = render_domain_xml("M", "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        self.assertEqual(len(root.findall("./devices/hostdev")), 0)

    def test_gpu_vm_gets_mdev_hostdev(self):
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        xml_str = render_domain_xml("M", "test-mission-id", "render01", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE, gpu_mdev_uuid="uuid-1234")
        root = ET.fromstring(xml_str)
        hostdevs = root.findall("./devices/hostdev")
        self.assertEqual(len(hostdevs), 1)
        self.assertEqual(hostdevs[0].get("type"), "mdev")
        self.assertEqual(hostdevs[0].get("model"), "vfio-pci")
        self.assertEqual(hostdevs[0].find("./source/address").get("uuid"), "uuid-1234")

    def test_gpu_vm_without_mdev_uuid_raises(self):
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        with self.assertRaises(ValueError):
            render_domain_xml("M", "test-mission-id", "render01", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE, gpu_mdev_uuid=None)

    def test_non_gpu_vm_with_mdev_uuid_raises(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        with self.assertRaises(ValueError):
            render_domain_xml("M", "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE, gpu_mdev_uuid="uuid-1234")

    def test_cpu_and_memory_reflected(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=6, memory_mb=12288, interfaces=ifaces("control"))
        xml_str = render_domain_xml("M", "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        self.assertEqual(int(root.find("./vcpu").text), 6)
        self.assertEqual(int(root.find("./memory").text), 12288)

    def test_interface_uses_ovn_virtualport_with_scoped_port_name(self):
        vm = VMSpec(name="db01", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        xml_str = render_domain_xml("Mission-Alpha", "test-mission-id", "db01", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        interface_id = root.find("./devices/interface/virtualport/parameters").get("interfaceid")
        self.assertEqual(interface_id, "Mission-Alpha-test-mission-id-db01-control")

    def test_well_formed_xml_output(self):
        vm = VMSpec(name="vm1", type=VMType.LINKED_CLONE, cpu=1, memory_mb=1024, interfaces=ifaces("control"), image="golden")
        xml_str = render_domain_xml("M", "test-mission-id", "vm1", vm, ["aa:bb:cc:00:00:00"], CEPH_STORAGE)
        ET.fromstring(xml_str)  # raises if malformed

    def test_metadata_block_embeds_mission_and_vm_identity(self):
        vm = VMSpec(name="db01", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        xml_str = render_domain_xml("Mission-Alpha", "deployment-xyz", "db01", vm, ["aa:bb:cc:00:00:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        ns = {"mission": "https://github.com/anthropics/mission-cluster/metadata"}
        info = root.find("./metadata/mission:info", ns)
        self.assertIsNotNone(info)
        self.assertEqual(info.find("mission:id", ns).text, "deployment-xyz")
        self.assertEqual(info.find("mission:name", ns).text, "Mission-Alpha")
        self.assertEqual(info.find("mission:vm", ns).text, "db01")


if __name__ == "__main__":
    unittest.main()
