"""
Mission orchestration: validates a submitted mission against the cluster's
host inventory (capacity check, per core/placement.py), registers it in
the in-memory state store, and exposes the get/list operations the
FastAPI routes need. The actual provisioning work (networks, storage, VMs,
validation) happens in workers/deploy.py as a background task -- this
module only handles the synchronous "is this request even valid, and
what ID do we track it under" part, so `POST /missions` can return
quickly (202 Accepted) as the design doc specifies.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.placement import validate_placement
from core.state import MissionStatus, store
from core.types import HostSpec, MissionSpec, VMSpec, VMType

INVENTORY_DIR = Path(__file__).resolve().parent.parent / "inventory"


class MissionValidationError(ValueError):
    pass


def load_host_inventory(path: Path | None = None) -> dict[str, HostSpec]:
    path = path or (INVENTORY_DIR / "hosts.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    hosts = {}
    for name, entry in data["hosts"].items():
        hosts[name] = HostSpec(
            name=name,
            cpus=entry["cpus"],
            memory_mb=entry["memory_mb"],
            gpus=entry.get("gpus", 0),
            address=entry.get("address", name),
            gpu_pci_addresses=entry.get("gpu_pci_addresses", []),
        )
    return hosts


def mission_spec_from_dict(data: dict) -> MissionSpec:
    """
    Build a core.types.MissionSpec from a plain dict (already parsed from
    YAML/JSON, or from api/models.py's Pydantic MissionDefinition.dict()).
    Kept separate from Pydantic so it's testable without pydantic
    installed.
    """
    vms = {}
    for vm_name, vm_data in data["vms"].items():
        vms[vm_name] = VMSpec(
            name=vm_name,
            type=VMType(vm_data["type"]),
            cpu=vm_data["cpu"],
            memory_mb=vm_data["memory"],
            interfaces=list(vm_data["interfaces"]),
            image=vm_data.get("image"),
            gpu=vm_data.get("gpu", False),
            macs=data.get("macs", {}).get(vm_name),
        )
    return MissionSpec(
        name=data["mission"],
        networks=dict(data["networks"]),
        vms=vms,
        placement=dict(data["placement"]),
    )


def load_mission_yaml(path: Path) -> MissionSpec:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return mission_spec_from_dict(data)


def register_mission(mission: MissionSpec, hosts: dict[str, HostSpec] | None = None) -> MissionStatus:
    """
    Validates placement capacity, then registers the mission in the store
    with state=Pending. Raises MissionValidationError (never touches the
    store) if placement is infeasible -- the API layer turns that into a
    4xx response instead of accepting work that can never succeed.
    """
    hosts = hosts if hosts is not None else load_host_inventory()
    report = validate_placement(mission, hosts)
    if not report.ok:
        raise MissionValidationError(
            f"placement is infeasible for mission '{mission.name}': {report.as_dict()['violations']}"
        )
    return store.create(mission.name, spec=mission)


def get_mission(mission_id: str) -> MissionStatus | None:
    return store.get(mission_id)


def list_missions() -> list[MissionStatus]:
    return store.list()
