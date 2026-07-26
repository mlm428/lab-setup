"""Unit tests for services/storage.py -- golden image catalog resolution and provisioning dispatch (mocked at the clients.ceph_client boundary)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import InterfaceSpec, MissionSpec, VMSpec, VMType
from core.xml_render import StorageContext
from services.storage import GoldenImageSource, provision_mission_storage


def make_golden(source="ceph_rbd_local"):
    return GoldenImageSource(
        source=source,
        ceph_rbd_local_pool="golden-images",
        remote_conf_path="/etc/ceph/golden.ceph.conf",
        remote_pool="golden-images-remote",
        mount_path="/mnt/golden-images",
        images={
            "rhel9-db-golden": {
                "default_revision": "Alpha",
                "revisions": {
                    "Alpha": {"filename": "rhel9-db-golden-alpha.qcow2", "snapshot": "snap-alpha"},
                    "Beta": {"filename": "rhel9-db-golden-beta.qcow2", "snapshot": "snap-beta"},
                },
            }
        },
    )


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


class TestGoldenImageSourceResolveImage(unittest.TestCase):
    def test_default_revision_used_when_unspecified(self):
        golden = make_golden()
        info = golden.resolve_image("rhel9-db-golden", None)
        self.assertEqual(info["revision"], "Alpha")
        self.assertEqual(info["filename"], "rhel9-db-golden-alpha.qcow2")

    def test_explicit_revision_used_when_specified(self):
        golden = make_golden()
        info = golden.resolve_image("rhel9-db-golden", "Beta")
        self.assertEqual(info["revision"], "Beta")
        self.assertEqual(info["filename"], "rhel9-db-golden-beta.qcow2")

    def test_unknown_image_raises(self):
        golden = make_golden()
        with self.assertRaises(ValueError):
            golden.resolve_image("does-not-exist", None)

    def test_unknown_revision_raises(self):
        golden = make_golden()
        with self.assertRaises(ValueError):
            golden.resolve_image("rhel9-db-golden", "Gamma")


def make_mission_with_one_linked_clone(image_revision=None):
    vm = VMSpec(
        name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192,
        interfaces=ifaces("control"), image="rhel9-db-golden", image_revision=image_revision,
    )
    return MissionSpec(name="M", networks={"control": 100}, vms={"db01": vm}, placement={"db01": "h1"})


class TestProvisionMissionStorageDispatch(unittest.TestCase):
    """Confirms provision_mission_storage calls the RIGHT ceph_client function for each (runtime.backend, golden.source) combination."""

    def test_ceph_local_uses_within_cluster_clone(self):
        mission = make_mission_with_one_linked_clone()
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden(source="ceph_rbd_local")

        with patch("services.storage.ceph_client.ensure_golden_snapshot") as m_snap, \
             patch("services.storage.ceph_client.clone_within_cluster") as m_clone, \
             patch("services.storage.ceph_client.copy_remote_image_to_pool") as m_remote, \
             patch("services.storage.ceph_client.copy_mount_image_to_pool") as m_mount:
            resolved = provision_mission_storage(mission, runtime, golden)

        m_snap.assert_called_once()
        m_clone.assert_called_once()
        m_remote.assert_not_called()
        m_mount.assert_not_called()
        self.assertEqual(resolved, {"db01": "Alpha"})
        self.assertEqual(m_clone.call_args.kwargs["clone_name"], "db01_clone")

    def test_ceph_remote_uses_remote_copy(self):
        mission = make_mission_with_one_linked_clone()
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden(source="ceph_rbd_remote")

        with patch("services.storage.ceph_client.clone_within_cluster") as m_clone, \
             patch("services.storage.ceph_client.copy_remote_image_to_pool") as m_remote, \
             patch("services.storage.ceph_client.copy_mount_image_to_pool") as m_mount:
            provision_mission_storage(mission, runtime, golden)

        m_remote.assert_called_once()
        m_clone.assert_not_called()
        m_mount.assert_not_called()

    def test_mount_source_uses_mount_copy(self):
        mission = make_mission_with_one_linked_clone()
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden(source="mount")

        with patch("services.storage.ceph_client.clone_within_cluster") as m_clone, \
             patch("services.storage.ceph_client.copy_remote_image_to_pool") as m_remote, \
             patch("services.storage.ceph_client.copy_mount_image_to_pool") as m_mount:
            provision_mission_storage(mission, runtime, golden)

        m_mount.assert_called_once()
        self.assertIn("rhel9-db-golden-alpha.qcow2", m_mount.call_args.kwargs["mount_file_path"])
        m_clone.assert_not_called()
        m_remote.assert_not_called()

    def test_explicit_image_revision_flows_through(self):
        mission = make_mission_with_one_linked_clone(image_revision="Beta")
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden(source="ceph_rbd_local")

        with patch("services.storage.ceph_client.ensure_golden_snapshot"), \
             patch("services.storage.ceph_client.clone_within_cluster"):
            resolved = provision_mission_storage(mission, runtime, golden)

        self.assertEqual(resolved, {"db01": "Beta"})

    def test_pxe_vms_are_skipped(self):
        vm = VMSpec(name="worker01", type=VMType.PXE, cpu=4, memory_mb=8192, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"worker01": vm}, placement={"worker01": "h1"})
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden()

        with patch("services.storage.ceph_client.clone_within_cluster") as m_clone:
            resolved = provision_mission_storage(mission, runtime, golden)

        m_clone.assert_not_called()
        self.assertEqual(resolved, {})

    def test_unknown_image_raises_before_any_clone_call(self):
        vm = VMSpec(name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=8192, interfaces=ifaces("control"), image="does-not-exist")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"db01": vm}, placement={"db01": "h1"})
        runtime = StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime")
        golden = make_golden()

        with patch("services.storage.ceph_client.clone_within_cluster") as m_clone:
            with self.assertRaises(ValueError):
                provision_mission_storage(mission, runtime, golden)
        m_clone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
