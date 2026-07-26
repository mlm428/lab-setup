#!/usr/bin/env python3
"""
Generates the proof-of-concept mission from the Executive Summary's use
case: a single mission of 40 VMs (10 persistent linked-clone, 30 diskless
PXE), each with 6 NICs on isolated OVN networks, 7 of the 40 requiring GPU
passthrough (MIG on H100 / vGPU on L4).

Design choices made explicit here (the source docs specify the shape of
the scenario but not every numeric knob):

  - Interface count: the Executive Summary and the acceptance-criteria
    table ("240 virtual NICs total (40 VMs x 6)") are explicit that EVERY
    VM has all 6 networks attached.

  - Which VMs get a GPU: the source docs specify "7 of the 40 VMs" need
    GPU passthrough but don't say which. Both docs' own worked examples
    pair PXE/"render" workloads with a GPU and DB/app workloads without
    one, so we follow that pattern: all 7 GPU VMs are PXE "render" nodes;
    none of the 10 persistent (DB/app) VMs have a GPU. 4 use an H100 MIG
    profile, 3 use an L4 vGPU profile, matching config/hosts.yaml's
    available slices.

  - VM sizing (cpu/memory): sized so the whole 40-VM mission comfortably
    fits the cluster capacity in config/hosts.yaml with headroom
    (176 vCPU / ~520 GiB requested vs. 256 vCPU / 1024 GiB available).

  - MAC addresses: per operator requirement, every interface's last-three-
    octet suffix is an explicit, arbitrary-but-fixed placeholder written
    directly into this mission file (see this script's
    build_arbitrary_suffixes()) -- replace them with your actual required
    values before a real deployment. The first three octets are NOT
    written here at all: a fresh random prefix is generated for each
    individual *deployment* of this file by core/macs.py at registration
    time, so the same mission file can be deployed more than once
    concurrently, fully isolated at the MAC layer, while every
    deployment's suffixes (and therefore any operator substring-matching
    tooling relying on them) stay identical. See
    management/mission_defs/README.md for the full explanation.

  - Golden images: `image_revision: Alpha` on every linked-clone VM,
    matching config/storage.yaml's golden.images catalog.

Usage:
    python3 tools/generate_mission.py
      (writes management/mission_defs/mission_alpha.yaml)
    python3 tools/generate_mission.py --small
      (writes management/mission_defs/mission_bravo.yaml -- a 4-VM
       (2 linked-clone + 2 PXE) quick-test mission with the same
       conventions applied)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

MANAGEMENT_DIR = Path(__file__).resolve().parent.parent / "management"
sys.path.insert(0, str(MANAGEMENT_DIR))

from core.placement import greedy_place  # noqa: E402
from services.missions import load_host_inventory  # noqa: E402

FILE_FORMAT_VERSION = "1.0.0"   # bump when the mission YAML *schema* itself changes; see mission_defs/README.md

NETWORKS = {
    "control": 100,
    "storage": 110,
    "operations": 120,
    "sensor": 130,
    "external": 140,
    "management": 150,
}
ALL_NETWORKS = list(NETWORKS.keys())


def build_vms(roles: list[tuple]) -> dict:
    """
    Expand a list of (role_prefix, count, type, cpu, memory_mb, gpu_profile, image)
    tuples into the mission's `vms:` dict, giving every VM all 6 networks.
    """
    vms = {}
    for prefix, count, vm_type, cpu, mem, gpu_profile, image in roles:
        for i in range(1, count + 1):
            name = f"{prefix}{i:02d}"
            vms[name] = {
                "type": vm_type,
                "cpu": cpu,
                "memory": mem,
                "interfaces": {net: {} for net in ALL_NETWORKS},  # mac_suffix filled in by build_arbitrary_suffixes()
            }
            if gpu_profile:
                vms[name]["gpu"] = {"profile": gpu_profile}
            if image:
                vms[name]["image"] = image
                vms[name]["image_revision"] = "Alpha"
    return vms


def build_arbitrary_suffixes(vms: dict) -> None:
    """
    Fill in an arbitrary-but-unique mac_suffix for every interface of
    every VM, IN PLACE. One byte for VM index (sorted order, guaranteeing
    no two VMs collide up to 256 VMs) + one byte for interface index --
    THESE ARE PLACEHOLDERS (see module docstring): swap in the actual
    suffixes your licensed guest images require before a real deployment.
    """
    for vm_idx, vm_name in enumerate(sorted(vms.keys())):
        for if_idx, net_name in enumerate(vms[vm_name]["interfaces"].keys()):
            vms[vm_name]["interfaces"][net_name] = {"mac_suffix": f"10:{vm_idx:02x}:{if_idx:02x}"}


def write_mission(name: str, roles: list[tuple], out_path: Path) -> None:
    """Build, place, and write one mission YAML file."""
    vms = build_vms(roles)
    build_arbitrary_suffixes(vms)
    hosts = load_host_inventory()

    gpu_vm_names = sorted([n for n, v in vms.items() if "gpu" in v])
    other_vm_names = sorted([n for n, v in vms.items() if "gpu" not in v], key=lambda n: -vms[n]["cpu"])
    ordered_names = gpu_vm_names + other_vm_names

    requirements = {
        n: {"cpu": v["cpu"], "memory_mb": v["memory"], "gpu_profile": v.get("gpu", {}).get("profile")}
        for n, v in vms.items()
    }
    placement = greedy_place(ordered_names, requirements, hosts)

    mission_doc = {
        "mission": name,
        "file_version": FILE_FORMAT_VERSION,
        "networks": NETWORKS,
        "vms": vms,
        "placement": placement,
    }

    total_cpu = sum(v["cpu"] for v in vms.values())
    total_mem_gb = sum(v["memory"] for v in vms.values()) / 1024
    total_nics = sum(len(v["interfaces"]) for v in vms.values())
    n_gpu = sum(1 for v in vms.values() if "gpu" in v)
    n_linked = sum(1 for v in vms.values() if v["type"] == "linked_clone")
    n_pxe = sum(1 for v in vms.values() if v["type"] == "pxe")

    header = (
        f"# Generated by tools/generate_mission.py -- DO NOT hand-edit the vms/\n"
        f"# placement sections; re-run the generator instead so they stay\n"
        f"# consistent with each other and with config/hosts.yaml. See\n"
        f"# management/mission_defs/README.md for the full schema reference\n"
        f"# and management/mission_defs/template.yaml for an annotated example.\n"
        f"#\n"
        f"# file_version: {FILE_FORMAT_VERSION} -- this mission file's own revision,\n"
        f"# recorded in deployment status/step logs for audit purposes.\n"
        f"#\n"
        f"# Summary: {len(vms)} VMs ({n_linked} linked-clone, {n_pxe} PXE), "
        f"{n_gpu} with GPU passthrough, {total_nics} total NICs.\n"
        f"# Requested: {total_cpu} vCPU, {total_mem_gb:.0f} GiB memory.\n"
        f"#\n"
        f"# IMPORTANT -- every interface's mac_suffix below is an ARBITRARY\n"
        f"# PLACEHOLDER, not a generated/derived value. Per operator\n"
        f"# confirmation, guest software running in these VMs has MAC\n"
        f"# addresses hardcoded into its licensing/configuration, so the real\n"
        f"# suffixes are a manual input the operator is responsible for.\n"
        f"# Before deploying against real hardware, review every VM's\n"
        f"# interfaces: below and replace these placeholders with the actual\n"
        f"# required values. The first three octets of every MAC are NOT\n"
        f"# configured here at all -- a fresh random prefix is generated for\n"
        f"# each individual deployment of this file at registration time (see\n"
        f"# core/macs.py), so this same file can be deployed more than once\n"
        f"# concurrently, fully isolated at the MAC layer, while your\n"
        f"# suffix-based substring matching keeps working inside each\n"
        f"# deployment's own isolated network.\n"
    )

    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump(mission_doc, fh, sort_keys=False, default_flow_style=False)

    print(f"Wrote {out_path}")
    print(f"{len(vms)} VMs, {total_nics} NICs, {n_gpu} GPU VMs, {total_cpu} vCPU, {total_mem_gb:.0f} GiB requested")
    print("NOTE: interfaces[].mac_suffix values are arbitrary placeholders -- replace with your real required values before deploying.")

    per_host_cpu: dict[str, int] = {h: 0 for h in hosts}
    per_host_gpu: dict[str, int] = {h: 0 for h in hosts}
    for vm_name, host in placement.items():
        per_host_cpu[host] += vms[vm_name]["cpu"]
        if "gpu" in vms[vm_name]:
            per_host_gpu[host] += 1
    for h in hosts:
        print(f"  {h}: {per_host_cpu[h]}/{hosts[h].cpus} vCPU, {per_host_gpu[h]} GPU VM(s)")


ALPHA_ROLES = [
    # (role prefix, count, type, cpu, memory_mb, gpu_profile, image)
    ("db", 4, "linked_clone", 4, 16384, None, "rhel9-db-golden"),
    ("app", 6, "linked_clone", 2, 8192, None, "rhel9-app-golden"),
    ("render", 4, "pxe", 8, 32768, "H100-MIG-3g.40gb", None),
    ("gfxrender", 3, "pxe", 8, 24576, "L4-vGPU-4Q", None),
    ("worker", 23, "pxe", 4, 8192, None, None),
]

BRAVO_ROLES = [
    # A quick-test mission: 2 linked-clone + 2 PXE, same conventions.
    ("db", 1, "linked_clone", 4, 16384, None, "rhel9-db-golden"),
    ("app", 1, "linked_clone", 2, 8192, None, "rhel9-app-golden"),
    ("render", 1, "pxe", 8, 32768, "H100-MIG-3g.40gb", None),
    ("worker", 1, "pxe", 4, 8192, None, None),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small", action="store_true", help="Also/instead generate the small 4-VM test mission (mission_bravo.yaml)")
    parser.add_argument("--only-small", action="store_true", help="Generate ONLY mission_bravo.yaml, skip mission_alpha.yaml")
    args = parser.parse_args()

    mission_defs_dir = MANAGEMENT_DIR / "mission_defs"

    if not args.only_small:
        write_mission("Mission-Alpha", ALPHA_ROLES, mission_defs_dir / "mission_alpha.yaml")
    if args.small or args.only_small:
        write_mission("Mission-Bravo", BRAVO_ROLES, mission_defs_dir / "mission_bravo.yaml")


if __name__ == "__main__":
    main()
