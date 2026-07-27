"""
Thin wrapper around the libvirt Python API (`import libvirt`), per the
design doc: "the libvirt Python bindings for VM operations". Import is
guarded so the rest of this codebase can be imported/tested on a machine
that doesn't have python3-libvirt installed (e.g. this build sandbox, or a
developer's laptop) -- only calling one of these functions without the
real binding installed raises, not importing the module.

On a bootstrapped RHEL host, python3-libvirt is installed by
bootstrap/modules/packages.py, so this guard never triggers in
production.
"""
from __future__ import annotations

from typing import Optional

try:
    import libvirt
except ImportError:  # pragma: no cover - expected without python3-libvirt installed
    libvirt = None


class LibvirtUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__(
            "python3-libvirt is not installed on this host. Install it via "
            "bootstrap.py / dnf install python3-libvirt, then retry."
        )


def _require_libvirt():
    if libvirt is None:
        raise LibvirtUnavailableError()


def connect(host: Optional[str] = None, ssh_user: str = "root"):
    """
    Open a libvirt connection. `host` is a compute node name/address for a
    remote connection (qemu+ssh://<ssh_user>@<host>/system); None connects
    to the local hypervisor (qemu:///system) -- useful when the management
    service itself runs on a compute node.

    Args:
        host: Compute host address, or None for a local connection.
        ssh_user: Remote SSH user for the qemu+ssh:// URI. Defaults to
            "root" only for a bare/manual call -- real callers should
            always pass config/hosts.yaml's `management_ssh_user`
            explicitly (see services/cluster_config.py) rather than rely
            on this default, since STIG-hardened hosts commonly disable
            direct root SSH login. A non-root user works fine here as
            long as it's a member of the `libvirt` group on the target
            host (libvirtd's default polkit rules grant that group full
            local access to the libvirt socket) -- no sudo needed.
    """
    _require_libvirt()
    uri = f"qemu+ssh://{ssh_user}@{host}/system" if host else "qemu:///system"
    conn = libvirt.open(uri)
    if conn is None:
        raise RuntimeError(f"libvirt.open() failed for URI {uri}")
    return conn


def define_and_start(conn, domain_xml: str):
    """
    virDomainDefineXML + create(), per the design doc:
        dom = conn.defineXML(domain_xml)
        dom.create()
    """
    _require_libvirt()
    dom = conn.defineXML(domain_xml)
    dom.create()
    return dom


def destroy_and_undefine(conn, vm_name: str) -> None:
    """Idempotent teardown: ignore "not found" errors so repeated
    teardown calls (or a teardown after partial failure) don't raise."""
    _require_libvirt()
    try:
        dom = conn.lookupByName(vm_name)
    except libvirt.libvirtError:
        return  # already gone
    try:
        if dom.isActive():
            dom.destroy()
    except libvirt.libvirtError:
        pass
    try:
        dom.undefine()
    except libvirt.libvirtError:
        pass


def list_domain_names(conn) -> list[str]:
    _require_libvirt()
    return [dom.name() for dom in conn.listAllDomains()]


def domain_info(conn, vm_name: str) -> dict:
    """Returns {state, max_mem_kb, mem_kb, vcpus, cpu_time_ns} via
    virDomainInfo, used by services/validation.py to confirm CPU/memory
    match the mission spec."""
    _require_libvirt()
    dom = conn.lookupByName(vm_name)
    state, max_mem, mem, vcpus, cpu_time = dom.info()
    return {
        "state": state,
        "max_mem_kb": max_mem,
        "mem_kb": mem,
        "vcpus": vcpus,
        "cpu_time_ns": cpu_time,
    }


def domain_xml_desc(conn, vm_name: str) -> str:
    _require_libvirt()
    dom = conn.lookupByName(vm_name)
    return dom.XMLDesc()
