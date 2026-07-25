#!/usr/bin/env python3
"""
Generates the proof-of-concept mission from the Executive Summary's use
case: a single mission of 40 VMs (10 persistent linked-clone, 30 diskless
PXE), each with 6 NICs on isolated OVN networks, 7 of the 40 requiring GPU
passthrough.

Design choices made explicit here (the source docs specify the shape of
the scenario but not every numeric knob):

  - Interface count: the Executive Summary and the acceptance-criteria
    table ("240 virtual NICs total (40 VMs x 6)") are explicit that EVERY
    VM has all 6 networks attached. The design doc's own illustrative
    mission.yaml snippet shows some example VMs with only 2-3 interfaces
    ("(other 3 unused)") -- we follow the authoritative acceptance
    criterion (all 6) over the illustrative snippet.

  - Which VMs get a GPU: the source docs specify "7 of the 40 VMs" need
    GPU passthrough but don't say which. Both docs' own worked examples
    pair PXE/"render" workloads with gpu=true and DB/app workloads with
    gpu=false, so we follow that pattern: all 7 GPU VMs are PXE "render"
    nodes; none of the 10 persistent (DB/app) VMs have a GPU.

  - VM sizing (cpu/memory): not specified by either doc beyond a couple of
    worked examples. Sized here so the whole 40-VM mission comfortably
    fits the cluster capacity in inventory/hosts.yaml with headroom
    (176 vCPU / ~520 GiB requested vs. 256 vCPU / 1024 GiB available).

  - GPU host capacity: the source doc's own illustrative host table only
    has 4 total GPU slots (2+2), insufficient for 7 GPU VMs -- see
    inventory/hosts.yaml's header comment for the fix (bumped to 4+4=8).

  - Static MACs: the operator has confirmed that guest software running
    inside these VMs has MAC addresses hardcoded into its licensing/
    configuration -- so the MAC for every interface is a value the
    operator determines and is responsible for, not something this system
    is free to invent at deploy time. This generator therefore writes an
    explicit `macs:` entry for all 40 VMs (240 addresses total) directly
    into mission_alpha.yaml. The values below are arbitrary placeholders
    from QEMU/KVM's locally-administered OUI (52:54:00), sequentially
    numbered for readability -- replace them with your actual required
    addresses before deploying against real hardware/licensed images.
    core/macs.py's deterministic generator is NOT used here; it exists
    only as a fallback for VMs a mission leaves unspecified, and no VM in
    this mission is left unspecified.

Usage:
    python3 tools/generate_mission.py
      (writes management/mission_defs/mission_alpha.yaml and
       management/placement/mission_alpha_place.yaml)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

MANAGEMENT_DIR = Path(__file__).resolve().parent.parent / "management"
sys.path.insert(0, str(MANAGEMENT_DIR))

from core.placement import greedy_place  # noqa: E402
from core.types import HostSpec  # noqa: E402

MISSION_NAME = "Mission-Alpha"

NETWORKS = {
    "control": 100,
    "storage": 110,
    "operations": 120,
    "sensor": 130,
    "external": 140,
    "management": 150,
}
ALL_NETWORKS = list(NETWORKS.keys())

ROLES = [
    # (role prefix, count, type, cpu, memory_mb, gpu, image)
    ("db", 4, "linked_clone", 4, 16384, False, "rhel9-db-golden"),
    ("app", 6, "linked_clone", 2, 8192, False, "rhel9-app-golden"),
    ("render", 7, "pxe", 8, 32768, True, None),
    ("worker", 23, "pxe", 4, 8192, False, None),
]


def build_vms() -> dict:
    vms = {}
    for prefix, count, vm_type, cpu, mem, gpu, image in ROLES:
        for i in range(1, count + 1):
            name = f"{prefix}{i:02d}"
            vms[name] = {
                "type": vm_type,
                "cpu": cpu,
                "memory": mem,
                "gpu": gpu,
                "interfaces": list(ALL_NETWORKS),
            }
            if image:
                vms[name]["image"] = image
    return vms


def build_arbitrary_macs(vm_names: list[str]) -> dict[str, list[str]]:
    """
    Arbitrary, placeholder-but-unique MAC addresses for every interface of
    every VM, written explicitly into the mission file per the operator's
    requirement (see module docstring). One byte for VM index (sorted
    order, guaranteeing no two VMs collide up to 256 VMs) + one byte for
    interface index (0-5 here). THESE ARE PLACEHOLDERS: swap in the actual
    MACs your licensed guest images require before a real deployment.
    """
    macs: dict[str, list[str]] = {}
    for vm_idx, vm_name in enumerate(sorted(vm_names)):
        macs[vm_name] = [
            f"52:54:00:10:{vm_idx:02x}:{if_idx:02x}" for if_idx in range(len(ALL_NETWORKS))
        ]
    return macs


def load_hosts_for_placement() -> dict[str, HostSpec]:
    with open(MANAGEMENT_DIR / "inventory" / "hosts.yaml", "r", encoding="utf-8") as fh:
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


def main() -> None:
    vms = build_vms()
    hosts = load_hosts_for_placement()
    arbitrary_macs = build_arbitrary_macs(list(vms.keys()))

    # Place GPU VMs first (while GPU-host capacity is still fully free),
    # then everything else, largest-CPU-first within each group -- a
    # simple, deterministic ordering for the reference greedy bin-packer.
    gpu_vm_names = sorted([n for n, v in vms.items() if v["gpu"]])
    other_vm_names = sorted([n for n, v in vms.items() if not v["gpu"]], key=lambda n: -vms[n]["cpu"])
    ordered_names = gpu_vm_names + other_vm_names

    requirements = {
        name: {"cpu": v["cpu"], "memory_mb": v["memory"], "gpu": v["gpu"]}
        for name, v in vms.items()
    }
    placement = greedy_place(ordered_names, requirements, hosts)

    mission_doc = {
        "mission": MISSION_NAME,
        "networks": NETWORKS,
        "vms": vms,
        "placement": placement,
        "macs": arbitrary_macs,
    }

    total_cpu = sum(v["cpu"] for v in vms.values())
    total_mem_gb = sum(v["memory"] for v in vms.values()) / 1024
    total_nics = sum(len(v["interfaces"]) for v in vms.values())
    n_gpu = sum(1 for v in vms.values() if v["gpu"])
    n_linked = sum(1 for v in vms.values() if v["type"] == "linked_clone")
    n_pxe = sum(1 for v in vms.values() if v["type"] == "pxe")

    header = (
        f"# Generated by tools/generate_mission.py -- DO NOT hand-edit the vms/\n"
        f"# placement sections; re-run the generator instead so they stay\n"
        f"# consistent with each other and with inventory/hosts.yaml.\n"
        f"#\n"
        f"# Summary: {len(vms)} VMs ({n_linked} linked-clone, {n_pxe} PXE), "
        f"{n_gpu} with GPU passthrough, {total_nics} total NICs.\n"
        f"# Requested: {total_cpu} vCPU, {total_mem_gb:.0f} GiB memory.\n"
        f"#\n"
        f"# IMPORTANT -- macs: below are ARBITRARY PLACEHOLDERS, not\n"
        f"# generated/derived values. Per operator confirmation, guest\n"
        f"# software running in these VMs has MAC addresses hardcoded into\n"
        f"# its licensing/configuration, so the real addresses are a\n"
        f"# manual input the operator is responsible for. Before deploying\n"
        f"# against real hardware, review every VM's macs: list below and\n"
        f"# replace these placeholders with the actual required addresses.\n"
        f"# The system uses whatever is written here as-is; it does not\n"
        f"# regenerate or validate them against any external source.\n"
    )

    mission_defs_dir = MANAGEMENT_DIR / "mission_defs"
    placement_dir = MANAGEMENT_DIR / "placement"
    mission_defs_dir.mkdir(exist_ok=True)
    placement_dir.mkdir(exist_ok=True)

    with open(mission_defs_dir / "mission_alpha.yaml", "w", encoding="utf-8") as fh:
        fh.write(header)
        yaml.safe_dump(mission_doc, fh, sort_keys=False, default_flow_style=False)

    with open(placement_dir / "mission_alpha_place.yaml", "w", encoding="utf-8") as fh:
        fh.write(
            "# Reference copy of mission_alpha.yaml's placement mapping,\n"
            "# kept here per the design doc's repo layout for readability.\n"
            "# The authoritative copy submitted to POST /missions is the\n"
            "# `placement:` section embedded in mission_defs/mission_alpha.yaml\n"
            "# (matching the API's single-document MissionDefinition schema).\n"
        )
        yaml.safe_dump({"mission": MISSION_NAME, "placement": placement}, fh, sort_keys=False, default_flow_style=False)

    print(f"Wrote {mission_defs_dir / 'mission_alpha.yaml'}")
    print(f"Wrote {placement_dir / 'mission_alpha_place.yaml'}")
    print(f"{len(vms)} VMs, {total_nics} NICs, {n_gpu} GPU VMs, {total_cpu} vCPU, {total_mem_gb:.0f} GiB requested")
    print("NOTE: macs: in mission_alpha.yaml are arbitrary placeholders -- replace with your real required addresses before deploying.")

    per_host_cpu: dict[str, int] = {h: 0 for h in hosts}
    per_host_gpu: dict[str, int] = {h: 0 for h in hosts}
    for name, host in placement.items():
        per_host_cpu[host] += vms[name]["cpu"]
        if vms[name]["gpu"]:
            per_host_gpu[host] += 1
    for h in hosts:
        print(f"  {h}: {per_host_cpu[h]}/{hosts[h].cpus} vCPU, {per_host_gpu[h]}/{hosts[h].gpus} GPU VMs")


if __name__ == "__main__":
    main()
