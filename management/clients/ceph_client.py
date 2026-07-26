"""
Thin wrapper around Ceph's RADOS/RBD Python bindings (plus, for
cross-cluster/mount golden-image sourcing, the `rbd`/`qemu-img` CLIs via
subprocess -- see module docstring further down for why), per the design
doc:

    import rados, rbd
    cluster = rados.Rados(conffile='/etc/ceph/ceph.conf')
    cluster.connect()
    ioctx = cluster.open_ioctx('rbd_pool')
    rbd_inst = rbd.RBD()
    rbd_inst.clone(ioctx, 'gold-image', ioctx, 'db01-clone', 'snap0')

Import is guarded the same way as the other clients -- python3-rados and
python3-rbd are dnf packages installed by bootstrap.py on real hosts, not
pip-installable, and this build sandbox has neither.

GOLDEN VS. RUNTIME: per operator requirement, golden (source) images and
runtime (per-VM clone) disks are deliberately separate stores -- see
config/storage.yaml. Three golden sources are supported:

  - ceph_rbd_local:  golden images live in a different POOL on the SAME
    Ceph cluster as the runtime pool. A true, space-efficient
    copy-on-write clone is possible here (RBD "clone v2" supports
    cross-pool clones within one cluster) -- clone_within_cluster().
  - ceph_rbd_remote: golden images live on a DIFFERENT Ceph cluster
    entirely (its own ceph.conf/keyring). RBD clone-from-snapshot cannot
    cross clusters, so this is a full data copy via the `rbd` CLI's
    export/import (piped, so the whole image never touches local disk) --
    copy_remote_image_to_pool(). Not space-efficient, but correct.
  - mount: golden images are plain qcow2 files on a filesystem mount
    (e.g. an NFS export). Converted directly into the runtime RBD pool via
    `qemu-img convert` -- copy_mount_image_to_pool(). Also a full copy.

Only ceph_rbd_local gives a thin/space-efficient clone; the other two are
documented, deliberate full copies. See services/storage.py, which
chooses among these three based on config/storage.yaml's `golden.source`.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager

try:
    import rados
    import rbd
except ImportError:  # pragma: no cover - expected without python3-rados/rbd installed
    rados = None
    rbd = None


class CephUnavailableError(RuntimeError):
    """Raised when python3-rados/python3-rbd aren't importable on this host."""
    def __init__(self):
        super().__init__(
            "python3-rados/python3-rbd are not installed on this host. "
            "Install via bootstrap.py / dnf, then retry."
        )


def _require_ceph():
    """Raise CephUnavailableError unless the real rados/rbd bindings are importable."""
    if rados is None or rbd is None:
        raise CephUnavailableError()


@contextmanager
def cluster_connection(conf_path: str = "/etc/ceph/ceph.conf"):
    """
    Context manager yielding a connected rados.Rados cluster handle,
    closed automatically on exit.

    Args:
        conf_path: Path to the ceph.conf identifying which cluster to
            connect to (and, implicitly via that file's keyring
            reference, which client identity to authenticate as).

    Yields:
        A connected rados.Rados instance.

    Raises:
        CephUnavailableError: if python3-rados/rbd aren't installed.
    """
    _require_ceph()
    cluster = rados.Rados(conffile=conf_path)
    cluster.connect(timeout=10)
    try:
        yield cluster
    finally:
        cluster.shutdown()


def get_fsid(conf_path: str = "/etc/ceph/ceph.conf") -> str:
    """Return the connected cluster's FSID (liveness/identity check)."""
    with cluster_connection(conf_path) as cluster:
        return cluster.get_fsid()


def ensure_golden_snapshot(conf_path: str, pool: str, golden_image: str, snapshot_name: str) -> None:
    """
    Create + protect a snapshot of a golden image if it doesn't already
    exist. Protection is required before RBD will allow cloning from it.

    Args:
        conf_path: ceph.conf for the cluster holding `pool`.
        pool: Pool containing the golden image.
        golden_image: Golden image name (RBD image, no extension).
        snapshot_name: Snapshot to create/ensure-protected.

    Returns:
        None. Idempotent -- a no-op if the snapshot already exists and is
        already protected.
    """
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            image = rbd.Image(ioctx, golden_image)
            try:
                existing_snaps = {s["name"] for s in image.list_snaps()}
                if snapshot_name not in existing_snaps:
                    image.create_snap(snapshot_name)
                if not image.is_protected_snap(snapshot_name):
                    image.protect_snap(snapshot_name)
            finally:
                image.close()


def clone_within_cluster(
    conf_path: str,
    golden_pool: str,
    golden_image: str,
    golden_snapshot: str,
    runtime_pool: str,
    clone_name: str,
) -> None:
    """
    Space-efficient, copy-on-write RBD clone, golden and runtime pools on
    the SAME cluster (config/storage.yaml's golden.source ==
    "ceph_rbd_local"). Requires RBD "clone v2" (Ceph Nautilus+) for the
    cross-pool case.

    Args:
        conf_path: ceph.conf shared by both pools (same cluster).
        golden_pool: Pool holding the golden image + its protected snapshot.
        golden_image: Golden image name.
        golden_snapshot: Protected snapshot to clone from (see ensure_golden_snapshot).
        runtime_pool: Pool the new clone is created in.
        clone_name: New RBD image name for the clone (typically "<vm_name>_clone").

    Returns:
        None. Idempotent -- a no-op if `clone_name` already exists in `runtime_pool`.
    """
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        with cluster.open_ioctx(golden_pool) as src_ioctx, cluster.open_ioctx(runtime_pool) as dst_ioctx:
            rbd_inst = rbd.RBD()
            if clone_name in rbd_inst.list(dst_ioctx):
                return  # idempotent: clone already exists
            rbd_inst.clone(src_ioctx, golden_image, golden_snapshot, dst_ioctx, clone_name)


