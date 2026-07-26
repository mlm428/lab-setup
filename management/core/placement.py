"""
Placement validation: given a mission's static placement (VM -> host) and
the cluster's host inventory, confirm no host is oversubscribed on CPU,
memory, or any specific GPU profile (MIG/vGPU slice type), per the design
doc's "Inventory Validation" step -- extended here to be GPU-profile-aware
rather than a single generic GPU count, since a VM needs a *specific*
MIG/vGPU profile (e.g. "H100-MIG-3g.40gb"), not just "a GPU".

Placement itself is static (operator- or generator-supplied) for this
prototype -- the design doc explicitly scopes auto-scheduling as future
work. `greedy_place` below is a simple reference bin-packer used only by
tools/generate_mission.py to produce a starting placement file; the
runtime path always validates whatever placement was supplied, regardless
of how it was derived.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import HostSpec, MissionSpec


@dataclass
class PlacementViolation:
    """One capacity violation found by validate_placement.

    Attributes:
        host: Host name the violation applies to.
        resource: "cpu" | "memory_mb" | "gpu_profile:<name>" | "unknown_host".
        requested: Total amount requested on this host for `resource`.
        available: Total amount actually available on this host for `resource`.
    """
    host: str
    resource: str
    requested: int
    available: int


@dataclass
class PlacementReport:
    """Aggregate result of validate_placement: zero or more violations."""
    violations: list[PlacementViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if no capacity violations were found."""
        return not self.violations

    def as_dict(self) -> dict:
        """JSON-friendly representation, used in API responses and logs."""
        return {
            "ok": self.ok,
            "violations": [
                {
                    "host": v.host,
                    "resource": v.resource,
                    "requested": v.requested,
                    "available": v.available,
                }
                for v in self.violations
            ],
        }


def validate_placement(mission: MissionSpec, hosts: dict[str, HostSpec]) -> PlacementReport:
    """
    Check a mission's static placement against host capacity: CPU, memory,
    and per-GPU-profile slice counts.

    Args:
        mission: The mission whose `placement` mapping is being checked.
        hosts: Full host inventory (name -> HostSpec) to validate against.

    Returns:
        A PlacementReport listing every violation found (empty if the
        placement fits within capacity on every host).
    """
    report = PlacementReport()

    unknown_hosts = set(mission.placement.values()) - set(hosts.keys())
    if unknown_hosts:
        for host in sorted(unknown_hosts):
            report.violations.append(PlacementViolation(host, "unknown_host", 0, 0))
        return report  # can't compute per-host totals against hosts we don't know

    per_host_cpu: dict[str, int] = {h: 0 for h in hosts}
    per_host_mem: dict[str, int] = {h: 0 for h in hosts}
    per_host_gpu_profile: dict[str, dict[str, int]] = {h: {} for h in hosts}

    for vm_name, vm in mission.vms.items():
        host = mission.placement[vm_name]
        per_host_cpu[host] += vm.cpu
        per_host_mem[host] += vm.memory_mb
        if vm.has_gpu:
            profile_counts = per_host_gpu_profile[host]
            profile_counts[vm.gpu_profile] = profile_counts.get(vm.gpu_profile, 0) + 1

    for host_name, host in hosts.items():
        if per_host_cpu[host_name] > host.cpus:
            report.violations.append(
                PlacementViolation(host_name, "cpu", per_host_cpu[host_name], host.cpus)
            )
        if per_host_mem[host_name] > host.memory_mb:
            report.violations.append(
                PlacementViolation(host_name, "memory_mb", per_host_mem[host_name], host.memory_mb)
            )

        available_profiles = host.available_profiles()
        for profile, requested_count in per_host_gpu_profile[host_name].items():
            available_count = available_profiles.get(profile, 0)
            if requested_count > available_count:
                report.violations.append(
                    PlacementViolation(host_name, f"gpu_profile:{profile}", requested_count, available_count)
                )

    return report


