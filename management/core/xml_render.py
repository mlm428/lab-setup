"""
Renders a libvirt domain XML document for a single VM, from its VMSpec
plus mission/cluster context (assigned MACs, storage backend, GPU PCI
addresses). This is the direct implementation of the design doc's:

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
    return str(uuid_lib.uuid5(MISSION_UUID_NAMESPACE, f"{mission_name}:{vm_name}"))


def switch_name(mission_name: str, network_name: str) -> str:
    """
    Mission-scoped OVN logical switch name. Per the design doc: "Identical
    VLAN IDs across different missions do not conflict, thanks to
    independent logical switch instances" -- this is what makes that true:
    two missions both defining a "control" network get two distinct
    switches, e.g. "Mission-Alpha-control" and "Mission-Bravo-control".
    """
    return f"{mission_name}-{network_name}"


def router_name(mission_name: str) -> str:
    return f"{mission_name}-router"


def router_port_name(mission_name: str, network_name: str) -> str:
    return f"{mission_name}-{network_name}-rp"


def port_name(mission_name: str, vm_name: str, network_name: str) -> str:
    """
    OVN logical switch port name for this VM's interface on this network.
    Must match exactly what management/services/networking.py passes to
    `ovn-nbctl lsp-add` / `ovsdbapp`'s ls_add API, since the libvirt
    <virtualport> interfaceid references this same string to bind the OVS
    port to the OVN logical port.

    Scoped by mission name: OVN's Logical_Switch_Port table is global (not
    per-switch), so two different missions that happen to both define a VM
    named e.g. "db01" on a network named "control" must not produce the
    same port name.
    """
    return f"{mission_name}-{vm_name}-{network_name}"


def parse_pci_address(address: str) -> dict:
    """
    Parse a PCI address string like '0000:83:00.0' into the
    domain/bus/slot/function components libvirt's hostdev XML needs.
    Accepts both '0000:83:00.0' and '83:00.0' (domain defaults to '0000').
    """
    addr = address.strip()
    if addr.count(":") == 2:
        domain, bus, rest = addr.split(":")
    elif addr.count(":") == 1:
        domain = "0000"
        bus, rest = addr.split(":")
    else:
        raise ValueError(f"unrecognized PCI address format: {address!r}")

    if "." not in rest:
        raise ValueError(f"unrecognized PCI address format: {address!r}")
    slot, function = rest.split(".")
    return {
        "domain": domain.zfill(4),
        "bus": bus.zfill(2),
        "slot": slot.zfill(2),
        "function": function.zfill(1),
    }


@dataclass
class StorageContext:
    backend: str                      # "ceph_rbd" | "local_qcow2"
    ceph_pool: str = "mission-images"
    ceph_client_id: str = "libvirt"
    ceph_secret_uuid: str = ""
    ceph_monitors: list[dict] | None = None   # [{"name": ..., "port": ...}, ...]
    local_qcow2_dir: str = "/var/lib/libvirt/images"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_domain_xml(
    mission_name: str,
    vm_name: str,
    vm: VMSpec,
    macs: list[str],
    storage: StorageContext,
    gpu_pci_addresses: list[str] | None = None,
    ovn_integration_bridge: str = "br-int",
) -> str:
    if len(macs) != len(vm.interfaces):
        raise ValueError(
            f"VM '{vm_name}': {len(macs)} MAC(s) provided for {len(vm.interfaces)} interface(s)"
        )
    if vm.gpu and not gpu_pci_addresses:
        raise ValueError(f"VM '{vm_name}': gpu=true but no gpu_pci_addresses supplied")
    if not vm.gpu and gpu_pci_addresses:
        raise ValueError(f"VM '{vm_name}': gpu_pci_addresses supplied but gpu=false")

    interfaces = [
        {
            "network": net_name,
            "mac": mac,
            "port_name": port_name(mission_name, vm_name, net_name),
        }
        for net_name, mac in zip(vm.interfaces, macs)
    ]

    gpu_devices = [parse_pci_address(addr) for addr in (gpu_pci_addresses or [])]

    template = _env().get_template("vm.xml.j2")
    raw_xml = template.render(
        vm_name=vm_name,
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
        local_qcow2_path=f"{storage.local_qcow2_dir}/{vm_name}.qcow2",
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
