"""
Framework-independent domain model for a "mission" -- the design doc's
name for a declarative set of VMs, networks, and placement that gets
provisioned in one automated run.

Why this file exists separately from management/api/models.py: this
build environment has no network egress, so fastapi/pydantic (what the
design doc specifies for the API layer) cannot be installed or exercised
here. Rather than leave the hardest, most bespoke logic in this project
(MAC resolution, capacity validation, libvirt XML generation, mission
state transitions) untestable, we factor it into plain dataclasses + pure
functions here in core/, with zero dependency on fastapi or pydantic.
management/api/models.py defines the Pydantic request/response schemas
the doc calls for and converts to/from these dataclasses at the API
boundary -- so on a real deployment (where requirements.txt IS installed)
the system behaves exactly as the design doc specifies, while in this
sandbox we can still unit-test every non-trivial algorithm with nothing
but the standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VMType(str, Enum):
    """The two supported VM provisioning modes."""
    LINKED_CLONE = "linked_clone"   # disk cloned from a golden image (see StorageContext/golden catalog)
    PXE = "pxe"                      # diskless, boots over the network


@dataclass
class InterfaceSpec:
    """
    One VM network interface.

    Attributes:
        network: Name of the mission network this interface attaches to
            (must be a key in the owning MissionSpec's `networks` dict).
        mac_suffix: The interface's MAC address, expressed as only its
            last three octets (e.g. "10:00:00"), per the operator
            requirement that guest software has MAC addresses hardcoded
            into its licensing -- the operator supplies this value and it
            is stable across every deployment of this mission, so
            substring matching on it inside a mission's isolated L2
            domain always finds the right VM. The first three octets (an
            OUI-like prefix) are randomized once per *deployment* of the
            mission -- not stored here -- so two concurrent deployments
            of the same mission definition never collide on the wire even
            though every `mac_suffix` in the file is identical between
            them. If left as None, a random suffix is generated for this
            interface at deploy time instead (see core/macs.py) --
            appropriate only for interfaces with no external
            substring-matching requirement, since an unspecified suffix
            is not guaranteed stable across redeploys.
    """
    network: str
    mac_suffix: Optional[str] = None

    def __post_init__(self):
        if self.mac_suffix is not None:
            _validate_octet_triplet(self.mac_suffix, label=f"interface on network '{self.network}'")


def _validate_octet_triplet(value: str, label: str) -> None:
    """Raise ValueError unless `value` is exactly 3 colon-separated hex octets, e.g. '10:00:00'."""
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"{label}: mac_suffix must be exactly 3 colon-separated octets (e.g. '10:00:00'), got {value!r}")
    for p in parts:
        if len(p) != 2:
            raise ValueError(f"{label}: mac_suffix octet {p!r} must be exactly 2 hex digits")
        try:
            int(p, 16)
        except ValueError as exc:
            raise ValueError(f"{label}: mac_suffix octet {p!r} is not valid hex") from exc


@dataclass
class VMSpec:
    """
    Declarative spec for one VM within a mission.

    Attributes:
        name: VM name, unique within the mission.
        type: linked_clone (disk-backed, cloned from a golden image) or
            pxe (diskless, network boot).
        cpu: vCPU count (must be positive).
        memory_mb: Memory in MiB (must be positive).
        interfaces: Ordered mapping of network name -> InterfaceSpec.
            Iteration order determines NIC order in the rendered libvirt
            XML.
        image: Golden image catalog name (required for linked_clone,
            forbidden for pxe) -- looked up in config/storage.yaml's
            golden.images catalog.
        image_revision: Which revision/tag of `image` to clone from (e.g.
            "Alpha", "Beta"). If omitted, the catalog's
            `default_revision` for that image is used. Forbidden for pxe
            (which has no image at all).
        gpu_profile: Name of a MIG (NVIDIA H100) or vGPU (NVIDIA L4)
            profile this VM requires (e.g. "H100-MIG-3g.40gb",
            "L4-vGPU-4Q"), matched at deploy time against a specific
            available slice recorded in config/hosts.yaml's
            `gpu_devices`. None means no GPU.
    """
    name: str
    type: VMType
    cpu: int
    memory_mb: int
    interfaces: dict[str, InterfaceSpec]
    image: Optional[str] = None
    image_revision: Optional[str] = None
    gpu_profile: Optional[str] = None

    def __post_init__(self):
        if self.type == VMType.LINKED_CLONE and not self.image:
            raise ValueError(f"VM '{self.name}': linked_clone VMs require an 'image'")
        if self.type == VMType.PXE:
            if self.image:
                raise ValueError(f"VM '{self.name}': pxe VMs must not specify an 'image' (diskless)")
            if self.image_revision:
                raise ValueError(f"VM '{self.name}': pxe VMs must not specify an 'image_revision' (diskless)")
        if self.cpu <= 0:
            raise ValueError(f"VM '{self.name}': cpu must be positive")
        if self.memory_mb <= 0:
            raise ValueError(f"VM '{self.name}': memory_mb must be positive")
        if not self.interfaces:
            raise ValueError(f"VM '{self.name}': must have at least one interface")

        suffixes_seen: dict[str, str] = {}
        for net_name, iface in self.interfaces.items():
            if iface.mac_suffix is not None:
                if iface.mac_suffix in suffixes_seen:
                    raise ValueError(
                        f"VM '{self.name}': mac_suffix '{iface.mac_suffix}' is used by both "
                        f"'{suffixes_seen[iface.mac_suffix]}' and '{net_name}' -- each interface "
                        f"needs a distinct suffix, since the deployment-wide prefix is identical "
                        f"for every interface and identical suffixes would collide"
                    )
                suffixes_seen[iface.mac_suffix] = net_name

    @property
    def has_gpu(self) -> bool:
        """True if this VM requires GPU passthrough (any profile)."""
        return self.gpu_profile is not None

    def interface_names(self) -> list[str]:
        """Ordered list of this VM's network names (order matches rendered NIC order)."""
        return list(self.interfaces.keys())


@dataclass
class MissionSpec:
    """
    A complete, declarative mission: a named set of networks, VMs, and
    their placement onto hosts.

    Attributes:
        name: Mission name, used to scope OVN switch/router/port names
            (see core/xml_render.py) and as the human-readable identifier
            in status output.
        networks: Mapping of network name -> VLAN id. Every VM interface
            must reference a network defined here.
        vms: Mapping of VM name -> VMSpec.
        placement: Mapping of VM name -> host name. Must cover every VM
            in `vms`, exactly.
        file_version: The mission definition file's own revision tag (see
            management/mission_defs/README.md), recorded for audit/status
            purposes. Not required to be set.
    """
    name: str
    networks: dict[str, int]
    vms: dict[str, VMSpec]
    placement: dict[str, str]
    file_version: Optional[str] = None

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
            unknown = [n for n in vm.interface_names() if n not in known_networks]
            if unknown:
                raise ValueError(
                    f"VM '{vm_name}' references undefined network(s): {unknown}"
                )

    def gpu_vm_names(self) -> list[str]:
        """Names of every VM in this mission that requires a GPU (any profile)."""
        return [name for name, vm in self.vms.items() if vm.has_gpu]

    def total_nics(self) -> int:
        """Total interface count across every VM in this mission."""
        return sum(len(vm.interfaces) for vm in self.vms.values())


@dataclass
class GpuDeviceSpec:
    """One specific, already-partitioned GPU slice available on a host.

    Attributes:
        profile: MIG or vGPU profile name (e.g. "H100-MIG-3g.40gb",
            "L4-vGPU-4Q") -- matched against a VM's requested
            `gpu_profile`.
        mdev_uuid: The mediated-device UUID libvirt attaches via
            `<hostdev type='mdev'>` -- must already exist on the host
            (created via nvidia-smi mig / the vGPU manager; see
            bootstrap/scripts/enumerate_mdev_gpus.sh).
    """
    profile: str
    mdev_uuid: str


@dataclass
class HostSpec:
    """
    Capacity and connection info for one compute host, as loaded from
    config/hosts.yaml.

    Attributes:
        name: Host's short name (inventory key).
        cpus: Total vCPU capacity available for VM placement.
        memory_mb: Total memory capacity in MiB.
        address: Real network address/hostname used for the libvirt
            connection (falls back to `name` if not set separately).
        gpu_devices: Every GPU slice (MIG or vGPU) available on this host.
    """
    name: str
    cpus: int
    memory_mb: int
    address: str = ""
    gpu_devices: list[GpuDeviceSpec] = field(default_factory=list)

    def available_profiles(self, exclude_uuids: "set[str] | None" = None) -> dict[str, int]:
        """
        Count of GPU slices per profile name on this host.

        Args:
            exclude_uuids: mdev UUIDs to treat as unavailable (already
                reserved by a currently-active mission deployment -- see
                core/state.py:MissionStore.active_gpu_allocations()).
                Omit (the default) to count total declared capacity
                regardless of current reservations.

        Returns:
            {profile_name: count}, counting only devices whose mdev_uuid
            is not in `exclude_uuids`.
        """
        exclude_uuids = exclude_uuids or set()
        counts: dict[str, int] = {}
        for dev in self.gpu_devices:
            if dev.mdev_uuid in exclude_uuids:
                continue
            counts[dev.profile] = counts.get(dev.profile, 0) + 1
        return counts


class MissionState(str, Enum):
    """Lifecycle states a mission deployment moves through."""
    PENDING = "Pending"
    DEPLOYING_NETWORKS = "DeployingNetworks"
    CLONING_STORAGE = "CloningStorage"
    DEFINING_VMS = "DefiningVMs"
    VALIDATING = "Validating"
    RUNNING = "Running"
    ERROR = "Error"
    ROLLING_BACK = "RollingBack"
    ROLLED_BACK = "RolledBack"
    DESTROYING = "Destroying"
    DESTROYED = "Destroyed"


# States that no longer hold any cluster resources (MAC prefix, GPU
# device allocations) -- excluded from core/state.py's
# active_mac_prefixes()/active_gpu_allocations(), so a mission that ended
# up here (whether via a clean operator-requested teardown, or an
# automatic rollback after a failed deploy) frees its reservations for a
# future deployment to reuse.
TERMINAL_FREEING_STATES = {MissionState.DESTROYED, MissionState.ROLLED_BACK}


@dataclass
class StepLogEntry:
    """One entry in a mission's step-by-step audit log."""
    step: str
    status: str      # "ok" | "error"
    detail: str = ""


@dataclass
class MissionStatus:
    """
    Live status of one mission *deployment* (a specific provisioning run
    of a MissionSpec -- the same MissionSpec deployed twice produces two
    separate MissionStatus records with two different mac_prefix values).

    Attributes:
        mission_id: Unique id for this deployment (not the mission name --
            multiple deployments of the same mission name/spec are
            expected and each gets its own id).
        name: The mission's name (from its MissionSpec).
        state: Current lifecycle state.
        steps: Ordered audit log of every provisioning step.
        error: Set when state == ERROR; the exception message that caused it.
        spec: The originating MissionSpec, retained so a later teardown
            request knows what to tear down.
        mac_prefix: The 3-octet MAC prefix randomly assigned to this
            specific deployment (see core/macs.py). Every VM interface's
            full MAC is this prefix + that interface's configured or
            generated suffix. Kept here (not per-VM) because it's one
            value shared by the whole deployment.
        resolved_macs: Every VM's fully-resolved MAC list (mac_prefix +
            each interface's suffix), computed once at registration time
            (core/macs.py:resolve_mission_macs) and reused by the deploy
            worker rather than recomputed, so the OVN-pinned address and
            the libvirt <mac> can never drift apart.
        gpu_allocations: {vm_name: mdev_uuid} for every GPU VM in this
            deployment, reserved once at registration time (see
            core/placement.py:reserve_gpu_devices) atomically against
            every other currently-active deployment's reservations, and
            reused by the deploy worker rather than recomputed -- so two
            competing deployments can never both be handed the same
            physical GPU slice.
    """
    mission_id: str
    name: str
    state: MissionState = MissionState.PENDING
    steps: list[StepLogEntry] = field(default_factory=list)
    error: Optional[str] = None
    spec: Optional[MissionSpec] = None
    mac_prefix: Optional[str] = None
    resolved_macs: dict[str, list[str]] = field(default_factory=dict)
    gpu_allocations: dict[str, str] = field(default_factory=dict)

    def log(self, step: str, status: str, detail: str = "") -> None:
        """Append one entry to this mission's step audit log."""
        self.steps.append(StepLogEntry(step, status, detail))

    def fail(self, step: str, error: Exception) -> None:
        """Transition this mission to Error state and record what failed."""
        self.state = MissionState.ERROR
        self.error = str(error)
        self.log(step, "error", str(error))
