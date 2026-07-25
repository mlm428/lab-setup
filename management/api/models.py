"""
Pydantic request/response models for the FastAPI service, per the design
doc's:

    class VMConfig(BaseModel):
        type: Literal['linked_clone','pxe']
        image: Optional[str]
        cpu: int
        memory: int
        gpu: bool = False
        interfaces: List[str]
        macs: Optional[List[str]] = None

    class MissionDefinition(BaseModel):
        mission: str
        networks: Dict[str, int]
        vms: Dict[str, VMConfig]
        placement: Dict[str, str]
        macs: Optional[Dict[str, List[str]]] = None

These mirror core/types.py's VMSpec/MissionSpec field-for-field and
convert to them at the API boundary (to_core()), so all the actual
validation logic (interfaces reference known networks, placement covers
every VM, MAC list length matches interface count, etc.) lives once, in
core/types.py, and is exercised by management/tests/test_types.py without
needing pydantic installed. This file itself requires pydantic + fastapi,
neither of which could be installed in the build sandbox (no network
egress) -- see README.md's "What was and wasn't executed" section.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from core.types import MissionSpec, VMSpec, VMType


class VMConfig(BaseModel):
    type: Literal["linked_clone", "pxe"]
    image: Optional[str] = None
    cpu: int = Field(gt=0)
    memory: int = Field(gt=0, description="Memory in MiB")
    gpu: bool = False
    interfaces: List[str]
    macs: Optional[List[str]] = None

    def to_core(self, name: str) -> VMSpec:
        return VMSpec(
            name=name,
            type=VMType(self.type),
            cpu=self.cpu,
            memory_mb=self.memory,
            interfaces=list(self.interfaces),
            image=self.image,
            gpu=self.gpu,
            macs=self.macs,
        )


class MissionDefinition(BaseModel):
    mission: str
    networks: Dict[str, int]
    vms: Dict[str, VMConfig]
    placement: Dict[str, str]
    macs: Optional[Dict[str, List[str]]] = None

    def to_core(self) -> MissionSpec:
        vms = {}
        for vm_name, vm_cfg in self.vms.items():
            core_vm = vm_cfg.to_core(vm_name)
            if core_vm.macs is None and self.macs:
                core_vm.macs = self.macs.get(vm_name)
            vms[vm_name] = core_vm
        return MissionSpec(
            name=self.mission,
            networks=dict(self.networks),
            vms=vms,
            placement=dict(self.placement),
        )


class MissionCreateResponse(BaseModel):
    mission_id: str


class StepLogEntryModel(BaseModel):
    step: str
    status: str
    detail: str = ""


class MissionStatusResponse(BaseModel):
    mission_id: str
    name: str
    state: str
    steps: List[StepLogEntryModel]
    error: Optional[str] = None


class HostSummary(BaseModel):
    name: str
    cpus: int
    memory_mb: int
    gpus: int


class HealthResponse(BaseModel):
    status: Literal["ok"]
    hosts: int
