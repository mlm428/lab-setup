"""
Static MAC address handling for mission VMs.

OPERATIONAL REQUIREMENT (confirmed by the operator): guest software
running inside mission VMs has MAC addresses hardcoded into its
licensing/configuration, so the MAC for every interface on every VM is a
value the operator determines ahead of time and writes into the mission
definition's `macs:` section -- it is NOT something this system is free to
invent. Every VM in management/mission_defs/mission_alpha.yaml carries an
explicit `macs:` list for exactly this reason; assign_all_macs() below
always uses those provided values when present.

The deterministic generator in this file (generate_mac /
compute_vm_indices) is kept only as a fallback for the case where a
mission genuinely doesn't care about specific addresses (e.g. ad hoc dev/
test VMs with no licensing constraint) and so leaves a VM's `macs:` unset --
it must never be relied on for any VM whose guest software is tied to a
specific MAC. Even in that fallback role it still needs to be
collision-free and reproducible (a destroy+redeploy of the same mission
must not silently reassign different generated addresses), which is what
the rest of this docstring documents.

All addresses use QEMU/KVM's locally-administered OUI 52:54:00, the same
prefix used throughout both source documents' examples: 52:54:00:<mission
octet>:<vm index>:<interface index>.

IMPORTANT: vm identity is a plain 0-based index into the mission's VM
names sorted alphabetically -- NOT a hash. An earlier version of this
module derived a 1-byte hash from the VM name, which collided (two VMs
sharing the same MAC set) once applied to a 40-VM mission -- a 1-byte
space (256 values) has a >90% chance of a collision by the birthday
paradox at n=40. Indexing instead of hashing guarantees zero collisions
for any mission with <= 256 VMs (a generous prototype-scale ceiling; see
MAX_VMS_PER_MISSION), while staying just as deterministic and
reproducible -- but again, this path is a fallback and mission_alpha.yaml
does not exercise it: every one of its 240 addresses comes from the
mission file's own `macs:` section.
"""
from __future__ import annotations

import hashlib

MAX_VMS_PER_MISSION = 256  # one byte of MAC address space for VM identity


def _mission_octet(mission_name: str) -> int:
    """
    One deterministic byte derived from the mission name, so MACs from
    different missions don't collide even if they happen to place
    same-named VMs (e.g. two missions both defining a VM called 'db01').
    A 1-in-256 chance of two *missions* sharing this byte is an accepted
    prototype-scale tradeoff (unlike VM identity, mission count is
    expected to be small); OVN logical switches are mission-scoped
    regardless, so an occasional shared mission octet does not create a
    network-isolation problem, only a cosmetic MAC coincidence.
    """
    digest = hashlib.sha256(mission_name.encode("utf-8")).digest()
    return digest[0]


def compute_vm_indices(vm_names: list[str]) -> dict[str, int]:
    """
    Deterministic 0-based index per VM name: based on sorted order, so it
    depends only on the *set* of VM names in the mission, not on dict/YAML
    ordering, and is guaranteed collision-free (unlike a hash) as long as
    the mission has <= MAX_VMS_PER_MISSION VMs.
    """
    if len(vm_names) > MAX_VMS_PER_MISSION:
        raise ValueError(
            f"mission has {len(vm_names)} VMs; this MAC scheme supports at "
            f"most {MAX_VMS_PER_MISSION} per mission"
        )
    return {name: i for i, name in enumerate(sorted(vm_names))}


def generate_mac(mission_name: str, vm_index: int, interface_index: int) -> str:
    if not (0 <= vm_index < MAX_VMS_PER_MISSION):
        raise ValueError(f"vm_index must be in [0, {MAX_VMS_PER_MISSION})")
    if not (0 <= interface_index <= 255):
        raise ValueError("interface_index must fit in a single byte (0-255)")
    m = _mission_octet(mission_name)
    return f"52:54:00:{m:02x}:{vm_index:02x}:{interface_index:02x}"


def assign_all_macs(mission_name: str, mission_vms: dict[str, list], mission_macs_override: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """
    Resolve the MAC list for every VM in a mission in one pass.

    `mission_macs_override` is the mission YAML's per-VM `macs:` values
    (operator-supplied, required whenever the guest's software/licensing
    is tied to a specific MAC -- see this module's docstring). Any VM
    present in `mission_macs_override` always gets exactly those
    addresses, verbatim. Only a VM with no entry there falls back to the
    deterministic generator below, and that fallback must not be used for
    any VM with a MAC-locked guest.

    `mission_vms` maps vm_name -> its ordered interface list (only the
    length matters, to size the generated fallback list when needed).

    This MUST be the single place MACs are resolved for a mission --
    callers (services/networking.py, services/compute.py) should thread
    the returned dict through rather than recomputing per VM, both for
    efficiency and so the OVN-pinned address and the libvirt <mac> always
    agree by construction rather than by coincidence.
    """
    overrides = mission_macs_override or {}
    indices = compute_vm_indices(list(mission_vms.keys()))

    result: dict[str, list[str]] = {}
    for vm_name, interfaces in mission_vms.items():
        if vm_name in overrides:
            provided = overrides[vm_name]
            if len(provided) != len(interfaces):
                raise ValueError(
                    f"VM '{vm_name}': provided {len(provided)} MAC(s) but has "
                    f"{len(interfaces)} interface(s)"
                )
            result[vm_name] = list(provided)
        else:
            vm_idx = indices[vm_name]
            result[vm_name] = [
                generate_mac(mission_name, vm_idx, i) for i in range(len(interfaces))
            ]
    return result


def validate_no_duplicate_macs(all_macs: list[str]) -> list[str]:
    """Return any MAC addresses that appear more than once across a mission."""
    seen: dict[str, int] = {}
    for mac in all_macs:
        seen[mac] = seen.get(mac, 0) + 1
    return [mac for mac, count in seen.items() if count > 1]
