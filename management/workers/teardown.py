"""
Background teardown task for DELETE /missions/{id}. Tears down VMs first
(so nothing is still attached to the OVN ports/RBD clones being removed),
then networking, then storage. Every step is idempotent (ignores "already
gone" errors) so teardown can be safely retried after a partial failure.
"""
from __future__ import annotations

from clients import ovn_client
from core.logging_setup import log
from core.state import store
from core.types import HostSpec, MissionSpec, MissionState
from services import compute, networking
from services import storage as storage_service
from services.cluster_config import DeploymentConfig


def teardown_mission(
    mission_id: str,
    mission: MissionSpec,
    hosts: dict[str, HostSpec],
    deployment_cfg: DeploymentConfig,
) -> None:
    """
    Tear down every resource belonging to one mission deployment: VMs,
    then OVN networking, then runtime storage. Never raises -- any
    exception is caught, logged, and recorded on the mission's status
    (state=Error) rather than propagated, since this runs as an untracked
    FastAPI background task.

    Args:
        mission_id: The deployment's id.
        mission: The mission spec that was deployed (from
            MissionStatus.spec -- see api/routes.py's DELETE handler).
        hosts: Full host inventory (for VM host addresses).
        deployment_cfg: OVN connection + runtime storage config.

    Returns:
        None. On success, the mission transitions to state=Destroyed,
        which also frees its reserved MAC prefix and GPU device
        allocations for future deployments (see
        core/state.py:MissionStore.active_mac_prefixes,
        active_gpu_allocations).
    """
    store.update_state(mission_id, MissionState.DESTROYING)

    try:
        for vm_name in mission.vms:
            host_name = mission.placement[vm_name]
            host_address = hosts[host_name].address
            compute.destroy_vm(vm_name, host_address, ssh_user=deployment_cfg.management_ssh_user)
            store.log_step(mission_id, f"destroy_vm:{vm_name}", "ok", host_name)

        api = ovn_client.connect(deployment_cfg.ovn_nb_connection)
        networking.teardown_mission_networking(api, mission_id, mission)
        store.log_step(mission_id, "networking_teardown", "ok", "")

        storage_service.teardown_mission_storage(mission, deployment_cfg.runtime)
        store.log_step(mission_id, "storage_teardown", "ok", "")

        store.update_state(mission_id, MissionState.DESTROYED)
        log.info("teardown: mission '%s' (%s) reached Destroyed", mission.name, mission_id)

    except Exception as exc:  # noqa: BLE001
        log.error("teardown: mission '%s' (%s) failed: %s", mission.name, mission_id, exc, exc_info=True)
        store.fail(mission_id, "teardown", exc)
