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
    gpu_mdev_uuid: str | None,
    ovn_integration_bridge: str = "br-int",
):
    """
    Render this VM's domain XML and define+start it on its placed host.

    Args:
        mission: Owning mission (for OVN naming + UUID derivation).
        vm_name: This VM's name.
        vm: The VM's spec.
        macs: Already-resolved MAC addresses in interface order (see
            core/macs.py:resolve_mission_macs) -- MACs for a mission are
            computed exactly once, at registration time, and threaded
            through by the caller (workers/deploy.py) rather than
            recomputed here, so the OVN-pinned address and the libvirt
            <mac> can never drift apart.
        host_address: Real network address of the host to define this VM
            on (from config/hosts.yaml's `address`, not the inventory key).
        storage_ctx: Runtime storage backend/connection info.
        gpu_mdev_uuid: The specific MIG/vGPU mdev UUID to attach, or None
            for a VM with no GPU requirement.
        ovn_integration_bridge: Host's OVS integration bridge name.

    Returns:
        The libvirt domain object.

    Raises:
        clients.libvirt_client.LibvirtUnavailableError: if python3-libvirt
            isn't installed on this host.
        libvirt.libvirtError: on a real host, for any libvirt-side failure
            (e.g. malformed XML, resource conflict, host out of capacity).
    """
    domain_xml = render_domain_xml(
        mission_name=mission.name,
        vm_name=vm_name,
        vm=vm,
        macs=macs,
        storage=storage_ctx,
        gpu_mdev_uuid=gpu_mdev_uuid,
        ovn_integration_bridge=ovn_integration_bridge,
    )
    conn = libvirt_client.connect(host_address)
    try:
        return libvirt_client.define_and_start(conn, domain_xml)
    finally:
        conn.close()


def destroy_vm(vm_name: str, host_address: str) -> None:
    """
    Idempotently destroy and undefine one VM by name on the given host.

    Args:
        vm_name: The VM's libvirt domain name.
        host_address: Real network address of the host it's running on.

    Returns:
        None. Silently succeeds if the VM is already gone (see
        clients.libvirt_client.destroy_and_undefine).
    """
    conn = libvirt_client.connect(host_address)
    try:
        libvirt_client.destroy_and_undefine(conn, vm_name)
    finally:
        conn.close()


def list_running_vm_names(host_addresses: list[str]) -> list[str]:
    """
    Aggregate VM names across every given compute host.

    Args:
        host_addresses: Real network addresses of the hosts to query.

    Returns:
        Combined list of every domain name found across all given hosts
        (used by services/validation.py's "exactly N domains exist" check,
        and by the cluster health endpoint).
    """
    names: list[str] = []
    for host in host_addresses:
        conn = libvirt_client.connect(host)
        try:
            names.extend(libvirt_client.list_domain_names(conn))
        finally:
            conn.close()
    return names
