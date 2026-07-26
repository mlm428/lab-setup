"""
GPU passthrough enablement: IOMMU kernel args, VFIO module loading
(including vfio_mdev, required for MIG/vGPU mediated-device passthrough),
and physical GPU adapter discovery.

The cluster's actual GPUs (NVIDIA H100 via MIG, NVIDIA L4 via vGPU) are
passed through to VMs as mediated devices (mdevs) selected by profile
name, not as whole PCI devices -- see core/xml_render.py and
management/mission_defs/README.md's "GPU profiles" section. This module's
job stops at the physical-host layer: enabling IOMMU/VFIO so mdev
passthrough is *possible*, and discovering which physical adapters exist
so an operator knows what to partition. It does NOT create MIG/vGPU
slices itself (that's a manual, driver-specific, one-time step -- see
../scripts/enumerate_mdev_gpus.sh, which lists whatever slices already
exist so their UUIDs can be copied into config/hosts.yaml).

Per the design doc's own note: "BIOS IOMMU enabling may require manual BIOS
steps; if automated tools like ipmitool are unavailable, mark as a manual
prerequisite." We check for VT-d/AMD-Vi in dmesg and clearly flag it as a
manual step when absent, rather than silently pressing on.
"""
from __future__ import annotations

import re
import subprocess

from .util import RunContext, run, log


def ensure_iommu_kernel_args(ctx: RunContext, cluster_cfg: dict) -> bool:
    """
    Idempotently ensure the required IOMMU kernel args are present in
    /etc/default/grub's GRUB_CMDLINE_LINUX, then regenerate grub config.

    Args:
        ctx: Run context (honors --dry-run).
        cluster_cfg: Parsed config/cluster.yaml (for cpu_vendor and iommu_kernel_args).

    Returns:
        True if a change was made (host needs a reboot for it to take
        effect), False if the required args were already present.
    """
    vendor = cluster_cfg["cpu_vendor"]
    required_args = cluster_cfg["iommu_kernel_args"][vendor]
    grub_file = "/etc/default/grub"

    if ctx.dry_run:
        log.info("[dry-run] would ensure %s present in %s", required_args, grub_file)
        return False

    with open(grub_file, "r", encoding="utf-8") as fh:
        content = fh.read()

    missing = [arg for arg in required_args if arg not in content]
    if not missing:
        log.info("gpu: IOMMU kernel args already present")
        ctx.record("iommu_kernel_args", "skipped", "already present")
        return False

    def _inject(match: re.Match) -> str:
        existing = match.group(1)
        return f'GRUB_CMDLINE_LINUX="{existing} {" ".join(missing)}"'

    new_content, count = re.subn(
        r'GRUB_CMDLINE_LINUX="([^"]*)"', _inject, content, count=1
    )
    if count == 0:
        # No existing GRUB_CMDLINE_LINUX line -- append one.
        new_content = content + f'\nGRUB_CMDLINE_LINUX="{" ".join(missing)}"\n'

    with open(grub_file, "w", encoding="utf-8") as fh:
        fh.write(new_content)

    run(ctx, ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"])
    ctx.record("iommu_kernel_args", "ok", f"added {missing}; reboot required")
    log.warning("gpu: added IOMMU kernel args %s -- host reboot required", missing)
    return True


def load_vfio_modules(ctx: RunContext, cluster_cfg: dict) -> None:
    """
    Load (modprobe) and persist across reboots every kernel module listed
    in config/cluster.yaml's vfio_modules -- vfio_pci for PCI passthrough,
    vfio_mdev for MIG/vGPU mediated-device passthrough.

    Args:
        ctx: Run context (honors --dry-run).
        cluster_cfg: Parsed config/cluster.yaml.

    Returns:
        None.
    """
    modules = cluster_cfg["vfio_modules"]
    for module in modules:
        run(ctx, ["modprobe", module], check=False)
    # Persist across reboots.
    conf_path = "/etc/modules-load.d/vfio.conf"
    if not ctx.dry_run:
        with open(conf_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(modules) + "\n")
    ctx.record("vfio_modules", "ok", ", ".join(modules))
    log.info("gpu: loaded/persisted VFIO modules: %s", ", ".join(modules))


def check_iommu_enabled(ctx: RunContext) -> bool:
    """Check dmesg for VT-d/AMD-Vi enablement confirmation."""
    if ctx.dry_run:
        return True
    try:
        result = subprocess.run(["dmesg"], capture_output=True, text=True)
        stdout = result.stdout
    except FileNotFoundError:
        stdout = ""
    enabled = bool(re.search(r"(DMAR|IOMMU).*enabled", stdout, re.IGNORECASE))
    ctx.record("iommu_active", "ok" if enabled else "not_confirmed", "")
    if not enabled:
        log.warning(
            "gpu: could not confirm IOMMU is active via dmesg. If this is a "
            "fresh kernel-arg change, reboot the host. If it persists, check "
            "that VT-d/AMD-Vi is enabled in BIOS/UEFI -- this is a manual "
            "step this automation cannot perform."
        )
    return enabled


def discover_gpus(ctx: RunContext) -> list[dict]:
    """
    Enumerate this host's physical GPU adapters via `lspci -nnmm`,
    filtering for the VGA/3D controller device classes -- informational
    only, telling the operator what physical hardware is present and
    available to partition into MIG (H100) or vGPU (L4) slices. This is
    NOT a list of passthrough targets to attach directly to a VM (that
    changed from whole-PCI-device passthrough to profile-based mdev
    passthrough -- see this module's docstring); after creating slices
    with nvidia-smi/the vGPU manager, use
    ../scripts/enumerate_mdev_gpus.sh to list THOSE and record their
    UUIDs in config/hosts.yaml's gpu_devices.

    Args:
        ctx: Run context (honors --dry-run -- returns [] without running
            lspci in a dry run, since nothing downstream depends on this
            list's contents at bootstrap time).

    Returns:
        A list of {"pci_address": str, "description": str} dicts, one per
        detected VGA/3D controller.
    """
    if ctx.dry_run:
        log.info("[dry-run] would run lspci -nnmm to discover GPUs")
        return []

    try:
        proc = subprocess.run(["lspci", "-nnmm"], capture_output=True, text=True)
        stdout = proc.stdout
    except FileNotFoundError:
        log.warning("gpu: lspci not found on this host -- cannot enumerate GPUs")
        stdout = ""

    gpus = []
    for line in stdout.splitlines():
        if re.search(r'"(VGA compatible controller|3D controller)"', line):
            pci_addr = line.split()[0]
            gpus.append({"pci_address": f"0000:{pci_addr}", "description": line.strip()})
    ctx.record("gpu_discovery", "ok", f"found {len(gpus)} candidate device(s)")
    log.info("gpu: discovered %d candidate passthrough device(s)", len(gpus))
    return gpus
