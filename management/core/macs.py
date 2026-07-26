"""
MAC address resolution for mission deployments.

OPERATIONAL REQUIREMENT (confirmed by the operator): guest software
running inside mission VMs has MAC addresses hardcoded into its
licensing/configuration. The operator therefore specifies, per VM
interface, only the *last three octets* of that interface's MAC (e.g.
"10:00:00") directly in the mission file (see
management/mission_defs/README.md) -- that value must stay identical
across every deployment of a given mission so the operator's own
in-guest tooling can substring-match on it.

The *first* three octets (an OUI-like prefix) are NOT specified in the
mission file at all. Instead, one random prefix is generated fresh for
each individual deployment of a mission and applied to every interface in
that deployment uniformly. This is what lets the operator run two (or
more) concurrent deployments of the exact same mission definition, fully
isolated from each other at the MAC layer, while every deployment's
in-guest substring match on the configured suffix still finds the right
interface within its own deployment's L2 domain.

    deployment 1:  AA:BB:CC:10:00:00   (random prefix + configured suffix)
    deployment 2:  XX:YY:ZZ:10:00:00   (different random prefix, same suffix)

If an interface's mac_suffix is left unset in the mission file, a random
suffix is generated for it instead -- appropriate only when that
interface has no external substring-matching requirement, since an
unspecified suffix is not guaranteed to repeat across redeploys.

Prefixes are generated with the "locally administered, unicast" bits set
(the same convention QEMU/KVM's own fixed 52:54:00 OUI follows), so
randomly-assigned prefixes never collide with a real hardware vendor's
assigned OUI range.
"""
from __future__ import annotations

import random

from .types import MissionSpec

LOCALLY_ADMINISTERED_BIT = 0b0000_0010
MULTICAST_BIT = 0b0000_0001


def _random_octet_triplet(rng: random.Random) -> str:
    """Generate 3 random octets, formatted as e.g. 'a1:b2:c3'."""
    return ":".join(f"{rng.randint(0, 255):02x}" for _ in range(3))


def _force_locally_administered_unicast(first_octet_hex: str) -> str:
    """
    Set the locally-administered bit and clear the multicast bit on a MAC
    address's first octet, so a randomly-generated prefix is a valid
    unicast address in the locally-administered range (never collides
    with a real vendor-assigned OUI) -- the same convention QEMU/KVM's own
    fixed 52:54:00 prefix follows (0x52 = 0101_0010: bit 1 set, bit 0 clear).
    """
    value = int(first_octet_hex, 16)
    value = (value & ~MULTICAST_BIT) | LOCALLY_ADMINISTERED_BIT
    return f"{value:02x}"


def random_prefix(existing_prefixes: set[str], rng: random.Random | None = None, max_attempts: int = 4096) -> str:
    """
    Generate a random 3-octet MAC prefix that does not collide with any
    prefix in `existing_prefixes` (the prefixes already reserved by other
    currently-active mission deployments).

    Args:
        existing_prefixes: Prefixes ("aa:bb:cc" strings) already in use by
            other active deployments -- see core/state.py's
            MissionStore.active_mac_prefixes().
        rng: Source of randomness; defaults to random.SystemRandom() (a
            deterministic rng can be passed in tests for repeatable output).
        max_attempts: Safety bound before giving up.

    Returns:
        A new prefix string, e.g. "4a:1e:9c", guaranteed not to be in
        `existing_prefixes` and to have the locally-administered-unicast
        bit pattern set on its first octet.

    Raises:
        RuntimeError: if no free prefix was found within `max_attempts`
            tries (implies an implausible number of concurrent deployments
            for a 3-octet, locally-administered space).
    """
    rng = rng or random.SystemRandom()
    for _ in range(max_attempts):
        triplet = _random_octet_triplet(rng)
        b0, b1, b2 = triplet.split(":")
        candidate = f"{_force_locally_administered_unicast(b0)}:{b1}:{b2}"
        if candidate not in existing_prefixes:
            return candidate
    raise RuntimeError(
        f"could not find a free MAC prefix after {max_attempts} attempts "
        f"({len(existing_prefixes)} prefixes already in use)"
    )


