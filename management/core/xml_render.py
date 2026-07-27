"""
Renders a libvirt domain XML document for a single VM, from its VMSpec
plus mission/cluster context (assigned MACs, storage backend, GPU mdev
UUID). This is the direct implementation of the design doc's:

    domain_xml = render_template("vm.xml.j2", **vm_params)
    dom = conn.defineXML(domain_xml)
    dom.create()

...minus the actual libvirt connection (that's
management/clients/libvirt_client.py); this module only produces the XML
string and is fully testable with just Jinja2 + the standard library's
xml.etree.ElementTree, both available without any of fastapi/pydantic/
libvirt-python/rados/rbd/ovsdbapp installed.
"""
from __future__ import annotations

import uuid as uuid_lib
import xml.dom.minidom as minidom
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .types import VMSpec

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Fixed namespace so uuid5(NAMESPACE, f"{mission}:{vm}") is stable across
# runs -- required for the "Repeatability" acceptance criterion (destroy +
# redeploy yields identical VM UUIDs, not just names/MACs).
MISSION_UUID_NAMESPACE = uuid_lib.UUID("6f9169d4-1b40-4d6f-9d2a-8f6c9a2e6b90")


def deterministic_vm_uuid(mission_name: str, vm_name: str) -> str:
    """Deterministic libvirt domain UUID for (mission_name, vm_name), stable across redeploys."""
    return str(uuid_lib.uuid5(MISSION_UUID_NAMESPACE, f"{mission_name}:{vm_name}"))


def switch_name(mission_name: str, mission_id: str, network_name: str) -> str:
    """
    Deployment-scoped OVN logical switch name. Per the design doc:
    "Identical VLAN IDs across different missions do not conflict, thanks
    to independent logical switch instances" -- and, per operator
    requirement, the SAME mission definition deployed twice concurrently
    must be "totally network isolated ... in its own segmented network",
    not just distinguishable by name. Scoping by `mission_id` (unique per
    deployment) rather than `mission_name` alone (which repeats across
    deployments of the same mission) is what makes that true: two
    deployments of the identical "Mission-Alpha" mission get two distinct
    switches for its "control" network, not one shared switch that
    `may_exist=True` would silently make the second deployment reuse.
    `mission_name` is included purely for human readability in
    `ovn-nbctl show` output; `mission_id` is what actually guarantees
    uniqueness.
    """
    return f"{mission_name}-{mission_id}-{network_name}"


def router_name(mission_name: str, mission_id: str) -> str:
    """Deployment-scoped OVN logical router name -- see switch_name's docstring for why mission_id (not just mission_name) is included."""
    return f"{mission_name}-{mission_id}-router"


def router_port_name(mission_name: str, mission_id: str, network_name: str) -> str:
    """Deployment-scoped name for the router-side port connecting to one of the mission's networks."""
    return f"{mission_name}-{mission_id}-{network_name}-rp"


def port_name(mission_name: str, mission_id: str, vm_name: str, network_name: str) -> str:
    """
    OVN logical switch port name for this VM's interface on this network.
    Must match exactly what management/services/networking.py passes to
    ovsdbapp's lsp_add, since the libvirt <virtualport> interfaceid
    references this same string to bind the OVS port to the OVN logical
    port.

    Scoped by mission_id (not just mission_name): OVN's
    Logical_Switch_Port table is global (not per-switch), so two
    different missions -- or two concurrent deployments of the same
    mission -- that happen to both define a VM named e.g. "db01" on a
    network named "control" must not produce the same port name.
    """
    return f"{mission_name}-{mission_id}-{vm_name}-{network_name}"


@dataclass
class StorageContext:
    """
    Storage backend selection + connection info needed to render a VM's
    disk (or lack thereof) and to actually provision it
    (services/storage.py). Runtime (where linked-clone VM disks live) and
    golden (where source images are read from) are deliberately separate
    -- see config/storage.yaml.

    Attributes:
        backend: "ceph_rbd" | "local_qcow2" -- where THIS VM's disk (the
            clone, not the golden source) lives.
        ceph_pool: Runtime pool name (only used when backend=="ceph_rbd").
        ceph_client_id: Ceph client id used for the runtime pool's cephx auth.
        ceph_secret_uuid: libvirt secret UUID holding that client's cephx key.
        ceph_monitors: Runtime cluster's monitor list, [{"name":..,"port":..}, ...].
        local_qcow2_dir: Directory holding qcow2 clone files (only used
            when backend=="local_qcow2").
    """
    backend: str
    ceph_pool: str = "mission-runtime"
    ceph_client_id: str = "libvirt"
    ceph_secret_uuid: str = ""
    ceph_monitors: list[dict] | None = None
    local_qcow2_dir: str = "/var/lib/libvirt/images"


