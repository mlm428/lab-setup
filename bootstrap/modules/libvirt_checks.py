"""
Libvirt-side host checks: libvirtd is active, virsh is responsive, and a
default local storage pool exists (used by the local_qcow2 storage backend
and generally handy even on Ceph-backed hosts for ISOs/cloud-init seeds).
"""
from __future__ import annotations

from .util import RunContext, run, service_is_active, log


def ensure_libvirtd_running(ctx: RunContext) -> None:
    if service_is_active(ctx, "libvirtd"):
        log.info("libvirt: libvirtd already active")
        ctx.record("libvirtd", "skipped", "already active")
        return
    run(ctx, ["systemctl", "enable", "--now", "libvirtd"])
    ctx.record("libvirtd", "ok", "enabled and started")


def ensure_default_pool(ctx: RunContext, path: str = "/var/lib/libvirt/images") -> None:
    """
    virsh pool-define-as/pool-start/pool-autostart are all safe to re-run:
    pool-define-as fails harmlessly if the pool already exists (we treat any
    non-zero exit here as "probably already defined" and just move on,
    logging the detail for a human to check if something else was wrong).
    """
    result = run(
        ctx,
        [
            "virsh",
            "pool-define-as",
            "default",
            "dir",
            "--target",
            path,
        ],
        check=False,
    )
    if result.returncode != 0 and "already exists" not in (result.stderr or ""):
        log.warning("libvirt: pool-define-as returned %s: %s", result.returncode, result.stderr.strip())

    run(ctx, ["virsh", "pool-start", "default"], check=False)
    run(ctx, ["virsh", "pool-autostart", "default"], check=False)
    ctx.record("default_storage_pool", "ok", path)
    log.info("libvirt: default storage pool ready at %s", path)


def verify_virsh_responsive(ctx: RunContext) -> bool:
    result = run(ctx, ["virsh", "list", "--all"], check=False)
    ok = result.returncode == 0
    ctx.record("virsh_responsive", "ok" if ok else "failed", result.stderr.strip())
    return ok
