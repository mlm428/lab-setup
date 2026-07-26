"""
Storage backend setup on each host: either wire up a libvirt storage pool
backed by Ceph RBD (primary path), or ensure a local qcow2-backing-file
pool exists (fallback path, per the design doc's own "have fallback if
needed for prototype" risk mitigation).

Reads from config/storage.yaml's `runtime` section (the pool VM disks are
actually written to). `golden` (source images) is a management-service-
side concern only (services/storage.py) -- bootstrap.py does not need to
reach the golden image store, since it never clones anything itself.

Ceph connectivity is validated with the librados Python binding, matching
the design doc's cited example almost verbatim:

    import rados
    cluster = rados.Rados(conffile='ceph.conf')
    cluster.connect()
    print(cluster.get_fsid())
    cluster.shutdown()
"""
from __future__ import annotations

from .util import RunContext, run, log

try:
    import rados  # python3-rados, installed by packages.py on real hosts
except ImportError:  # pragma: no cover - expected in the build sandbox / on dev laptops
    rados = None


def verify_ceph_connectivity(ctx: RunContext, storage_cfg: dict) -> bool:
    """
    Connect to the Ceph cluster with librados and fetch the cluster FSID
    as a liveness check.

    Args:
        ctx: Run context (honors --dry-run).
        storage_cfg: Parsed config/storage.yaml.

    Returns:
        True if the connection + FSID fetch succeeded, False otherwise
        (never raises) -- so bootstrap.py can treat "no Ceph reachable" as
        a soft failure when a host's storage_backend is local_qcow2.
    """
    if ctx.dry_run:
        log.info("[dry-run] would verify Ceph connectivity via librados")
        return True

    if rados is None:
        log.warning("storage: python3-rados not importable on this host -- skipping live Ceph check")
        ctx.record("ceph_connectivity", "skipped", "python3-rados not installed")
        return False

    conf_path = storage_cfg["runtime"]["ceph"]["conf_path"]
    try:
        cluster = rados.Rados(conffile=conf_path)
        cluster.connect(timeout=5)
        fsid = cluster.get_fsid()
        cluster.shutdown()
        log.info("storage: connected to Ceph cluster fsid=%s", fsid)
        ctx.record("ceph_connectivity", "ok", fsid)
        return True
    except Exception as exc:  # noqa: BLE001 - report and let caller decide
        log.warning("storage: Ceph connectivity check failed: %s", exc)
        ctx.record("ceph_connectivity", "failed", str(exc))
        return False


def ensure_libvirt_rbd_pool(ctx: RunContext, storage_cfg: dict) -> None:
    """
    Idempotently define a libvirt storage pool of type 'rbd' pointing at
    the runtime pool, per the design doc:

        virsh pool-create-as ceph-pool --type rbd \
            --target /var/lib/libvirt/images --source-name mission-runtime

    Args:
        ctx: Run context.
        storage_cfg: Parsed config/storage.yaml.

    Returns:
        None.
    """
    pool_name = "ceph-pool"
    runtime_pool = storage_cfg["runtime"]["ceph"]["pool"]

    check = run(ctx, ["virsh", "pool-info", pool_name], check=False)
    if check.returncode == 0:
        log.info("storage: libvirt pool %s already defined", pool_name)
        ctx.record("libvirt_rbd_pool", "skipped", pool_name)
        return

    run(
        ctx,
        [
            "virsh",
            "pool-create-as",
            pool_name,
            "--type",
            "rbd",
            "--target",
            "/var/lib/libvirt/images",
            "--source-name",
            runtime_pool,
        ],
    )
    ctx.record("libvirt_rbd_pool", "ok", f"{pool_name} -> {runtime_pool}")
    log.info("storage: created libvirt RBD pool %s -> ceph pool %s", pool_name, runtime_pool)


def ensure_local_fallback_pool(ctx: RunContext, storage_cfg: dict) -> None:
    """
    Idempotently define and start a local directory-backed libvirt storage
    pool, for hosts using storage_backend: local_qcow2.

    Args:
        ctx: Run context.
        storage_cfg: Parsed config/storage.yaml.

    Returns:
        None.
    """
    cfg = storage_cfg["runtime"]["local_qcow2"]
    run(ctx, ["mkdir", "-p", cfg["pool_path"]], check=False)
    check = run(ctx, ["virsh", "pool-info", cfg["pool_name"]], check=False)
    if check.returncode == 0:
        ctx.record("local_pool", "skipped", cfg["pool_name"])
        return
    run(
        ctx,
        [
            "virsh",
            "pool-define-as",
            cfg["pool_name"],
            "dir",
            "--target",
            cfg["pool_path"],
        ],
        check=False,
    )
    run(ctx, ["virsh", "pool-start", cfg["pool_name"]], check=False)
    run(ctx, ["virsh", "pool-autostart", cfg["pool_name"]], check=False)
    ctx.record("local_pool", "ok", cfg["pool_name"])


def configure_storage(ctx: RunContext, storage_cfg: dict, backend: str) -> None:
    """
    Set up whichever storage backend this host uses.

    Args:
        ctx: Run context.
        storage_cfg: Parsed config/storage.yaml.
        backend: "ceph_rbd" or "local_qcow2" (from this host's entry in
            config/hosts.yaml).

    Returns:
        None.

    Raises:
        RuntimeError: if backend=="ceph_rbd" but Ceph is unreachable (and
            not a dry run).
        ValueError: if `backend` is neither "ceph_rbd" nor "local_qcow2".
    """
    if backend == "ceph_rbd":
        reachable = verify_ceph_connectivity(ctx, storage_cfg)
        if reachable or ctx.dry_run:
            ensure_libvirt_rbd_pool(ctx, storage_cfg)
        else:
            log.error("storage: backend=ceph_rbd requested but Ceph is unreachable")
            raise RuntimeError("Ceph unreachable; cannot configure ceph_rbd storage backend")
    elif backend == "local_qcow2":
        ensure_local_fallback_pool(ctx, storage_cfg)
    else:
        raise ValueError(f"unknown storage_backend: {backend}")
