"""
NetworkManager client -- listed in the design doc's "Common Clients" as
`nm_client.py`. Host-level bridge/bond/VLAN configuration is primarily
bootstrap.py's job (bootstrap/modules/networking.py) since it only needs
to run once per host, not once per mission; this module re-exports the
same subprocess-based approach for the rare case a mission needs a
host-network change at deploy time (e.g. an additional uplink for a new
external network), so the management service doesn't need a second
implementation of "how do I talk to nmcli".
"""
from __future__ import annotations

import subprocess


def bridge_exists(name: str) -> bool:
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return name in result.stdout.splitlines()


def add_bridge(name: str, autoconnect: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "nmcli", "connection", "add",
            "type", "bridge",
            "con-name", name,
            "ifname", name,
            "autoconnect", "yes" if autoconnect else "no",
        ],
        capture_output=True,
        text=True,
    )


def attach_port(ifname: str, master: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "nmcli", "connection", "add",
            "type", "ovs-port",
            "conn-name", f"ovs-port-{ifname}",
            "ifname", ifname,
            "master", master,
        ],
        capture_output=True,
        text=True,
    )