def copy_remote_image_to_pool(
    remote_conf_path: str,
    remote_pool: str,
    remote_image: str,
    remote_snapshot: str,
    local_conf_path: str,
    local_pool: str,
    clone_name: str,
) -> None:
    """
    Full copy of a golden image FROM a different Ceph cluster INTO the
    local runtime pool (config/storage.yaml's golden.source ==
    "ceph_rbd_remote"). Not a space-efficient clone -- RBD's
    copy-on-write clone mechanism cannot cross clusters -- but this is the
    correct, documented behavior for that configuration.

    Implemented via the `rbd` CLI's export/import, piped directly between
    the two processes so the full image never touches local disk as an
    intermediate file. Requires the `rbd` binary on PATH (provided by the
    ceph-common package bootstrap.py installs).

    Args:
        remote_conf_path: ceph.conf for the remote (golden) cluster.
        remote_pool: Pool on the remote cluster holding the golden image.
        remote_image: Golden image name on the remote cluster.
        remote_snapshot: Snapshot to export (does not need to be
            "protected" for a plain export, unlike clone_within_cluster).
        local_conf_path: ceph.conf for the local (runtime) cluster.
        local_pool: Local runtime pool to import into.
        clone_name: New RBD image name in `local_pool`.

    Returns:
        None. Idempotent -- checks for an existing image of `clone_name`
        in `local_pool` first and skips the copy if already present.

    Raises:
        subprocess.CalledProcessError: if the underlying `rbd export`/
            `rbd import` pipeline fails.
    """
    _require_ceph()
    with cluster_connection(local_conf_path) as cluster:
        with cluster.open_ioctx(local_pool) as ioctx:
            if clone_name in rbd.RBD().list(ioctx):
                return  # idempotent

    export_cmd = ["rbd", "--conf", remote_conf_path, "export", f"{remote_pool}/{remote_image}@{remote_snapshot}", "-"]
    import_cmd = ["rbd", "--conf", local_conf_path, "import", "-", f"{local_pool}/{clone_name}"]

    exporter = subprocess.Popen(export_cmd, stdout=subprocess.PIPE)
    try:
        result = subprocess.run(import_cmd, stdin=exporter.stdout, check=True, capture_output=True, text=True)
    finally:
        if exporter.stdout:
            exporter.stdout.close()
        exporter.wait()
    if exporter.returncode not in (0, None):
        raise subprocess.CalledProcessError(exporter.returncode, export_cmd)
    return result


def copy_mount_image_to_pool(mount_file_path: str, local_conf_path: str, local_pool: str, clone_name: str) -> None:
    """
    Full copy of a golden qcow2 image FROM a filesystem mount (e.g. an
    NFS export) INTO the local runtime RBD pool (config/storage.yaml's
    golden.source == "mount"). Uses `qemu-img convert`, which supports
    `rbd:` destination URIs directly.

    Args:
        mount_file_path: Full path to the golden qcow2 file (e.g.
            "/mnt/golden-images/rhel9-db-golden-alpha.qcow2").
        local_conf_path: ceph.conf for the local (runtime) cluster.
        local_pool: Local runtime pool to write into.
        clone_name: New RBD image name in `local_pool`.

    Returns:
        None. Idempotent -- checks for an existing image of `clone_name`
        in `local_pool` first and skips the copy if already present.

    Raises:
        subprocess.CalledProcessError: if `qemu-img convert` fails.
    """
    _require_ceph()
    with cluster_connection(local_conf_path) as cluster:
        with cluster.open_ioctx(local_pool) as ioctx:
            if clone_name in rbd.RBD().list(ioctx):
                return  # idempotent

    subprocess.run(
        [
            "qemu-img", "convert", "-O", "raw",
            mount_file_path,
            f"rbd:{local_pool}/{clone_name}:conf={local_conf_path}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def remove_clone(conf_path: str, pool: str, clone_name: str) -> None:
    """
    Idempotently remove a runtime clone image.

    Args:
        conf_path: ceph.conf for the cluster holding `pool`.
        pool: Runtime pool containing the clone.
        clone_name: Clone image name to remove.

    Returns:
        None. A no-op (not an error) if the image doesn't exist.
    """
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            rbd_inst = rbd.RBD()
            try:
                rbd_inst.remove(ioctx, clone_name)
            except rbd.ImageNotFound:
                pass


def pool_stats(conf_path: str, pool: str) -> dict:
    """
    Return usage stats for one pool (used by services/validation.py and
    the cluster health endpoint for the 'Storage Efficiency' criterion).

    Args:
        conf_path: ceph.conf for the target cluster.
        pool: Pool name to report on.

    Returns:
        A dict of whatever librados' get_pool_stats() reports for `pool`
        (bytes used, object count, etc. -- shape depends on the Ceph
        version), or {} if the binding doesn't expose pool-level stats.
    """
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        stats = cluster.get_pool_stats() if hasattr(cluster, "get_pool_stats") else {}
        return stats.get(pool, {})
