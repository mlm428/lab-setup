"""
Compute service: turns a VMSpec + assigned MACs + storage/GPU context into
a defined, running libvirt domain (or tears one down). This is the
service-layer glue between core/xml_render.py (pure XML generation,
fully unit-tested) and clients/libvirt_client.py (the actual libvirt
Python API calls, which need python3-libvirt + a real hypervisor and so
cannot run in this build sandbox).
"""
from __future__ import annotations

from clients import libvirt_client
from core.types import MissionSpec, VMSpec
from core.xml_render import StorageContext, render_domain_xml


def define_and_start_vm(
    mission: MissionSpec,
    vm_name: str,
    vm: VMSpec,
    macs: list[str],
    host_address: str,
    storage_ctx: StorageContext,
    gpu_pci_addresses: list[str] | None,
    ovn_integration_bridge: str = "br-int",
):
    """
    Renders this VM's domain XML and defines+starts it on its placed host.
    `macs` must be the same list already used to create this VM's OVN
    logical ports (services/networking.py's add_vm_ports) -- MACs for a
    mission are computed exactly once (core.macs.assign_all_macs) and
    threaded through by the caller (workers/deploy.py), rather than
    recomputed here, so the OVN-pinned address and the libvirt <mac> can
    never drift apart.

    Returns the libvirt domain object (or raises LibvirtUnavailableError /
    libvirt.libvirtError on a real host if something goes wrong).
    """
    domain_xml = render_domain_xml(
        mission_name=mission.name,
        vm_name=vm_name,
        vm=vm,
        macs=macs,
        storage=storage_ctx,
        gpu_pci_addresses=gpu_pci_addresses,
        ovn_integration_bridge=ovn_integration_bridge,
    )
    conn = libvirt_client.connect(host_address)
    try:
        return libvirt_client.define_and_start(conn, domain_xml)
    finally:
        conn.close()


def destroy_vm(vm_name: str, host_address: str) -> None:
    conn = libvirt_client.connect(host_address)
    try:
        libvirt_client.destroy_and_undefine(conn, vm_name)
    finally:
        conn.close()


def list_running_vm_names(host_addresses: list[str]) -> list[str]:
    """Aggregate VM names across every compute host (used by
    services/validation.py for the 'exactly N domains exist' check)."""
    names: list[str] = []
    for host in host_addresses:
        conn = libvirt_client.connect(host)
        try:
            names.extend(libvirt_client.list_domain_names(conn))
        finally:
            conn.close()
    return names
