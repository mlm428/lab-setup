"""
Background deployment task: the actual "read a mission spec and make it
real" workflow from the design doc, run via FastAPI's BackgroundTasks so
`POST /missions` returns immediately. Steps, in order:

  1. Create Networks   -- OVN logical switches, router, router ports
  2. Clone Disks        -- Ceph RBD clone (or local qcow2) per linked-clone VM
  3. Define VMs          -- render libvirt XML, attach OVN ports + GPU
                            hostdevs, define+start
  4. Boot VMs / Validate -- confirm VM count, NICs, MACs, disks, GPU
                            hostdevs, CPU/mem all match spec

Mirrors the design doc's four numbered steps almost exactly. On the first
failure at any step, the mission is marked Error with the failing step and
exception message recorded (per the doc: "abort on first failure...
Rollback of partial resources is complex (marked as future work)").
"""
from __future__ import annotations

from core.state import store
from core.types import MissionSpec, MissionState
from core.xml_render import StorageContext
from clients import ovn_client
from services import compute, networking, storage as storage_service, missions as missions_service, validation


def _gpu_allocator(hosts: dict) -> dict:
    """Tracks which of each host's gpu_pci_addresses have been handed out
    so two GPU VMs on the same host never get the same PCI device."""
    return {name: list(host.gpu_pci_addresses) for name, host in hosts.items()}


def _storage_context_from_cluster_cfg(cluster_cfg: dict) -> StorageContext:
    backend = cluster_cfg["storage"]["backend"]
    if backend == "ceph_rbd":
        ceph_cfg = cluster_cfg["storage"]["ceph"]
        return StorageContext(
            backend="ceph_rbd",
            ceph_pool=ceph_cfg["pool"],
            ceph_client_id=ceph_cfg["client_id"],
            ceph_secret_uuid=ceph_cfg["secret_uuid"],
            ceph_monitors=ceph_cfg["monitors"],
        )
    return StorageContext(backend="local_qcow2", local_qcow2_dir=cluster_cfg["storage"]["local_qcow2"]["dir"])


def deploy_mission(
    mission_id: str,
    mission: MissionSpec,
    hosts: dict,
    cluster_cfg: dict,
) -> None:
    storage_ctx = _storage_context_from_cluster_cfg(cluster_cfg)
    gpu_pool = _gpu_allocator(hosts)

    try:
        # --- 1. Networks ---------------------------------------------------
        store.update_state(mission_id, MissionState.DEPLOYING_NETWORKS)
        api = ovn_client.connect(cluster_cfg["ovn"]["nb_connection"])
        networking.provision_networks(api, mission)
        assigned_macs = networking.add_vm_ports(api, mission)
        store.log_step(mission_id, "networks", "ok", f"{len(mission.networks)} logical switches, {mission.total_nics()} ports")

        # --- 2. Storage ------------------------------------------------------
        store.update_state(mission_id, MissionState.CLONING_STORAGE)
        storage_service.provision_mission_storage(mission, storage_ctx)
        n_linked = sum(1 for vm in mission.vms.values() if vm.type.value == "linked_clone")
        store.log_step(mission_id, "storage", "ok", f"{n_linked} linked-clone disk(s) provisioned")

        # --- 3. Define + start VMs -------------------------------------------
        store.update_state(mission_id, MissionState.DEFINING_VMS)
        host_addresses_by_vm: dict[str, str] = {}
        gpu_pci_by_vm: dict[str, list[str]] = {}

        for vm_name, vm in mission.vms.items():
            host_name = mission.placement[vm_name]
            host_address = hosts[host_name].address  # e.g. compute01.cluster.local
            host_addresses_by_vm[vm_name] = host_name

            gpu_pci = None
            if vm.gpu:
                available = gpu_pool.get(host_name, [])
                if not available:
                    raise RuntimeError(f"no free GPU PCI device on host '{host_name}' for VM '{vm_name}'")
                gpu_pci = [available.pop(0)]
                gpu_pci_by_vm[vm_name] = gpu_pci

            compute.define_and_start_vm(
                mission=mission,
                vm_name=vm_name,
                vm=vm,
                macs=assigned_macs[vm_name],
                host_address=host_address,
                storage_ctx=storage_ctx,
                gpu_pci_addresses=gpu_pci,
                ovn_integration_bridge=cluster_cfg["ovn"]["integration_bridge"],
            )
            store.log_step(mission_id, f"define_vm:{vm_name}", "ok", host_name)

        # --- 4. Validate ------------------------------------------------------
        store.update_state(mission_id, MissionState.VALIDATING)
        missing_networks = validation.validate_networks(api, mission)
        report = validation.validate_mission_deployment(
            mission, host_addresses_by_vm, assigned_macs, gpu_pci_by_vm,
        )
        store.log_step(mission_id, "validation", "ok" if (report.ok and not missing_networks) else "error", str(report.as_dict()))

        if report.ok and not missing_networks:
            store.update_state(mission_id, MissionState.RUNNING)
        else:
            raise RuntimeError(
                f"post-deploy validation failed: missing_networks={missing_networks} "
                f"issues={report.as_dict()['issues']}"
            )

    except Exception as exc:  # noqa: BLE001 - top-level worker: capture and report, never crash the process
        store.fail(mission_id, "deploy", exc)
