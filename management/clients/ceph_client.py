"""
Thin wrapper around Ceph's RADOS/RBD Python bindings, per the design doc:

    import rados, rbd
    cluster = rados.Rados(conffile='/etc/ceph/ceph.conf')
    cluster.connect()
    ioctx = cluster.open_ioctx('rbd_pool')
    rbd_inst = rbd.RBD()
    rbd_inst.clone(ioctx, 'gold-image', ioctx, 'db01-clone', 'snap0')

Import is guarded the same way as the other clients -- python3-rados and
python3-rbd are dnf packages installed by bootstrap.py on real hosts, not
pip-installable, and this build sandbox has neither.
"""
from __future__ import annotations

from contextlib import contextmanager

try:
    import rados
    import rbd
except ImportError:  # pragma: no cover - expected without python3-rados/rbd installed
    rados = None
    rbd = None


class CephUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__(
            "python3-rados/python3-rbd are not installed on this host. "
            "Install via bootstrap.py / dnf, then retry."
        )


def _require_ceph():
    if rados is None or rbd is None:
        raise CephUnavailableError()


@contextmanager
def cluster_connection(conf_path: str = "/etc/ceph/ceph.conf"):
    _require_ceph()
    cluster = rados.Rados(conffile=conf_path)
    cluster.connect(timeout=10)
    try:
        yield cluster
    finally:
        cluster.shutdown()


def get_fsid(conf_path: str = "/etc/ceph/ceph.conf") -> str:
    with cluster_connection(conf_path) as cluster:
        return cluster.get_fsid()


def clone_golden_image(
    conf_path: str,
    pool: str,
    golden_image: str,
    golden_snapshot: str,
    clone_name: str,
) -> None:
    """
    Linked-clone equivalent for Ceph RBD: snapshot+protect the golden image
    once (idempotent, see ensure_golden_snapshot below), then clone it into
    a new thin RBD image per VM, per the design doc:

        rbd snap create mission-images/golden-db@snap1
        rbd snap protect mission-images/golden-db@snap1
        rbd clone mission-images/golden-db@snap1 mission-images/db01_clone
    """
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            rbd_inst = rbd.RBD()
            existing = rbd_inst.list(ioctx)
            if clone_name in existing:
                return  # idempotent: clone already exists
            rbd_inst.clone(ioctx, golden_image, golden_snapshot, ioctx, clone_name)


def ensure_golden_snapshot(conf_path: str, pool: str, golden_image: str, snapshot_name: str) -> None:
    """Create + protect a snapshot of the golden image if it doesn't
    already exist. Protection is required before RBD will allow cloning
    from it."""
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


def remove_clone(conf_path: str, pool: str, clone_name: str) -> None:
    """Idempotent teardown: ignore 'does not exist' errors."""
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        with cluster.open_ioctx(pool) as ioctx:
            rbd_inst = rbd.RBD()
            try:
                rbd_inst.remove(ioctx, clone_name)
            except rbd.ImageNotFound:
                pass


def pool_stats(conf_path: str, pool: str) -> dict:
    """Used by services/validation.py for the 'Storage Efficiency'
    acceptance criterion (Ceph pool usage after N clones)."""
    _require_ceph()
    with cluster_connection(conf_path) as cluster:
        stats = cluster.get_pool_stats() if hasattr(cluster, "get_pool_stats") else {}
        return stats.get(pool, {})
