"""
Cockpit (+ cockpit-machines) enablement -- the vCenter-web-client
replacement for ad hoc/manual inspection. Bulk operations still go through
the management API (Deliverable B), per the design doc: "Its integration
with libvirt is not as rich as vCenter, so we will rely on REST APIs and
automation scripts for bulk tasks."
"""
from __future__ import annotations

from .util import RunContext, run, service_is_active, log


def enable_cockpit(ctx: RunContext) -> None:
    """
    Idempotently enable + start cockpit.socket (which activates
    cockpit-machines for ad hoc VM inspection alongside the management
    API's bulk operations).

    Args:
        ctx: Run context (honors --dry-run).

    Returns:
        None. A no-op if cockpit.socket is already active.
    """
    if service_is_active(ctx, "cockpit.socket"):
        log.info("cockpit: already active")
        ctx.record("cockpit", "skipped", "already active")
        return
    run(ctx, ["systemctl", "enable", "--now", "cockpit.socket"])
    ctx.record("cockpit", "ok", "enabled on :9090")
    log.info("cockpit: enabled (https://<host>:9090)")
