"""
Background deployment task: the actual "read a mission spec and make it
real" workflow from the design doc, run via FastAPI's BackgroundTasks so
`POST /missions` returns immediately. Steps, in order:

  1. Create Networks   -- OVN logical switches, router, router ports
  2. Clone Disks        -- Ceph RBD clone/copy (or local qcow2) per linked-clone VM
  3. Define VMs          -- render libvirt XML, attach OVN ports + GPU
                            mdev hostdevs, define+start
  4. Boot VMs / Validate -- confirm VM count, NICs, MACs, disks, GPU
                            hostdevs, CPU/mem all match spec

Mirrors the design doc's four numbered steps almost exactly. On the first
failure at any step, the mission is marked Error (with the failing step
and exception message recorded on its status AND emitted via this
process's own logger -- see core/logging_setup.py -- so it surfaces in
the service's console/journal output too, not only via the API), and
every resource this attempt had already created is automatically rolled
back (see _rollback_partial_deployment below) so the operator can
resubmit the same mission immediately without hitting "already exists" or
MAC-prefix/GPU-slice contention errors from the half-finished attempt.
"""
from __future__ import annotations

from clients import ovn_client
from core.logging_setup import log
from core.state import store
from core.types import HostSpec, MissionSpec, MissionState
from services import compute, networking
from services import storage as storage_service
from services import validation
from services.cluster_config import DeploymentConfig


def deploy_mission(
    mission_id: str,
    mission: MissionSpec,
    hosts: dict[str, HostSpec],
    deployment_cfg: DeploymentConfig,
) -> None:
    """
    Run the full four-step provisioning workflow for one mission
    deployment. Never raises -- any exception is caught, logged, and
    recorded on the mission's status (state=Error, then -- once automatic
    rollback completes -- state=RolledBack) rather than propagated, since
    this runs as an untracked FastAPI background task.

    Args:
        mission_id: The deployment's id (from services.missions.register_mission).
        mission: The mission spec to provision. MAC addresses and GPU
            device allocations for this deployment were already resolved
            once, atomically, at registration time (see
            core/state.py:MissionStore.register_deployment) and are read
            from the mission's status here rather than recomputed --
            guaranteeing they can never disagree with what was actually
            reserved against other concurrently-active deployments.
        hosts: Full host inventory (for VM host addresses).
        deployment_cfg: OVN connection + runtime/golden storage config
            (see services/cluster_config.py).

    Returns:
        None. Progress and the final outcome are recorded on the mission's
        MissionStatus (core.state.store) for callers to poll, and mirrored
        to this process's own logger.
    """
    status = store.get(mission_id)
    resolved_macs = status.resolved_macs if status else {}
    gpu_allocations = status.gpu_allocations if status else {}

    try:
        # --- 1. Networks ---------------------------------------------------
        store.update_state(mission_id, MissionState.DEPLOYING_NETWORKS)
        api = ovn_client.connect(deployment_cfg.ovn_nb_connection)
        networking.provision_networks(api, mission_id, mission)
        networking.add_vm_ports(api, mission_id, mission, resolved_macs)
        store.log_step(mission_id, "networks", "ok", f"{len(mission.networks)} logical switches, {mission.total_nics()} ports")

        # --- 2. Storage ------------------------------------------------------
        store.update_state(mission_id, MissionState.CLONING_STORAGE)
        resolved_revisions = storage_service.provision_mission_storage(mission, deployment_cfg.runtime, deployment_cfg.golden)
        store.log_step(mission_id, "storage", "ok", f"cloned revisions: {resolved_revisions}")

        # --- 3. Define + start VMs -------------------------------------------
        store.update_state(mission_id, MissionState.DEFINING_VMS)
        host_addresses_by_vm: dict[str, str] = {}
        gpu_mdev_by_vm: dict[str, str] = {}

        for vm_name, vm in mission.vms.items():
            host_name = mission.placement[vm_name]
            host_address = hosts[host_name].address
            host_addresses_by_vm[vm_name] = host_address

            gpu_mdev_uuid = None
            if vm.has_gpu:
                gpu_mdev_uuid = gpu_allocations.get(vm_name)
                if gpu_mdev_uuid is None:
                    # Should not happen: register_mission's atomic
                    # reserve_gpu_devices call would have raised at
                    # registration time if no slice were available. This
                    # is a defensive check, not the primary allocation path.
                    raise RuntimeError(
                        f"VM '{vm_name}' requires gpu_profile={vm.gpu_profile!r} but has no "
                        f"GPU device reserved in this deployment's status -- this indicates a "
                        f"bug in registration (reserve_gpu_devices should have caught this earlier)"
                    )
                gpu_mdev_by_vm[vm_name] = gpu_mdev_uuid

            compute.define_and_start_vm(
                mission=mission,
                mission_id=mission_id,
                vm_name=vm_name,
                vm=vm,
                macs=resolved_macs[vm_name],
                host_address=host_address,
                storage_ctx=deployment_cfg.runtime,
                gpu_mdev_uuid=gpu_mdev_uuid,
                ovn_integration_bridge=deployment_cfg.ovn_integration_bridge,
                ssh_user=deployment_cfg.management_ssh_user,
            )
            store.log_step(mission_id, f"define_vm:{vm_name}", "ok", host_name)

        # --- 4. Validate ------------------------------------------------------
        store.update_state(mission_id, MissionState.VALIDATING)
        missing_networks = validation.validate_networks(api, mission_id, mission)
        report = validation.validate_mission_deployment(
            mission, host_addresses_by_vm, resolved_macs, gpu_mdev_by_vm,
            ssh_user=deployment_cfg.management_ssh_user,
        )
        store.log_step(mission_id, "validation", "ok" if (report.ok and not missing_networks) else "error", str(report.as_dict()))

        if report.ok and not missing_networks:
            store.update_state(mission_id, MissionState.RUNNING)
            log.info("deploy: mission '%s' (%s) reached Running", mission.name, mission_id)
        else:
            raise RuntimeError(
                f"post-deploy validation failed: missing_networks={missing_networks} "
                f"issues={report.as_dict()['issues']}"
            )

    except Exception as exc:  # noqa: BLE001 - top-level worker: capture and report, never crash the process
        log.error("deploy: mission '%s' (%s) failed: %s", mission.name, mission_id, exc, exc_info=True)
        store.fail(mission_id, "deploy", exc)
        _rollback_partial_deployment(mission_id, mission, hosts, deployment_cfg)