def _env() -> Environment:
    """Build the Jinja2 environment used to render vm.xml.j2 (StrictUndefined so a missing template var fails loudly, not silently)."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_domain_xml(
    mission_name: str,
    mission_id: str,
    vm_name: str,
    vm: VMSpec,
    macs: list[str],
    storage: StorageContext,
    gpu_mdev_uuid: str | None = None,
    ovn_integration_bridge: str = "br-int",
) -> str:
    """
    Render a complete libvirt domain XML document for one VM.

    Args:
        mission_name: Owning mission's name (used for OVN naming + UUID derivation).
        mission_id: This specific deployment's id -- embedded in the
            rendered <metadata> block alongside mission_name and vm_name,
            so a live cluster scan can trace a running VM back to its
            mission deployment purely from its own domain XML (see
            services/reconciliation.py), independent of whether this
            service's own database still has a record of it.
        vm_name: This VM's name within the mission.
        vm: The VM's spec (type, cpu, memory, interfaces, gpu_profile, etc.).
        macs: Fully-resolved MAC addresses, one per interface, in the same
            order as vm.interface_names() -- see core/macs.py:resolve_mission_macs.
        storage: Where to source this VM's disk from (ignored for pxe VMs).
        gpu_mdev_uuid: The specific MIG/vGPU mediated-device UUID to
            attach, required if vm.has_gpu is True and forbidden
            otherwise (see config/hosts.yaml's gpu_devices, matched by
            vm.gpu_profile at deploy time).
        ovn_integration_bridge: Host's OVS integration bridge name (every
            NIC binds to this bridge, with OVN doing the logical
            switching -- see port_name's docstring).

    Returns:
        A pretty-printed, well-formed libvirt domain XML string.

    Raises:
        ValueError: if `macs` doesn't have exactly one entry per
            interface, or if vm.has_gpu and gpu_mdev_uuid disagree about
            whether this VM needs a GPU.
    """
    interface_names = vm.interface_names()
    if len(macs) != len(interface_names):
        raise ValueError(
            f"VM '{vm_name}': {len(macs)} MAC(s) provided for {len(interface_names)} interface(s)"
        )
    if vm.has_gpu and not gpu_mdev_uuid:
        raise ValueError(f"VM '{vm_name}': gpu_profile={vm.gpu_profile!r} but no gpu_mdev_uuid supplied")
    if not vm.has_gpu and gpu_mdev_uuid:
        raise ValueError(f"VM '{vm_name}': gpu_mdev_uuid supplied but this VM has no gpu_profile")

    interfaces = [
        {
            "network": net_name,
            "mac": mac,
            "port_name": port_name(mission_name, mission_id, vm_name, net_name),
        }
        for net_name, mac in zip(interface_names, macs)
    ]

    gpu_devices = [{"mdev_uuid": gpu_mdev_uuid}] if gpu_mdev_uuid else []

    template = _env().get_template("vm.xml.j2")
    raw_xml = template.render(
        vm_name=vm_name,
        mission_id=mission_id,
        mission_name=mission_name,
        uuid=deterministic_vm_uuid(mission_name, vm_name),
        memory_mb=vm.memory_mb,
        cpu=vm.cpu,
        vm_type=vm.type.value if hasattr(vm.type, "value") else vm.type,
        interfaces=interfaces,
        gpu_devices=gpu_devices,
        ovn_integration_bridge=ovn_integration_bridge,
        storage_backend=storage.backend,
        ceph_pool=storage.ceph_pool,
        ceph_client_id=storage.ceph_client_id,
        ceph_secret_uuid=storage.ceph_secret_uuid,
        ceph_monitors=storage.ceph_monitors or [],
        local_qcow2_path=f"{storage.local_qcow2_dir}/{vm_name}_clone.qcow2",
    )
    return _pretty_print(raw_xml)


def _pretty_print(xml_str: str) -> str:
    """Re-serialize with consistent indentation; purely cosmetic, does not
    change element structure, attributes, or text content."""
    dom = minidom.parseString(xml_str)
    pretty = dom.toprettyxml(indent="  ")
    # minidom emits a standalone XML declaration and blank lines between
    # elements; strip both for a clean, libvirt-friendly document.
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines[1:]) + "\n"
