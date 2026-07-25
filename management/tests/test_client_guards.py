"""
This build sandbox has none of python3-libvirt, ovsdbapp, python3-rados,
or python3-rbd installed (no network egress to install them, and they
either need compiled system libraries or a real OVN/Ceph cluster to be
meaningful). These tests confirm the guarded-import pattern in
clients/*.py works as designed: importing the modules never raises, and
calling into them raises one clear, actionable error rather than an
ImportError/AttributeError deep in some call stack.

This is the honest substitute for actually exercising libvirt/OVN/Ceph
calls in this environment -- see README's transparency section.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestClientModulesImportCleanly(unittest.TestCase):
    def test_libvirt_client_imports(self):
        from clients import libvirt_client
        self.assertIsNone(libvirt_client.libvirt)  # confirms we're testing the "not installed" path

    def test_ovn_client_imports(self):
        from clients import ovn_client
        self.assertIsNone(ovn_client.nb_impl)

    def test_ceph_client_imports(self):
        from clients import ceph_client
        self.assertIsNone(ceph_client.rados)
        self.assertIsNone(ceph_client.rbd)

    def test_nm_client_imports_and_handles_missing_nmcli(self):
        from clients import nm_client
        # nmcli isn't installed in this sandbox either -- should return
        # False, not raise.
        self.assertFalse(nm_client.bridge_exists("br-int"))


class TestLibvirtClientRaisesClearError(unittest.TestCase):
    def test_connect_raises_actionable_error(self):
        from clients.libvirt_client import LibvirtUnavailableError, connect
        with self.assertRaises(LibvirtUnavailableError) as ctx:
            connect("compute01")
        self.assertIn("python3-libvirt", str(ctx.exception))


class TestOvnClientRaisesClearError(unittest.TestCase):
    def test_connect_raises_actionable_error(self):
        from clients.ovn_client import OvnUnavailableError, connect
        with self.assertRaises(OvnUnavailableError) as ctx:
            connect("tcp:compute01.cluster.local:6641")
        self.assertIn("ovsdbapp", str(ctx.exception))


class TestCephClientRaisesClearError(unittest.TestCase):
    def test_get_fsid_raises_actionable_error(self):
        from clients.ceph_client import CephUnavailableError, get_fsid
        with self.assertRaises(CephUnavailableError) as ctx:
            get_fsid()
        self.assertIn("python3-rados", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
