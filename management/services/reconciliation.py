"""
Startup reconciliation: scans every host's actual live libvirt domains
and compares them against the mission database (core/state.py's SQLite
persistence, if configured), so state loss or drift gets surfaced
immediately -- before the service starts accepting new deployment
requests -- rather than silently.

Two distinct failure modes this catches, per operator request ("if it
restarts, i think it would be good to do a scan of current VMs deployed
throughout the environment... to firstly populate the database prior to
running new management services and calls for deployments"):

  1. The database has no record of a mission at all (its file was lost,
     deleted, or this is a fresh management-service instance pointed at
     an already-provisioned cluster) but VMs belonging to it are still
     running. Reconstructed on a best-effort basis directly from what's
     live (VM names, MACs, GPU mdev UUIDs) -- see the module-level note
     below on what this can and can't recover.
  2. The database says a mission should be Running, but some or all of
     its VMs are missing on their hosts (deleted out-of-band via virsh
     directly, a host that never came back up after a reboot, etc.).
     Flagged clearly rather than silently left showing "Running".

VM -> mission attribution works by reading each domain's embedded
<metadata> block (written by core/xml_render.py at deploy time), not by
guessing from naming conventions -- so this can always correctly
attribute a live VM to its mission_id/name, even across a management
service restart where the in-memory MissionSpec objects from before the
restart no longer exist.

WHAT RECONSTRUCTION CANNOT RECOVER: a live domain's XML exposes its
actual MAC addresses, GPU mdev UUIDs, vCPU/memory, and (for a
linked-clone VM) its RBD/qcow2 disk path -- but NOT the mission's
original `networks:` VLAN mapping, `image_revision` pinning, or
`file_version`, none of which are written into the domain XML itself.
A mission reconstructed this way therefore gets a minimal MissionStatus
(state, resolved MACs, GPU allocations) with `spec=None` -- good enough to
free its MAC prefix/GPU reservations correctly and to know it exists, but
NOT enough to redeploy or fully re-validate against acceptance criteria
without the operator re-supplying the original mission file.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from clients import libvirt_client
from core.logging_setup import log
from core.state import MissionStore
from core.types import HostSpec, MissionState, MissionStatus

METADATA_NS = "https://github.com/anthropics/mission-cluster/metadata"


@dataclass
class DiscoveredVM:
    """One running domain whose embedded metadata identifies which mission/VM it belongs to."""
    mission_id: str
    mission_name: str
    vm_name: str
    host_name: str
    macs: list[str]
    gpu_mdev_uuids: list[str]


@dataclass
class ReconciliationReport:
    """Summary of one startup reconciliation pass."""
    discovered_vm_count: int = 0
    missions_confirmed_intact: list[str] = field(default_factory=list)
    missions_missing_vms: dict[str, list[str]] = field(default_factory=dict)  # mission_id -> [expected vm names not found]
    missions_reconstructed: list[str] = field(default_factory=list)  # mission_ids found live but absent from the DB entirely

    @property
    def ok(self) -> bool:
        """True if nothing was missing and nothing needed reconstruction -- a fully clean startup."""
        return not self.missions_missing_vms and not self.missions_reconstructed

    def as_dict(self) -> dict:
        """JSON-friendly representation, for logging/status output."""
        return {
            "ok": self.ok,
            "discovered_vm_count": self.discovered_vm_count,
            "missions_confirmed_intact": self.missions_confirmed_intact,
            "missions_missing_vms": self.missions_missing_vms,
            "missions_reconstructed": self.missions_reconstructed,
        }


def _parse_domain_metadata(xml_desc: str) -> "tuple[str, str, str] | None":
    """Extract (mission_id, mission_name, vm_name) from a domain's embedded <metadata>, or None if absent (e.g. a VM this system didn't create)."""
    root = ET.fromstring(xml_desc)
    info = root.find(f"./metadata/{{{METADATA_NS}}}info")
    if info is None:
        return None
    mission_id_el = info.find(f"{{{METADATA_NS}}}id")
    mission_name_el = info.find(f"{{{METADATA_NS}}}name")
    vm_name_el = info.find(f"{{{METADATA_NS}}}vm")
    if mission_id_el is None or mission_name_el is None or vm_name_el is None:
        return None
    return mission_id_el.text, mission_name_el.text, vm_name_el.text


def _extract_macs(xml_desc: str) -> list[str]:
    """Read every interface's actual assigned MAC directly from a domain's live XML."""
    root = ET.fromstring(xml_desc)
    return [i.find("mac").get("address") for i in root.findall("./devices/interface") if i.find("mac") is not None]


def _extract_gpu_uuids(xml_desc: str) -> list[str]:
    """Read every attached mdev GPU's UUID directly from a domain's live XML."""
    root = ET.fromstring(xml_desc)
    uuids = []
    for hostdev in root.findall("./devices/hostdev"):
        addr = hostdev.find("./source/address")
        if addr is not None and addr.get("uuid"):
            uuids.append(addr.get("uuid"))
    return uuids


def scan_live_vms(hosts: dict[str, HostSpec], ssh_user: str = "root") -> list[DiscoveredVM]:
    """
    Query every host's actual libvirt domains and extract mission
    attribution from each one's embedded metadata.

    Args:
        hosts: Full host inventory to scan.
        ssh_user: Non-root SSH user for each host's libvirt connection
            (see config/hosts.yaml's management_ssh_user).

    Returns:
        One DiscoveredVM per running domain that carries this system's
        metadata block. Domains lacking it (e.g. manually created VMs
        unrelated to this tooling) are silently skipped, not reported as
        an error -- their presence isn't this system's concern.
    """
    discovered = []
    for host_name, host in hosts.items():
        conn = libvirt_client.connect(host.address, ssh_user=ssh_user)
        try:
            for vm_name in libvirt_client.list_domain_names(conn):
                xml_desc = libvirt_client.domain_xml_desc(conn, vm_name)
                parsed = _parse_domain_metadata(xml_desc)
                if parsed is None:
                    continue
                mission_id, mission_name, meta_vm_name = parsed
                discovered.append(DiscoveredVM(
                    mission_id=mission_id, mission_name=mission_name, vm_name=meta_vm_name,
                    host_name=host_name, macs=_extract_macs(xml_desc), gpu_mdev_uuids=_extract_gpu_uuids(xml_desc),
                ))
        finally:
            conn.close()
    return discovered


def reconcile_on_startup(hosts: dict[str, HostSpec], store: MissionStore, ssh_user: str = "root") -> ReconciliationReport:
    """
    Compare the mission database (already reloaded from persistent
    storage, if configured -- see core/state.py's MissionStore.__init__)
    against what's actually running on the cluster right now, and surface
    any drift before the service starts accepting new deployment
    requests. Every finding is both returned in the report and logged
    (core/logging_setup.py) at an appropriate level.

    Args:
        hosts: Full host inventory to scan.
        store: The MissionStore to reconcile against (and update, for
            missions discovered live but entirely absent from the DB).
        ssh_user: Non-root SSH user for each host's libvirt connection
            (see config/hosts.yaml's management_ssh_user).

    Returns:
        A ReconciliationReport summarizing what was found. Never raises --
        a scan failure against one host (e.g. unreachable) is logged and
        that host is simply skipped, so one bad host doesn't block startup
        entirely.
    """
    report = ReconciliationReport()

    try:
        discovered = scan_live_vms(hosts, ssh_user=ssh_user)
    except Exception as exc:  # noqa: BLE001 - reconciliation must never prevent the service from starting
        log.error("reconciliation: live cluster scan failed, proceeding without it: %s", exc, exc_info=True)
        return report

    report.discovered_vm_count = len(discovered)

    by_mission: dict[str, list[DiscoveredVM]] = {}
    for d in discovered:
        by_mission.setdefault(d.mission_id, []).append(d)

    known_mission_ids = {m.mission_id for m in store.list()}

    for mission_id, vms in by_mission.items():
        if mission_id not in known_mission_ids:
            status = MissionStatus(
                mission_id=mission_id,
                name=vms[0].mission_name,
                state=MissionState.RUNNING,
                resolved_macs={v.vm_name: v.macs for v in vms},
                gpu_allocations={v.vm_name: v.gpu_mdev_uuids[0] for v in vms if v.gpu_mdev_uuids},
            )
            status.log(
                "reconciliation", "ok",
                "reconstructed from a live cluster scan on startup -- no prior database "
                "record existed for this mission_id; original networks/placement/image/"
                "file_version metadata is NOT recoverable this way, only what each VM's "
                "own live domain XML exposes (VM names, MACs, GPU mdev UUIDs)",
            )
            store.adopt(status)
            report.missions_reconstructed.append(mission_id)
            log.warning(
                "reconciliation: mission '%s' (%s) found running live with no database record -- "
                "reconstructed a minimal status for it (%d VM(s))",
                vms[0].mission_name, mission_id, len(vms),
            )
            continue

        existing = store.get(mission_id)
        if existing.spec is not None:
            expected_vm_names = set(existing.spec.vms.keys())
            found_vm_names = {v.vm_name for v in vms}
            missing = expected_vm_names - found_vm_names
            if missing:
                report.missions_missing_vms[mission_id] = sorted(missing)
                store.log_step(mission_id, "reconciliation", "error", f"expected VMs not found running after restart: {sorted(missing)}")
                log.warning("reconciliation: mission '%s' (%s) is missing VMs after restart: %s", existing.name, mission_id, sorted(missing))
            else:
                report.missions_confirmed_intact.append(mission_id)

    # Missions the DB believes are Running but that had ZERO VMs
    # discovered anywhere (the whole thing is gone) also belong in
    # missions_missing_vms, not silently ignored.
    for status in store.list():
        if status.state == MissionState.RUNNING and status.mission_id not in by_mission and status.spec:
            report.missions_missing_vms[status.mission_id] = sorted(status.spec.vms.keys())
            store.log_step(status.mission_id, "reconciliation", "error", "no VMs for this mission found running on any host after restart")
            log.warning("reconciliation: mission '%s' (%s) has NO VMs running anywhere after restart", status.name, status.mission_id)

    log.info(
        "reconciliation: scan complete -- %d live VM(s) found, %d mission(s) confirmed intact, "
        "%d mission(s) reconstructed, %d mission(s) with missing VMs",
        report.discovered_vm_count, len(report.missions_confirmed_intact),
        len(report.missions_reconstructed), len(report.missions_missing_vms),
    )
    return report