def random_suffix(existing_suffixes: set[str], rng: random.Random | None = None, max_attempts: int = 4096) -> str:
    """
    Generate a random 3-octet MAC suffix not already in `existing_suffixes`.
    Used for an interface whose mission file left `mac_suffix` unset.

    Args:
        existing_suffixes: Suffixes already assigned elsewhere in this
            same mission deployment (across all VMs), so the result is
            unique within the deployment.
        rng: Source of randomness; see random_prefix.
        max_attempts: Safety bound before giving up.

    Returns:
        A new suffix string, e.g. "7b:22:0f".

    Raises:
        RuntimeError: if no free suffix was found within `max_attempts` tries.
    """
    rng = rng or random.SystemRandom()
    for _ in range(max_attempts):
        candidate = _random_octet_triplet(rng)
        if candidate not in existing_suffixes:
            return candidate
    raise RuntimeError(
        f"could not find a free MAC suffix after {max_attempts} attempts "
        f"({len(existing_suffixes)} suffixes already in use in this deployment)"
    )


def resolve_mission_macs(
    mission: MissionSpec,
    existing_prefixes: set[str],
    rng: random.Random | None = None,
) -> tuple[str, dict[str, list[str]]]:
    """
    Resolve full 6-octet MAC addresses for every interface of every VM in
    one mission deployment.

    Generates a single random prefix for the whole deployment (unique
    against every other currently-active deployment's prefix -- see
    random_prefix), then combines it with each interface's configured
    `mac_suffix` (falling back to a freshly generated random suffix for
    any interface that left it unset) to produce the address libvirt/OVN
    actually use.

    Args:
        mission: The mission being deployed. Not mutated.
        existing_prefixes: Prefixes already reserved by other active
            deployments (see core/state.py's MissionStore.active_mac_prefixes()).
        rng: Source of randomness; see random_prefix.

    Returns:
        (prefix, {vm_name: [full_mac, ...]}) -- the prefix chosen for this
        deployment, and every VM's resolved MAC list in the same order as
        that VM's interfaces (VMSpec.interface_names()).

    Raises:
        ValueError: if two different VMs in the mission were configured
            with the same mac_suffix -- since the prefix is identical for
            the whole deployment, that would produce two interfaces with
            an identical full MAC. (A single VM reusing a suffix across
            its own interfaces is already rejected earlier, by
            VMSpec.__post_init__.)
        RuntimeError: if a free prefix or suffix could not be found.
    """
    rng = rng or random.SystemRandom()
    prefix = random_prefix(existing_prefixes, rng)

    used_suffixes: set[str] = set()
    result: dict[str, list[str]] = {}
    conflicts: list[str] = []

    for vm_name, vm in mission.vms.items():
        macs: list[str] = []
        for net_name, iface in vm.interfaces.items():
            suffix = iface.mac_suffix
            if suffix is None:
                suffix = random_suffix(used_suffixes, rng)
            elif suffix in used_suffixes:
                conflicts.append(f"{vm_name}/{net_name} (suffix {suffix})")
            used_suffixes.add(suffix)
            macs.append(f"{prefix}:{suffix}")
        result[vm_name] = macs

    if conflicts:
        raise ValueError(
            f"mission '{mission.name}': duplicate mac_suffix used by more than one "
            f"interface across different VMs -- every interface in a deployment needs "
            f"a distinct suffix, since the {prefix} prefix is shared by the whole "
            f"deployment and identical suffixes would produce identical MACs: {conflicts}"
        )

    return prefix, result


def validate_no_duplicate_macs(all_macs: list[str]) -> list[str]:
    """Return any MAC addresses that appear more than once in `all_macs`."""
    seen: dict[str, int] = {}
    for mac in all_macs:
        seen[mac] = seen.get(mac, 0) + 1
    return [mac for mac, count in seen.items() if count > 1]
