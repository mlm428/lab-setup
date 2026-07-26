"""
Networking service: turns a mission's `networks` + `vms` into OVN logical
switches, a mission-scoped logical router, and per-VM-interface logical
ports with static MAC addresses. Mirrors the design doc's:

    ovn-nbctl ls-add control-net
    ovn-nbctl ls-add storage-net
    ovn-nbctl lr-add global-router
    ...
    ovn-nbctl lsp-add control-net vm1-eth0
    ovn-nbctl lsp-set-addresses vm1-eth0 "52:54:00:AA:BB:01"

...via ovsdbapp instead of shelling out to ovn-nbctl, and with every name
mission-scoped (see core/xml_render.py's switch_name/router_name/
port_name) so multiple missions -- or multiple concurrent *deployments* of
the same mission -- can safely coexist on the same OVN Northbound DB.
"""
from __future__ import annotations

import hashlib

from clients import ovn_client
from core.types import MissionSpec
from core.xml_render import port_name, router_name, router_port_name, switch_name


def _router_gateway_cidr(vlan_id: int) -> str:
    """
    Derive a deterministic /24 gateway CIDR for a mission network's router
    port, one octet per VLAN id (e.g. VLAN 110 -> 10.110.0.1/24). The
    design doc's own example (`192.168.100.1/24`) is a single hardcoded
    value; this generalizes it so a mission with N networks gets N
    distinct subnets without operator input. Prototype-scope: a real
    deployment would want this driven by IPAM instead.
    """
    third_octet = vlan_id % 256
    return f"10.{third_octet}.0.1/24"


def _router_port_mac(mission_name: str, network_name: str) -> str:
    """
    Deterministic MAC for a mission's router port on one network. Uses a
    separate hash (not core/macs.py's per-deployment prefix scheme, which
    is reserved for actual VM NICs) with a fixed 0xff marker octet so
    router ports are trivially distinguishable from VM NICs when reading a
    packet capture; a mission has at most a handful of these, so a 2-byte
    hash space is more than enough to avoid collisions between them.
    """
    digest = hashlib.sha256(f"{mission_name}:router:{network_name}".encode("utf-8")).digest()
    return f"52:54:00:ff:{digest[0]:02x}:{digest[1]:02x}"


def provision_networks(api, mission: MissionSpec) -> dict[str, str]:
    """
    Create one OVN logical switch per mission network, a logical router,
    and connect every switch to the router.

    Args:
        api: A connected ovsdbapp OVN Northbound API object (see
            clients/ovn_client.py:connect).
        mission: The mission whose `networks` to provision.

    Returns:
        {network_name: ovn_switch_name} for callers (e.g. add_vm_ports)
        that need the OVN-side switch name.
    """
    router = router_name(mission.name)
    ovn_client.ensure_logical_router(api, router)

    switches: dict[str, str] = {}
    for net_name, vlan_id in mission.networks.items():
        switch = switch_name(mission.name, net_name)
        ovn_client.ensure_logical_switch(api, switch)
        switches[net_name] = switch

        rp_name = router_port_name(mission.name, net_name)
        ovn_client.ensure_router_port(
            api,
            router=router,
            port_name=rp_name,
            switch=switch,
            switch_port_name=f"{rp_name}-sw",
            mac=_router_port_mac(mission.name, net_name),
            cidr=_router_gateway_cidr(vlan_id),
        )

    return switches


def add_vm_ports(api, mission: MissionSpec, resolved_macs: dict[str, list[str]]) -> None:
    """
    Create one OVN logical port per VM interface and pin its already-
    resolved MAC address.

    Args:
        api: A connected ovsdbapp OVN Northbound API object.
        mission: The mission whose VMs to create ports for.
        resolved_macs: {vm_name: [mac, ...]} in the same order as each
            VM's interface_names() -- computed once at mission
            registration by core/macs.py:resolve_mission_macs (see
            core/state.py:MissionStore.register_deployment) and
            threaded through unchanged, so this function and
            services/compute.py's define_and_start_vm always agree on
            every interface's address by construction.

    Returns:
        None.
    """
    for vm_name, vm in mission.vms.items():
        macs = resolved_macs[vm_name]
        for (net_name, _iface), mac in zip(vm.interfaces.items(), macs):
            switch = switch_name(mission.name, net_name)
            port = port_name(mission.name, vm_name, net_name)
            ovn_client.add_vm_port(api, switch, port, mac)


def teardown_mission_networking(api, mission: MissionSpec) -> None:
    """
    Idempotently remove every VM port, every switch, and the mission's
    router for one deployment. Order matters -- ports before switches.

    Args:
        api: A connected ovsdbapp OVN Northbound API object.
        mission: The mission (deployment) to tear down networking for.

    Returns:
        None.
    """
    for vm_name, vm in mission.vms.items():
        for net_name in vm.interface_names():
            port = port_name(mission.name, vm_name, net_name)
            ovn_client.remove_vm_port(api, port)

    for net_name in mission.networks:
        ovn_client.remove_logical_switch(api, switch_name(mission.name, net_name))

    ovn_client.remove_logical_router(api, router_name(mission.name))
