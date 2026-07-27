"""
JSON serialization of MissionSpec/MissionStatus, for core/state.py's
SQLite-backed persistence. A direct, field-for-field mirror of the
dataclasses in core/types.py -- NOT to be confused with
services/missions.py's mission_spec_from_dict, which parses the
user-facing mission definition YAML/JSON schema (interfaces as
{mac_suffix}, gpu as {profile}, memory not memory_mb, etc.) -- a related
but distinct format from this module's internal, direct dataclass mirror.
Kept dependency-free (stdlib only), like the rest of core/.
"""
from __future__ import annotations

from .types import (
    GpuDeviceSpec,
    HostSpec,
    InterfaceSpec,
    MissionSpec,
    MissionState,
    MissionStatus,
    StepLogEntry,
    VMSpec,
    VMType,
)


def mission_spec_to_jsonable(spec: MissionSpec) -> dict:
    """Convert a MissionSpec into a plain, JSON-serializable dict (json.dumps-ready)."""
    return {
        "name": spec.name,
        "networks": dict(spec.networks),
        "vms": {
            vm_name: {
                "type": vm.type.value,
                "cpu": vm.cpu,
                "memory_mb": vm.memory_mb,
                "interfaces": {
                    net_name: {"network": iface.network, "mac_suffix": iface.mac_suffix}
                    for net_name, iface in vm.interfaces.items()
                },
                "image": vm.image,
                "image_revision": vm.image_revision,
                "gpu_profile": vm.gpu_profile,
            }
            for vm_name, vm in spec.vms.items()
        },
        "placement": dict(spec.placement),
        "file_version": spec.file_version,
    }


def mission_spec_from_jsonable(data: dict) -> MissionSpec:
    """Reconstruct a MissionSpec from a dict produced by mission_spec_to_jsonable (json.loads output)."""
    vms = {}
    for vm_name, vm_data in data["vms"].items():
        interfaces = {
            net_name: InterfaceSpec(network=iface_data["network"], mac_suffix=iface_data.get("mac_suffix"))
            for net_name, iface_data in vm_data["interfaces"].items()
        }
        vms[vm_name] = VMSpec(
            name=vm_name,
            type=VMType(vm_data["type"]),
            cpu=vm_data["cpu"],
            memory_mb=vm_data["memory_mb"],
            interfaces=interfaces,
            image=vm_data.get("image"),
            image_revision=vm_data.get("image_revision"),
            gpu_profile=vm_data.get("gpu_profile"),
        )
    return MissionSpec(
        name=data["name"],
        networks=dict(data["networks"]),
        vms=vms,
        placement=dict(data["placement"]),
        file_version=data.get("file_version"),
    )


def mission_status_to_jsonable(status: MissionStatus) -> dict:
    """Convert a MissionStatus (including its retained MissionSpec, if any) into a plain, JSON-serializable dict."""
    return {
        "mission_id": status.mission_id,
        "name": status.name,
        "state": status.state.value,
        "steps": [{"step": s.step, "status": s.status, "detail": s.detail} for s in status.steps],
        "error": status.error,
        "spec": mission_spec_to_jsonable(status.spec) if status.spec is not None else None,
        "mac_prefix": status.mac_prefix,
        "resolved_macs": dict(status.resolved_macs),
        "gpu_allocations": dict(status.gpu_allocations),
    }


def mission_status_from_jsonable(data: dict) -> MissionStatus:
    """Reconstruct a MissionStatus from a dict produced by mission_status_to_jsonable (json.loads output)."""
    return MissionStatus(
        mission_id=data["mission_id"],
        name=data["name"],
        state=MissionState(data["state"]),
        steps=[StepLogEntry(step=s["step"], status=s["status"], detail=s.get("detail", "")) for s in data["steps"]],
        error=data.get("error"),
        spec=mission_spec_from_jsonable(data["spec"]) if data.get("spec") is not None else None,
        mac_prefix=data.get("mac_prefix"),
        resolved_macs=data.get("resolved_macs", {}),
        gpu_allocations=data.get("gpu_allocations", {}),
    )
