"""
Host networking setup: OVS integration/external bridges via NetworkManager,
and joining the host to the OVN fabric (ovn-controller pointed at the
cluster's southbound DB).

Mirrors the design doc's `configure_network()` snippet, made idempotent
(check-before-create) and config-driven (bridge names / OVN endpoint come
from cluster.yaml + hosts.yaml rather than being hardcoded).
"""
from __future__ import annotations

from .util import RunContext, run, log


def _bridge_exists(ctx: RunContext, bridge: str) -> bool:
    if ctx.dry_run:
        return False
    result = run(ctx, ["ovs-vsctl", "br-exists", bridge], check=False)
    # ovs-vsctl br-exists: exit 0 if it exists, 2 if it does not.
    return result.returncode == 0


def setup_ovs_bridges(ctx: RunContext, cluster_cfg: dict) -> None:
    bridges = cluster_cfg["ovs_bridges"]
    for role, name in bridges.items():
        if _bridge_exists(ctx, name):
            log.info("networking: OVS bridge %s (%s) already exists", name, role)
            ctx.record("ovs_bridge", "skipped", name)
            continue
        run(ctx, ["ovs-vsctl", "add-br", name])
        ctx.record("ovs_bridge", "ok", name)
        log.info("networking: created OVS bridge %s (%s)", name, role)


def attach_physical_to_external_bridge(ctx: RunContext, ifname: str, bridge: str) -> None:
    """
    Hand a physical NIC over to the external bridge (br-ex) via
    NetworkManager's ovs-port connection type, so provider/external traffic
    can reach the OVN overlay. `ifname` is the host's uplink NIC.
    """
    run(
        ctx,
        [
            "nmcli",
            "connection",
            "add",
            "type",
            "ovs-port",
            "conn-name",
            f"ovs-port-{ifname}",
            "ifname",
            ifname,
            "master",
            bridge,
        ],
        check=False,  # idempotent-ish: NM reports "already exists" as non-zero
    )
    ctx.record("attach_physical_nic", "ok", f"{ifname} -> {bridge}")


def join_ovn_fabric(ctx: RunContext, hosts_cfg: dict) -> None:
    """
    Point ovn-controller's external_ids at the cluster's OVN southbound DB
    and (re)start it. This is what lets the host's OVS instance realize
    logical switch/port state pushed via the management service's
    ovn_client.py (ovsdbapp -> Northbound DB -> ovn-northd -> Southbound DB
    -> every host's ovn-controller).
    """
    sb_conn = hosts_cfg["ovn_central"]["sb_connection"]
    run(
        ctx,
        [
            "ovs-vsctl",
            "set",
            "open",
            ".",
            f"external-ids:ovn-remote={sb_conn}",
        ],
    )
    run(ctx, ["ovs-vsctl", "set", "open", ".", "external-ids:ovn-encap-type=geneve"])
    run(ctx, ["systemctl", "enable", "--now", "ovn-controller"])
    ctx.record("ovn_join", "ok", sb_conn)
    log.info("networking: joined OVN fabric via %s", sb_conn)
