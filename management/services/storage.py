"""
Storage service: provisions the backing disk for each linked-clone VM
before it's defined in libvirt. PXE VMs are diskless and this module does
nothing for them.

Golden (source) images and runtime (per-VM clone) disks are deliberately
separate stores, per operator requirement -- see config/storage.yaml and
clients/ceph_client.py's module docstring for the three supported golden
sources (ceph_rbd_local: true thin clone; ceph_rbd_remote / mount: full
copy, less space-efficient but correct).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from clients import ceph_client
from core.types import MissionSpec, VMType
from core.xml_render import StorageContext


@dataclass
class GoldenImageSource:
    """
    Where and how to read golden (source) images from, and the catalog of
    known images/revisions -- loaded from config/storage.yaml's `golden`
    section. Kept separate from StorageContext (which describes the
    RUNTIME pool a clone is written into) since the two are independently
    configurable.

    Attributes:
        source: "ceph_rbd_local" | "ceph_rbd_remote" | "mount".
        ceph_rbd_local_pool: Pool name (same cluster as runtime) when source=="ceph_rbd_local".
        remote_conf_path/remote_client_id/remote_pool: Connection info for
            a separate Ceph cluster, when source=="ceph_rbd_remote".
        mount_path: Directory holding golden qcow2 files, when source=="mount".
        images: Catalog: {image_name: {"default_revision": str,
            "revisions": {revision: {"filename": str, "snapshot": str}}}}.
    """
    source: str
    ceph_rbd_local_pool: str = "golden-images"
    remote_conf_path: str = "/etc/ceph/golden.ceph.conf"
    remote_client_id: str = "golden-reader"
    remote_pool: str = "golden-images"
    mount_path: str = "/mnt/golden-images"
    images: dict = field(default_factory=dict)

    def resolve_image(self, image_name: str, image_revision: str | None) -> dict:
        """
        Look up a golden image + revision in the catalog.

        Args:
            image_name: Catalog key (e.g. "rhel9-db-golden") -- from a
                VM's `image:` field.
            image_revision: Requested revision (e.g. "Alpha"), or None to
                use the catalog's `default_revision` for that image.

        Returns:
            {"revision": str, "filename": str, "snapshot": str | None}

        Raises:
            ValueError: if `image_name` isn't in the catalog, if no
                revision could be determined, or if the resolved revision
                isn't in the catalog for that image.
        """
        catalog_entry = self.images.get(image_name)
        if catalog_entry is None:
            raise ValueError(
                f"unknown golden image '{image_name}' -- not found in "
                f"config/storage.yaml's golden.images catalog (known: {sorted(self.images)})"
            )
        revision = image_revision or catalog_entry.get("default_revision")
        if not revision:
            raise ValueError(
                f"golden image '{image_name}': no image_revision requested and "
                f"the catalog has no default_revision"
            )
        revisions = catalog_entry.get("revisions", {})
        rev_entry = revisions.get(revision)
        if rev_entry is None:
            raise ValueError(
                f"golden image '{image_name}': revision '{revision}' not found "
                f"(available: {sorted(revisions)})"
            )
        return {"revision": revision, "filename": rev_entry["filename"], "snapshot": rev_entry.get("snapshot")}


def provision_mission_storage(mission: MissionSpec, runtime: StorageContext, golden: GoldenImageSource) -> dict[str, str]:
    """
    Provision the backing disk for every linked-clone VM in a mission.
    No-op for pxe VMs (diskless).

    Args:
        mission: The mission whose VMs to provision storage for.
        runtime: Where the resulting clone disks are written (backend +
            connection info).
        golden: Where the source images are read from (backend +
            connection info + image/revision catalog).

    Returns:
        {vm_name: resolved_revision} for every linked-clone VM -- recorded
        in the mission's step log so it's always possible to tell which
        build of a golden image a given deployment was actually cloned
        from.

    Raises:
        ValueError: if a VM's `image`/`image_revision` doesn't resolve
            against the golden catalog, or if `runtime.backend` /
            `golden.source` is not a recognized value.
        subprocess.CalledProcessError: on a real host, if an underlying
            `rbd`/`qemu-img` copy operation fails.
    """
    resolved_revisions: dict[str, str] = {}

    for vm_name, vm in mission.vms.items():
        if vm.type != VMType.LINKED_CLONE:
            continue

        image_info = golden.resolve_image(vm.image, vm.image_revision)
        resolved_revisions[vm_name] = image_info["revision"]
        clone_name = f"{vm_name}_clone"

        if runtime.backend == "ceph_rbd":
            _provision_ceph_rbd_clone(runtime, golden, image_info, clone_name)
        elif runtime.backend == "local_qcow2":
            _provision_local_qcow2_clone(runtime, golden, image_info, clone_name)
        else:
            raise ValueError(f"unknown runtime storage backend: {runtime.backend}")

    return resolved_revisions


def _provision_ceph_rbd_clone(runtime: StorageContext, golden: GoldenImageSource, image_info: dict, clone_name: str) -> None:
    """Dispatch to the right clients.ceph_client function based on golden.source, for a runtime.backend=='ceph_rbd' VM."""
    golden_image_base = image_info["filename"].rsplit(".", 1)[0]  # strip .qcow2 -- RBD image names have no extension

    if golden.source == "ceph_rbd_local":
        ceph_client.ensure_golden_snapshot(
            conf_path="/etc/ceph/ceph.conf",
            pool=golden.ceph_rbd_local_pool,
            golden_image=golden_image_base,
            snapshot_name=image_info["snapshot"] or "golden-snap",
        )
        ceph_client.clone_within_cluster(
            conf_path="/etc/ceph/ceph.conf",
            golden_pool=golden.ceph_rbd_local_pool,
            golden_image=golden_image_base,
            golden_snapshot=image_info["snapshot"] or "golden-snap",
            runtime_pool=runtime.ceph_pool,
            clone_name=clone_name,
        )
    elif golden.source == "ceph_rbd_remote":
        ceph_client.copy_remote_image_to_pool(
            remote_conf_path=golden.remote_conf_path,
            remote_pool=golden.remote_pool,
            remote_image=golden_image_base,
            remote_snapshot=image_info["snapshot"] or "golden-snap",
            local_conf_path="/etc/ceph/ceph.conf",
            local_pool=runtime.ceph_pool,
            clone_name=clone_name,
        )
    elif golden.source == "mount":
        ceph_client.copy_mount_image_to_pool(
            mount_file_path=f"{golden.mount_path}/{image_info['filename']}",
            local_conf_path="/etc/ceph/ceph.conf",
            local_pool=runtime.ceph_pool,
            clone_name=clone_name,
        )
    else:
        raise ValueError(f"unknown golden image source: {golden.source}")


def _provision_local_qcow2_clone(runtime: StorageContext, golden: GoldenImageSource, image_info: dict, clone_name: str) -> None:
    """Dispatch for a runtime.backend=='local_qcow2' VM: always ends with a qcow2 file in runtime.local_qcow2_dir."""
    clone_path = f"{runtime.local_qcow2_dir}/{clone_name}.qcow2"
    if os.path.exists(clone_path):
        return  # idempotent

    if golden.source == "mount":
        golden_path = f"{golden.mount_path}/{image_info['filename']}"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", golden_path, clone_path],
            check=True, capture_output=True, text=True,
        )
    elif golden.source in ("ceph_rbd_local", "ceph_rbd_remote"):
        # Golden source is Ceph but runtime is local qcow2 -- pull a full
        # copy down via qemu-img (no backing-file relationship possible
        # across an RBD-to-local-file boundary).
        conf_path = "/etc/ceph/ceph.conf" if golden.source == "ceph_rbd_local" else golden.remote_conf_path
        pool = golden.ceph_rbd_local_pool if golden.source == "ceph_rbd_local" else golden.remote_pool
        golden_image_base = image_info["filename"].rsplit(".", 1)[0]
        snap_suffix = f"@{image_info['snapshot']}" if image_info.get("snapshot") else ""
        subprocess.run(
            [
                "qemu-img", "convert", "-O", "qcow2",
                f"rbd:{pool}/{golden_image_base}{snap_suffix}:conf={conf_path}",
                clone_path,
            ],
            check=True, capture_output=True, text=True,
        )
    else:
        raise ValueError(f"unknown golden image source: {golden.source}")


def teardown_mission_storage(mission: MissionSpec, runtime: StorageContext) -> None:
    """
    Idempotently remove every linked-clone VM's runtime disk for one
    deployment. Never touches golden images (read-only, shared across
    deployments).

    Args:
        mission: The mission (deployment) being torn down.
        runtime: Runtime storage backend/connection info (must match what
            was used to provision it).

    Returns:
        None.
    """
    for vm_name, vm in mission.vms.items():
        if vm.type != VMType.LINKED_CLONE:
            continue
        clone_name = f"{vm_name}_clone"
        if runtime.backend == "ceph_rbd":
            ceph_client.remove_clone("/etc/ceph/ceph.conf", runtime.ceph_pool, clone_name)
        elif runtime.backend == "local_qcow2":
            clone_path = f"{runtime.local_qcow2_dir}/{clone_name}.qcow2"
            if os.path.exists(clone_path):
                os.remove(clone_path)
