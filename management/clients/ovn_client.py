"""
Thin wrapper around ovsdbapp's OVN Northbound DB client, per the design
doc: "the ovsdbapp library for OVN Northbound DB calls" and
"api.ls_add('name').execute()".

Import is guarded the same way as libvirt_client.py -- ovsdbapp is a pure
pip package (no compiled system dependency), so on a real deployment it's
installed from requirements.txt into the management service's virtualenv;
it just isn't installable in this network-disabled build sandbox.

NOTE ON OVSDB CONNECTION BOOTSTRAPPING: `connect()` below follows the
standard ovsdbapp IDL-connection pattern (the same one OpenStack's
networking-ovn/neutron-ovn driver uses), but exact constructor signatures
have shifted across ovsdbapp releases. Pin the version in requirements.txt
and validate `connect()` against it in a real environment before relying
on it -- this is the one piece of this codebase we could not exercise
against a live OVN Northbound DB.
"""
from __future__ import annotations

try:
    from ovsdbapp.backend.ovs_idl import connection as ovs_connection
    from ovsdbapp.schema.ovn_northbound import impl_idl as nb_impl
except ImportError:  # pragma: no cover - expected without ovsdbapp installed
    ovs_connection = None
    nb_impl = None


class OvnUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__(
            "ovsdbapp is not installed in this environment. "
            "pip install -r management/requirements.txt in the service's venv."
        )


def _require_ovsdbapp():
    if nb_impl is None:
        raise OvnUnavailableError()


def connect(nb_connection_string: str, timeout: int = 10):
    """
    nb_connection_string example: 'tcp:compute01.cluster.local:6641'
    (see config/hosts.yaml: ovn_central.nb_connection).
    """
    _require_ovsdbapp()
    idl = ovs_connection.OvsdbIdl.from_server(nb_connection_string, "OVN_Northbound")
    conn = ovs_connection.Connection(idl=idl, timeout=timeout)
    return nb_impl.OvnNbApiIdlImpl(conn)


def ensure_logical_switch(api, name: str) -> None:
    """Idempotent: may_exist=True makes ls_add a no-op if it's already there."""
    api.ls_add(name, may_exist=True).execute(check_error=True)


def ensure_logical_router(api, name: str) -> None:
    api.lr_add(name, may_exist=True).execute(check_error=True)


def ensure_router_port(api, router: str, port_name: str, switch: str, switch_port_name: str, mac: str, cidr: str) -> None:
    """
    Connects a logical switch to the mission's logical router: creates a
    router port with the gateway IP, plus the switch-side port that peers
    with it, per the design doc's:
        ovn-nbctl lsp-add control-net control-router-port
        ovn-nbctl lrp-add global-router control-router-port 192.168.100.1/24
    """
    api.lrp_add(router, port_name, mac, [cidr], may_exist=True).execute(check_error=True)
    api.lsp_add(switch, switch_port_name, type="router", may_exist=True).execute(check_error=True)
    api.lsp_set_options(switch_port_name, **{"router-port": port_name}).execute(check_error=True)


def add_vm_port(api, switch: str, port_name: str, mac: str) -> None:
    """
    Create the OVN logical switch port for one VM interface and pin its
    address. `port_name` MUST match the libvirt <virtualport> interfaceid
    for this interface (see core/xml_render.py:port_name) so
    ovn-controller binds the OVS port on the compute host to this exact
    logical port. Per the design doc:
        ovn-nbctl lsp-add control-net vm1-eth0
        ovn-nbctl lsp-set-addresses vm1-eth0 "52:54:00:AA:BB:01"
    """
    api.lsp_add(switch, port_name, may_exist=True).execute(check_error=True)
    api.lsp_set_addresses(port_name, [mac]).execute(check_error=True)


def remove_vm_port(api, port_name: str) -> None:
    api.lsp_del(port_name, if_exists=True).execute(check_error=True)


def remove_logical_switch(api, name: str) -> None:
    api.ls_del(name, if_exists=True).execute(check_error=True)


def remove_logical_router(api, name: str) -> None:
    api.lr_del(name, if_exists=True).execute(check_error=True)


def list_logical_switches(api) -> list[str]:
    return [ls.name for ls in api.ls_list().execute(check_error=True)]


def count_ports_on_switch(api, switch: str) -> int:
    ls = api.lookup("Logical_Switch", switch)
    return len(ls.ports)
