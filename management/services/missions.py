"""
Mission orchestration: parses a mission definition (YAML file or API
request body), validates it against the cluster's host inventory
(capacity + GPU profile check, per core/placement.py), atomically resolves
its MAC addresses for this specific deployment (core/macs.py), and
registers it in the in-memory state store. The actual provisioning work
(networks, storage, VMs, validation) happens in workers/deploy.py as a
background task -- this module only handles the synchronous "is this
request even valid, and what deployment ID do we track it under" part, so
`POST /missions` can return quickly (202 Accepted) as the design doc
specifies.
"""
from __future__ import annotations

from pathlib import Path

from core import config_loader
from core.macs import resolve_mission_macs
from core.placement import reserve_gpu_devices, validate_placement
from core.state import MissionStatus, store
from core.types import GpuDeviceSpec, HostSpec, InterfaceSpec, MissionSpec, VMSpec, VMType


class MissionValidationError(ValueError):
    """Raised when a mission fails placement/capacity validation -- the API layer turns this into a 4xx response."""


def load_host_inventory(config_dir: Path | None = None) -> dict[str, HostSpec]:
    """
    Load the cluster's host inventory from config/hosts.yaml (the single
    source of truth shared with bootstrap/).

    Args:
        config_dir: Override for the config/ directory (defaults to the
            repo-root config/ via core.config_loader.CONFIG_DIR).

    Returns:
        {host_name: HostSpec}, including each host's GPU device slices.
    """
    data = config_loader.load_hosts_config(config_dir)
    hosts = {}
    for name, entry in data["hosts"].items():
        gpu_devices = [
            GpuDeviceSpec(profile=d["profile"], mdev_uuid=d["mdev_uuid"])
            for d in entry.get("gpu_devices", [])
        ]
        hosts[name] = HostSpec(
            name=name,
            cpus=entry["cpus"],
            memory_mb=entry["memory_mb"],
            address=entry.get("address", name),
            gpu_devices=gpu_devices,
        )
    return hosts


def _parse_interfaces(raw_interfaces: dict) -> dict[str, InterfaceSpec]:
    """
    Parse a VM's `interfaces:` mapping from mission YAML/JSON into
    {network_name: InterfaceSpec}, preserving order.

    Accepts two shapes for each entry's value, for convenience:
      - a mapping: {mac_suffix: "10:00:00"} (or {} / omitted mac_suffix for "generate one")
      - null: interfaces: {control: null} -- same as {} (no configured suffix)

    Args:
        raw_interfaces: The parsed YAML/JSON `interfaces:` value.

    Returns:
        {network_name: InterfaceSpec} in the same order as `raw_interfaces`.
    """
    result: dict[str, InterfaceSpec] = {}
    for net_name, entry in raw_interfaces.items():
        mac_suffix = None
        if isinstance(entry, dict):
            mac_suffix = entry.get("mac_suffix")
        result[net_name] = InterfaceSpec(network=net_name, mac_suffix=mac_suffix)
    return result


def mission_spec_from_dict(data: dict) -> MissionSpec:
    """
    Build a core.types.MissionSpec from a plain dict (already parsed from
    YAML/JSON, or from api/models.py's Pydantic MissionDefinition.dict()).
    Kept separate from Pydantic so it's testable without pydantic
    installed.

    Args:
        data: Parsed mission document. Expected top-level keys: `mission`
            (name), `networks` (name -> VLAN), `vms` (name -> VM entry),
            `placement` (VM name -> host name), and optionally
            `file_version` (mission file's own revision tag -- see
            management/mission_defs/README.md).

    Returns:
        A validated MissionSpec (raises inside VMSpec/MissionSpec's own
        __post_init__ for any structural problem -- unknown network
        references, placement/VM mismatches, duplicate mac_suffix within
        one VM, etc.).
    """
    vms = {}
    for vm_name, vm_data in data["vms"].items():
        gpu_data = vm_data.get("gpu")
        gpu_profile = gpu_data.get("profile") if isinstance(gpu_data, dict) else gpu_data

        vms[vm_name] = VMSpec(
            name=vm_name,
            type=VMType(vm_data["type"]),
            cpu=vm_data["cpu"],
            memory_mb=vm_data["memory"],
            interfaces=_parse_interfaces(vm_data["interfaces"]),
            image=vm_data.get("image"),
            image_revision=vm_data.get("image_revision"),
            gpu_profile=gpu_profile,
        )
    return MissionSpec(
        name=data["mission"],
        networks=dict(data["networks"]),
        vms=vms,
        placement=dict(data["placement"]),
        file_version=data.get("file_version"),
    )


def load_mission_yaml(path: Path) -> MissionSpec:
    """
    Load and parse one mission definition file.

    Args:
        path: Path to a mission YAML file (see
            management/mission_defs/README.md for the schema, or
            management/mission_defs/template.yaml for an annotated example).

    Returns:
        A validated MissionSpec.
    """
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return mission_spec_from_dict(data)


def register_mission(mission: MissionSpec, hosts: dict[str, HostSpec] | None = None) -> MissionStatus:
    """
    Validate a mission's placement against host capacity (including
    GPU-profile availability), then atomically resolve this specific
    deployment's MAC addresses AND its GPU device allocations, and
    register it in the state store with state=Pending.

    Args:
        mission: The mission to register.
        hosts: Host inventory to validate against (defaults to
            load_host_inventory()).

    Returns:
        The new MissionStatus (mission_id, mac_prefix, resolved_macs,
        gpu_allocations, and the retained `spec` are all populated --
        ready for workers/deploy.py to pick up).

    Raises:
        MissionValidationError: if placement is infeasible (oversubscribed
            CPU/memory/GPU-profile on some host, or references an unknown
            host) -- nothing is registered in this case.
        ValueError: if the mission's configured mac_suffix values collide
            across different VMs (see core/macs.py:resolve_mission_macs) --
            also nothing registered.
        RuntimeError: if every GPU device matching some VM's requested
            profile is already reserved by this or another currently-
            active deployment (see core/placement.py:reserve_gpu_devices)
            -- also nothing registered. This is a *cross-deployment*
            check that validate_placement's own capacity check cannot
            make on its own, since that check only looks at this
            mission's own VMs against total (not currently-available)
            host capacity.
    """
    hosts = hosts if hosts is not None else load_host_inventory()
    report = validate_placement(mission, hosts)
    if not report.ok:
        raise MissionValidationError(
            f"placement is infeasible for mission '{mission.name}': {report.as_dict()['violations']}"
        )
    return store.register_deployment(mission.name, mission, hosts, resolve_mission_macs, reserve_gpu_devices)


def get_mission(mission_id: str) -> MissionStatus | None:
    """Look up one mission deployment's live status by id."""
    return store.get(mission_id)


def list_missions() -> list[MissionStatus]:
    """Return every tracked mission deployment (any state)."""
    return store.list()