def reserve_gpu_devices(
    mission: MissionSpec,
    hosts: dict[str, HostSpec],
    already_reserved_uuids: set[str],
) -> dict[str, str]:
    """
    Atomically reserve one specific, free GPU mdev UUID for every GPU VM
    in a mission, checked against every mdev UUID already reserved by
    OTHER currently-active mission deployments -- not just this mission's
    own VMs. Without this cross-deployment check, validate_placement's
    per-mission capacity check alone would let two different mission
    deployments (or two deployments of the same mission) each be told
    "yes, that GPU profile fits" independently, even though the cluster
    only has enough physical slices for one of them -- the collision
    would then only surface later, as an opaque libvirt/mdev "device
    busy" error when the second one actually tries to start.

    Args:
        mission: The mission being registered.
        hosts: Full host inventory (for each host's gpu_devices list).
        already_reserved_uuids: mdev UUIDs already claimed by other
            currently-active deployments -- see
            core/state.py:MissionStore.active_gpu_allocations().

    Returns:
        {vm_name: mdev_uuid} for every GPU VM in the mission (VMs with no
        gpu_profile are simply absent from the result).

    Raises:
        RuntimeError: if no free slice with the right profile remains on
            a GPU VM's placed host, after accounting for both
            already_reserved_uuids and every other GPU VM already
            reserved earlier in this same call (processed in sorted VM-name
            order, so which VM "wins" a contested slice is deterministic).
    """
    reserved_this_call: set[str] = set()
    result: dict[str, str] = {}

    for vm_name in sorted(mission.gpu_vm_names()):
        vm = mission.vms[vm_name]
        host_name = mission.placement[vm_name]
        host = hosts[host_name]
        candidate = next(
            (
                d for d in host.gpu_devices
                if d.profile == vm.gpu_profile
                and d.mdev_uuid not in already_reserved_uuids
                and d.mdev_uuid not in reserved_this_call
            ),
            None,
        )
        if candidate is None:
            raise RuntimeError(
                f"no free GPU device matching profile '{vm.gpu_profile}' on host "
                f"'{host_name}' for VM '{vm_name}' -- every slice with that profile "
                f"is already reserved by this or another currently-active mission deployment"
            )
        reserved_this_call.add(candidate.mdev_uuid)
        result[vm_name] = candidate.mdev_uuid

    return result


def greedy_place(
    vm_names_in_order: list[str],
    vm_requirements: dict[str, dict],
    hosts: dict[str, HostSpec],
) -> dict[str, str]:
    """
    Simple reference bin-packer: place VMs onto the least-loaded feasible
    host, GPU VMs restricted to hosts offering their exact requested GPU
    profile with a free slice. Used only to generate a first-draft
    placement file (tools/generate_mission.py) -- NOT invoked at
    deployment time. A smarter scheduler is explicitly future work per the
    design doc.

    Args:
        vm_names_in_order: VM names in the order to place them (placing
            GPU VMs first, while capacity is fully free, generally works
            best -- see tools/generate_mission.py).
        vm_requirements: vm_name -> {"cpu": int, "memory_mb": int,
            "gpu_profile": Optional[str]}.
        hosts: Full host inventory to place onto.

    Returns:
        vm_name -> host_name for every VM in vm_names_in_order.

    Raises:
        RuntimeError: if any VM has no feasible host given remaining
            capacity (message includes the VM name and its requirements).
    """
    remaining_cpu = {h: host.cpus for h, host in hosts.items()}
    remaining_mem = {h: host.memory_mb for h, host in hosts.items()}
    remaining_gpu_profiles = {h: dict(host.available_profiles()) for h, host in hosts.items()}

    placement: dict[str, str] = {}
    for vm_name in vm_names_in_order:
        req = vm_requirements[vm_name]
        gpu_profile = req.get("gpu_profile")

        candidates = [
            h for h in hosts
            if remaining_cpu[h] >= req["cpu"]
            and remaining_mem[h] >= req["memory_mb"]
            and (gpu_profile is None or remaining_gpu_profiles[h].get(gpu_profile, 0) > 0)
        ]
        if not candidates:
            raise RuntimeError(f"no feasible host for VM '{vm_name}' (requirements: {req})")
        # Prefer the host with the most spare CPU headroom (simple greedy
        # load-spreading heuristic).
        chosen = max(candidates, key=lambda h: remaining_cpu[h])
        placement[vm_name] = chosen
        remaining_cpu[chosen] -= req["cpu"]
        remaining_mem[chosen] -= req["memory_mb"]
        if gpu_profile is not None:
            remaining_gpu_profiles[chosen][gpu_profile] -= 1

    return placement
