"""
Unit tests for core/xml_render.py -- verifies the generated libvirt domain
XML is well-formed and matches every structural requirement in the design
docs' acceptance-criteria table: exactly 6 NICs with correct static MACs,
correct disk presence/absence by VM type, correct GPU hostdev presence/
absence and PCI address decoding, and correct vCPU/memory values.
"""
from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import VMSpec, VMType
from core.xml_render import (
    StorageContext,
    deterministic_vm_uuid,
    parse_pci_address,
    port_name,
    render_domain_xml,
    router_name,
    switch_name,
)

SIX_NETS = ["control", "storage", "operations", "sensor", "external", "management"]

CEPH_STORAGE = StorageContext(
    backend="ceph_rbd",
    ceph_secret_uuid="5e4b8c1a-9f2d-4e3a-8b7a-1c2d3e4f5a6b",
    ceph_monitors=[{"name": "ceph-mon1", "port": 6789}, {"name": "ceph-mon2", "port": 6789}],
)
LOCAL_STORAGE = StorageContext(backend="local_qcow2")


class TestParsePciAddress(unittest.TestCase):
    def test_full_domain_form(self):
        result = parse_pci_address("0000:83:00.0")
        self.assertEqual(result, {"domain": "0000", "bus": "83", "slot": "00", "function": "0"})

    def test_short_form_without_domain(self):
        result = parse_pci_address("83:00.0")
        self.assertEqual(result["domain"], "0000")
        self.assertEqual(result["bus"], "83")

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            parse_pci_address("not-a-pci-address")


class TestNamingHelpers(unittest.TestCase):
    def test_switch_name_is_mission_scoped(self):
        self.assertNotEqual(
            switch_name("Mission-Alpha", "control"),
            switch_name("Mission-Bravo", "control"),
        )

    def test_port_name_is_mission_and_vm_scoped(self):
        self.assertNotEqual(
            port_name("Mission-Alpha", "db01", "control"),
            port_name("Mission-Bravo", "db01", "control"),
        )
        self.assertNotEqual(
            port_name("Mission-Alpha", "db01", "control"),
            port_name("Mission-Alpha", "db02", "control"),
        )

    def test_router_name_is_mission_scoped(self):
        self.assertNotEqual(router_name("Mission-Alpha"), router_name("Mission-Bravo"))

    def test_vm_uuid_is_deterministic(self):
        self.assertEqual(
            deterministic_vm_uuid("Mission-Alpha", "db01"),
            deterministic_vm_uuid("Mission-Alpha", "db01"),
        )
        self.assertNotEqual(
            deterministic_vm_uuid("Mission-Alpha", "db01"),
            deterministic_vm_uuid("Mission-Alpha", "db02"),
        )


class TestRenderDomainXmlStructure(unittest.TestCase):
    def _render_pxe_gpu_vm(self):
        vm = VMSpec(
            name="render01", type=VMType.PXE, cpu=8, memory_mb=32768,
            interfaces=list(SIX_NETS), gpu=True,
        )
        macs = [f"52:54:00:00:00:{i:02x}" for i in range(6)]
        xml_str = render_domain_xml(
            "Mission-Alpha", "render01", vm, macs, CEPH_STORAGE,
            gpu_pci_addresses=["0000:83:00.0"],
        )
        return xml_str, ET.fromstring(xml_str), macs

    def test_pxe_gpu_vm_is_well_formed_and_matches_spec(self):
        xml_str, root, macs = self._render_pxe_gpu_vm()

        interfaces = root.findall("./devices/interface")
        self.assertEqual(len(interfaces), 6, "design doc requires exactly 6 NICs per VM")
        found_macs = sorted(i.find("mac").get("address") for i in interfaces)
        self.assertEqual(found_macs, sorted(macs))

        # every interfaceid must match core.xml_render.port_name exactly,
        # since that's what lets ovn-controller bind the OVS port to the
        # right OVN logical port
        expected_ports = {port_name("Mission-Alpha", "render01", n) for n in SIX_NETS}
        found_ports = {i.find("./virtualport/parameters").get("interfaceid") for i in interfaces}
        self.assertEqual(found_ports, expected_ports)

        self.assertEqual(len(root.findall("./devices/disk")), 0, "PXE VMs must be diskless")
        self.assertEqual(root.find("./os/boot").get("dev"), "network")

        hostdevs = root.findall("./devices/hostdev")
        self.assertEqual(len(hostdevs), 1)
        addr = hostdevs[0].find("./source/address")
        self.assertEqual(addr.get("bus"), "0x83")

        self.assertEqual(root.find("./vcpu").text, "8")
        self.assertEqual(root.find("./memory").get("unit"), "MiB")
        self.assertEqual(root.find("./memory").text, "32768")

    def test_linked_clone_vm_has_disk_no_hostdev(self):
        vm = VMSpec(
            name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=16384,
            interfaces=["control", "storage", "management"], gpu=False,
            image="rhel9-db-golden",
        )
        macs = [f"52:54:00:00:01:{i:02x}" for i in range(3)]
        xml_str = render_domain_xml("Mission-Alpha", "db01", vm, macs, CEPH_STORAGE)
        root = ET.fromstring(xml_str)

        self.assertEqual(len(root.findall("./devices/interface")), 3)
        disks = root.findall("./devices/disk")
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0].find("./source").get("name"), "mission-images/db01_clone")
        self.assertEqual(len(root.findall("./devices/hostdev")), 0)
        self.assertEqual(root.find("./os/boot").get("dev"), "hd")

    def test_local_qcow2_backend_uses_file_disk(self):
        vm = VMSpec(
            name="app01", type=VMType.LINKED_CLONE, cpu=2, memory_mb=8192,
            interfaces=["control"], image="rhel9-app-golden",
        )
        xml_str = render_domain_xml("Mission-Alpha", "app01", vm, ["52:54:00:00:02:00"], LOCAL_STORAGE)
        root = ET.fromstring(xml_str)
        disk = root.find("./devices/disk")
        self.assertEqual(disk.get("type"), "file")
        self.assertIn("app01.qcow2", disk.find("./source").get("file"))

    def test_gpu_true_without_pci_addresses_raises(self):
        vm = VMSpec(name="v", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=["control"], gpu=True)
        with self.assertRaises(ValueError):
            render_domain_xml("M", "v", vm, ["52:54:00:00:00:00"], CEPH_STORAGE, gpu_pci_addresses=None)

    def test_gpu_false_with_pci_addresses_raises(self):
        vm = VMSpec(name="v", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=["control"], gpu=False)
        with self.assertRaises(ValueError):
            render_domain_xml("M", "v", vm, ["52:54:00:00:00:00"], CEPH_STORAGE, gpu_pci_addresses=["0000:01:00.0"])

    def test_mac_count_mismatch_raises(self):
        vm = VMSpec(name="v", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=["control", "storage"])
        with self.assertRaises(ValueError):
            render_domain_xml("M", "v", vm, ["52:54:00:00:00:00"], CEPH_STORAGE)  # only 1 MAC for 2 interfaces


if __name__ == "__main__":
    unittest.main()
