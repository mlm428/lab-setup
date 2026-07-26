"""
Pydantic request/response schemas for the FastAPI service. These mirror
core/types.py's dataclasses field-for-field and convert to them at the API
boundary (to_core()), so all the actual validation logic (interfaces
reference known networks, placement covers every VM, mac_suffix
uniqueness, etc.) lives once, in core/types.py, and is exercised by
management/tests/ without needing pydantic installed. This file itself
requires pydantic + fastapi, neither of which could be installed in the
build sandbox (no network egress) -- see README.md's "What was and wasn't
executed" section.

API VERSIONING: this module's schemas are versioned independently of the
HTTP route paths -- see api/routes.py's module docstring for the
API_VERSION constant and how it's surfaced.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from core.types import InterfaceSpec, MissionSpec, VMSpec, VMType


class InterfaceConfig(BaseModel):
    """One VM interface. `mac_suffix` is the operator-supplied last three
    octets of this interface's MAC (e.g. "10:00:00") -- see
    management/mission_defs/README.md. Omit it (or set to null) to have a
    random suffix generated for this interface at deploy time."""
    mac_suffix: Optional[str] = None


class GpuConfig(BaseModel):
    """GPU requirement for a VM: a specific MIG (H100) or vGPU (L4) profile name."""
    profile: str


class VMConfig(BaseModel):
    type: Literal["linked_clone", "pxe"]
    image: Optional[str] = None
    image_revision: Optional[str] = None
    cpu: int = Field(gt=0)
    memory: int = Field(gt=0, description="Memory in MiB")
    gpu: Optional[Union[GpuConfig, str]] = None
    interfaces: Dict[str, Optional[InterfaceConfig]]

    def to_core(self, name: str) -> VMSpec:
        """Convert this API model to the framework-independent VMSpec core/types.py uses everywhere else."""
        gpu_profile = None
        if isinstance(self.gpu, GpuConfig):
            gpu_profile = self.gpu.profile
        elif isinstance(self.gpu, str):
            gpu_profile = self.gpu

        interfaces = {}
        for net_name, cfg in self.interfaces.items():
            mac_suffix = cfg.mac_suffix if cfg else None
            interfaces[net_name] = InterfaceSpec(network=net_name, mac_suffix=mac_suffix)

        return VMSpec(
            name=name,
            type=VMType(self.type),
            cpu=self.cpu,
            memory_mb=self.memory,
            interfaces=interfaces,
            image=self.image,
            image_revision=self.image_revision,
            gpu_profile=gpu_profile,
        )


class MissionDefinition(BaseModel):
    mission: str
    file_version: Optional[str] = None
    networks: Dict[str, int]
    vms: Dict[str, VMConfig]
    placement: Dict[str, str]

    def to_core(self) -> MissionSpec:
        """Convert this API model to the framework-independent MissionSpec core/types.py uses everywhere else."""
        vms = {vm_name: vm_cfg.to_core(vm_name) for vm_name, vm_cfg in self.vms.items()}
        return MissionSpec(
            name=self.mission,
            networks=dict(self.networks),
            vms=vms,
            placement=dict(self.placement),
            file_version=self.file_version,
        )


class MissionCreateResponse(BaseModel):
    mission_id: str
    mac_prefix: str


class StepLogEntryModel(BaseModel):
    step: str
    status: str
    detail: str = ""


class MissionStatusResponse(BaseModel):
    mission_id: str
    name: str
    state: str
    mac_prefix: Optional[str] = None
    steps: List[StepLogEntryModel]
    error: Optional[str] = None


class HostSummary(BaseModel):
    name: str
    cpus: int
    memory_mb: int
    gpu_profiles: Dict[str, int]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    hosts: int


class HostHealth(BaseModel):
    """Live health snapshot for one compute host, part of ClusterHealthResponse."""
    host: str
    reachable: bool
    libvirt_ok: Optional[bool] = None
    vm_count: Optional[int] = None
    detail: str = ""


class ClusterHealthResponse(BaseModel):
    """
    Response for GET /cluster/health -- a full infrastructure scan, per
    operator request: "a scan/search through the cluster to validate the
    infrastructure ... simple rollup for a good pass/fail ... but then
    also provide more detailed status."
    """
    ok: bool
    ovn_reachable: bool
    ceph_reachable: Optional[bool] = None
    hosts: List[HostHealth]
    missions_total: int
    missions_by_state: Dict[str, int]
    detail: str = ""
