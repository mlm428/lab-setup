"""
Framework-independent domain model for a "mission" -- the design doc's
name for a declarative set of VMs, networks, and placement that gets
provisioned in one automated run.

Why this file exists separately from management/api/models.py:
This build environment has no network egress, so fastapi/pydantic (what
the design doc specifies for the API layer) cannot be installed or
exercised here. Rather than leave the hardest, most bespoke logic in this
project (MAC assignment, capacity validation, libvirt XML generation,
mission state transitions) untestable, we factor it into plain dataclasses
+ pure functions here in core/, with zero dependency on fastapi or
pydantic. management/api/models.py defines the Pydantic request/response
schemas the doc calls for and converts to/from these dataclasses at the
API boundary -- so on a real deployment (where requirements.txt IS
installed) the system behaves exactly as the design doc specifies, while
in this sandbox we can still unit-test every non-trivial algorithm with
nothing but the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VMType(str, Enum):
    LINKED_CLONE = "linked_clone"
    PXE = "pxe"


@dataclass
class VMSpec:
    name: str
    type: VMType
    cpu: int
    memory_mb: int
    interfaces: list[str]                  # ordered list of network names
    image: Optional[str] = None             # golden image name, required for linked_clone
    gpu: bool = False
    macs: Optional[list[str]] = None        # one per interface, same order; auto-generated if omitted

    def __post_init__(self):
        if self.type == VMType.LINKED_CLONE and not self.image:
            raise ValueError(f"VM '{self.name}': linked_clone VMs require an 'image'")
        if self.type == VMType.PXE and self.image:
            raise ValueError(f"VM '{self.name}': pxe VMs must not specify an 'image' (diskless)")
        if self.cpu <= 0:
            raise ValueError(f"VM '{self.name}': cpu must be positive")
        if self.memory_mb <= 0:
            raise ValueError(f"VM '{self.name}': memory_mb must be positive")
        if not self.interfaces:
            raise ValueError(f"VM '{self.name}': must have at least one interface")
        if self.macs is not None and len(self.macs) != len(self.interfaces):
            raise ValueError(
                f"VM '{self.name}': macs length ({len(self.macs)}) must match "
                f"interfaces length ({len(self.interfaces)})"
            )


@dataclass
class MissionSpec:
    name: str
    networks: dict[str, int]                # network name -> VLAN id
    vms: dict[str, VMSpec]                   # vm name -> spec
    placement: dict[str, str]                # vm name -> host name

    def __post_init__(self):
        vm_names = set(self.vms.keys())
        placed_names = set(self.placement.keys())
        if vm_names != placed_names:
            missing = vm_names - placed_names
            extra = placed_names - vm_names
            problems = []
            if missing:
                problems.append(f"no placement for: {sorted(missing)}")
            if extra:
                problems.append(f"placement for unknown VMs: {sorted(extra)}")
            raise ValueError(f"mission '{self.name}': placement mismatch -- {'; '.join(problems)}")

        known_networks = set(self.networks.keys())
        for vm_name, vm in self.vms.items():
            unknown = [n for n in vm.interfaces if n not in known_networks]
            if unknown:
                raise ValueError(
                    f"VM '{vm_name}' references undefined network(s): {unknown}"
                )

    def gpu_vm_names(self) -> list[str]:
        return [name for name, vm in self.vms.items() if vm.gpu]

    def total_nics(self) -> int:
        return sum(len(vm.interfaces) for vm in self.vms.values())


@dataclass
class HostSpec:
    name: str
    cpus: int
    memory_mb: int
    gpus: int
    address: str = ""
    gpu_pci_addresses: list[str] = field(default_factory=list)


class MissionState(str, Enum):
    PENDING = "Pending"
    DEPLOYING_NETWORKS = "DeployingNetworks"
    CLONING_STORAGE = "CloningStorage"
    DEFINING_VMS = "DefiningVMs"
    VALIDATING = "Validating"
    RUNNING = "Running"
    ERROR = "Error"
    DESTROYING = "Destroying"
    DESTROYED = "Destroyed"


@dataclass
class StepLogEntry:
    step: str
    status: str      # "ok" | "error"
    detail: str = ""


@dataclass
class MissionStatus:
    mission_id: str
    name: str
    state: MissionState = MissionState.PENDING
    steps: list[StepLogEntry] = field(default_factory=list)
    error: Optional[str] = None
    spec: Optional[MissionSpec] = None

    def log(self, step: str, status: str, detail: str = "") -> None:
        self.steps.append(StepLogEntry(step, status, detail))

    def fail(self, step: str, error: Exception) -> None:
        self.state = MissionState.ERROR
        self.error = str(error)
        self.log(step, "error", str(error))