def _rollback_partial_deployment(
    mission_id: str,
    mission: MissionSpec,
    hosts: dict[str, HostSpec],
    deployment_cfg: DeploymentConfig,
) -> None:
    """
    Best-effort teardown of whatever this deployment attempt managed to
    create before it failed -- every VM, then OVN networking, then
    runtime storage -- so the operator can resubmit the same mission
    immediately afterward without hitting "already exists"/MAC-prefix/
    GPU-slice contention errors left over from the half-finished attempt.

    Every step here is already idempotent
    (compute.destroy_vm/networking.teardown_mission_networking/
    storage_service.teardown_mission_storage all silently no-op for a
    resource that was never actually created), so this is safe to call
    unconditionally regardless of which of deploy_mission's four steps
    the original failure happened at -- including step 1, where nothing
    was created yet.

    Args:
        mission_id: The deployment's id.
        mission: The mission spec that partially deployed.
        hosts: Full host inventory (for VM host addresses).
        deployment_cfg: OVN connection + runtime storage config.

    Returns:
        None. Never raises -- rollback failures are caught, logged, and
        recorded on the mission's status rather than propagated (this is
        already being called from deploy_mission's own except block).

        If every rollback step succeeds, the mission transitions to
        state=RolledBack, which frees its MAC prefix and GPU device
        allocations (see core.types.TERMINAL_FREEING_STATES) for a future
        deployment to reuse -- while `status.error` and the step log
        still show the original failure, so the history isn't lost.

        If any rollback step itself fails, the mission is deliberately
        LEFT in state=Error (not RolledBack) and its MAC prefix/GPU
        allocations stay reserved -- freeing them while real leftover
        resources might still exist on some host would be worse than
        requiring a human to investigate.
    """
    store.update_state(mission_id, MissionState.ROLLING_BACK)
    rollback_errors: list[str] = []

    for vm_name in mission.vms:
        try:
            host_name = mission.placement.get(vm_name)
            if host_name and host_name in hosts:
                compute.destroy_vm(vm_name, hosts[host_name].address, ssh_user=deployment_cfg.management_ssh_user)
        except Exception as exc:  # noqa: BLE001
            rollback_errors.append(f"destroy_vm:{vm_name}: {exc}")

    try:
        api = ovn_client.connect(deployment_cfg.ovn_nb_connection)
        networking.teardown_mission_networking(api, mission_id, mission)
    except Exception as exc:  # noqa: BLE001
        rollback_errors.append(f"networking_teardown: {exc}")

    try:
        storage_service.teardown_mission_storage(mission, deployment_cfg.runtime)
    except Exception as exc:  # noqa: BLE001
        rollback_errors.append(f"storage_teardown: {exc}")

    if rollback_errors:
        detail = "; ".join(rollback_errors)
        log.error("deploy: automatic rollback for mission '%s' (%s) had errors: %s", mission.name, mission_id, detail)
        store.log_step(mission_id, "rollback", "error", detail)
        store.update_state(mission_id, MissionState.ERROR)  # rollback incomplete -- stay Error, keep resources reserved
    else:
        log.info("deploy: automatic rollback for mission '%s' (%s) completed -- safe to resubmit", mission.name, mission_id)
        store.log_step(mission_id, "rollback", "ok", "all partial resources removed -- safe to resubmit this mission")
        store.update_state(mission_id, MissionState.ROLLED_BACK)
