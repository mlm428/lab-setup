"""
Cluster-wide health scan: iterates the full host inventory, checks OVN
and Ceph reachability, and rolls up mission deployment counts by state --
per operator request for "a scan/search through the cluster to validate
the infrastructure and ensure it is functioning/available ... a simple
rollup for a good pass/fail ... but then also provide more detailed
status ... dynamic ... search for 1-to-many hosts, running mission
deployments, which missions, etc."

Kept separate from services/validation.py (which checks one *mission
deployment* against its spec) -- this module checks the *infrastructure*
itself, independent of any particular mission.
"""
from __future__ import annotations

from clients import ceph_client, libvirt_client, ovn_client
from core.state import store
from core.types import HostSpec


def check_host(host: HostSpec, ssh_user: str = "root") -> dict:
    """
    Check one compute host: can we open a libvirt connection, and if so
    how many domains are currently defined there.

    Args:
        host: The host to check (uses host.address for the connection).
        ssh_user: Non-root SSH user for the libvirt connection (see
            config/hosts.yaml's management_ssh_user).

    Returns:
        {"host": str, "reachable": bool, "libvirt_ok": bool | None,
         "vm_count": int | None, "detail": str}. `reachable` is False only
        when the connection attempt itself failed (host down, libvirt
        down, network unreachable); `libvirt_ok` is None when `reachable`
        is False (couldn't even check).
    """
    try:
        conn = libvirt_client.connect(host.address, ssh_user=ssh_user)
    except Exception as exc:  # noqa: BLE001 - report as an unreachable host, not a crash
        return {"host": host.name, "reachable": False, "libvirt_ok": None, "vm_count": None, "detail": str(exc)}

    try:
        names = libvirt_client.list_domain_names(conn)
        return {"host": host.name, "reachable": True, "libvirt_ok": True, "vm_count": len(names), "detail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"host": host.name, "reachable": True, "libvirt_ok": False, "vm_count": None, "detail": str(exc)}
    finally:
        conn.close()


def check_ovn(nb_connection: str) -> tuple[bool, str]:
    """
    Check OVN Northbound DB reachability.

    Args:
        nb_connection: OVN NB connection string (config/hosts.yaml's ovn_central.nb_connection).

    Returns:
        (reachable, detail) -- detail holds the error message when unreachable.
    """
    try:
        api = ovn_client.connect(nb_connection)
        ovn_client.list_logical_switches(api)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_ceph(conf_path: str) -> tuple[bool, str]:
    """
    Check Ceph cluster reachability.

    Args:
        conf_path: Path to the ceph.conf identifying the runtime cluster.

    Returns:
        (reachable, detail) -- detail holds the FSID on success or the
        error message on failure.
    """
    try:
        fsid = ceph_client.get_fsid(conf_path)
        return True, fsid
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def scan_cluster(hosts: dict[str, HostSpec], ovn_nb_connection: str, ceph_conf_path: str | None, ssh_user: str = "root") -> dict:
    """
    Run a full infrastructure scan: every host, OVN, Ceph (if configured),
    and a rollup of currently-tracked mission deployments by state.

    Args:
        hosts: Full host inventory to check.
        ovn_nb_connection: OVN NB connection string.
        ceph_conf_path: Runtime Ceph cluster's ceph.conf path, or None to
            skip the Ceph check (e.g. an all-local_qcow2 cluster).
        ssh_user: Non-root SSH user for each host's libvirt connection
            (see config/hosts.yaml's management_ssh_user).

    Returns:
        A dict matching api/models.py's ClusterHealthResponse shape:
        {"ok": bool, "ovn_reachable": bool, "ceph_reachable": bool | None,
         "hosts": [...], "missions_total": int,
         "missions_by_state": {state: count}, "detail": str}.
        `ok` is True only if every host is reachable with libvirt_ok, OVN
        is reachable, and Ceph is reachable (when checked).
    """
    host_results = [check_host(h, ssh_user=ssh_user) for h in hosts.values()]
    ovn_ok, ovn_detail = check_ovn(ovn_nb_connection)

    ceph_ok: bool | None = None
    ceph_detail = ""
    if ceph_conf_path:
        ceph_ok, ceph_detail = check_ceph(ceph_conf_path)

    missions = store.list()
    missions_by_state: dict[str, int] = {}
    for m in missions:
        key = m.state.value
        missions_by_state[key] = missions_by_state.get(key, 0) + 1

    all_hosts_ok = all(h["reachable"] and h["libvirt_ok"] for h in host_results)
    overall_ok = all_hosts_ok and ovn_ok and (ceph_ok is not False)

    detail_parts = []
    if not ovn_ok:
        detail_parts.append(f"OVN unreachable: {ovn_detail}")
    if ceph_ok is False:
        detail_parts.append(f"Ceph unreachable: {ceph_detail}")
    unreachable_hosts = [h["host"] for h in host_results if not (h["reachable"] and h["libvirt_ok"])]
    if unreachable_hosts:
        detail_parts.append(f"hosts with issues: {unreachable_hosts}")

    return {
        "ok": overall_ok,
        "ovn_reachable": ovn_ok,
        "ceph_reachable": ceph_ok,
        "hosts": host_results,
        "missions_total": len(missions),
        "missions_by_state": missions_by_state,
        "detail": "; ".join(detail_parts) if detail_parts else "all checks passed",
    }
