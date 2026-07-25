"""
Storage service: provisions the backing disk for each linked-clone VM
before it's defined in libvirt. PXE VMs are diskless and this module does
nothing for them.

Implements both paths the design doc's Requirements Mapping table
describes:
  - Ceph RBD native clone (primary; snapshot+protect golden once, then
    clone per VM) -- clients/ceph_client.py
  - local qcow2 `qemu-img create -b` backing file (fallback for non-Ceph
    hosts, per the doc's own "have fallback (NFS) if needed for
    prototype" risk mitigation)
"""
from __future__ import annotations

import subprocess

from clients import ceph_client
from core.types import MissionSpec, VMType
from core.xml_render import StorageContext


def provision_mission_storage(mission: MissionSpec, storage: StorageContext, golden_snapshot: str = "golden-snap") -> None:
    for vm_name, vm in mission.vms.items():
        if vm.type != VMType.LINKED_CLONE:
            continue
        clone_name = f"{vm_name}_clone"

        if storage.backend == "ceph_rbd":
            ceph_client.ensure_golden_snapshot(
                conf_path="/etc/ceph/ceph.conf",
                pool=storage.ceph_pool,
                golden_image=vm.image,
                snapshot_name=golden_snapshot,
            )
            ceph_client.clone_golden_image(
                conf_path="/etc/ceph/ceph.conf",
                pool=storage.ceph_pool,
                golden_image=vm.image,
                golden_snapshot=golden_snapshot,
                clone_name=clone_name,
            )
        elif storage.backend == "local_qcow2":
            _create_local_backing_clone(storage, vm.image, clone_name)
        else:
            raise ValueError(f"unknown storage backend: {storage.backend}")


def _create_local_backing_clone(storage: StorageContext, golden_image: str, clone_name: str) -> None:
    """
    `qemu-img create -f qcow2 -b <golden> <clone>`, per the design doc's
    local/non-Ceph example. Idempotent: skip if the clone file already
    exists.
    """
    import os

    golden_path = f"{storage.local_qcow2_dir}/golden/{golden_image}"
    clone_path = f"{storage.local_qcow2_dir}/{clone_name}.qcow2"
    if os.path.exists(clone_path):
        return
    subprocess.run(
        [
            "qemu-img", "create", "-f", "qcow2",
            "-F", "qcow2",
            "-b", golden_path,
            clone_path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def teardown_mission_storage(mission: MissionSpec, storage: StorageContext) -> None:
    for vm_name, vm in mission.vms.items():
        if vm.type != VMType.LINKED_CLONE:
            continue
        clone_name = f"{vm_name}_clone"
        if storage.backend == "ceph_rbd":
            ceph_client.remove_clone("/etc/ceph/ceph.conf", storage.ceph_pool, clone_name)
        elif storage.backend == "local_qcow2":
            import os
            clone_path = f"{storage.local_qcow2_dir}/{clone_name}.qcow2"
            if os.path.exists(clone_path):
                os.remove(clone_path)
