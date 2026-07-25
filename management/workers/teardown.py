"""
Background teardown task for DELETE /missions/{id}. Tears down VMs first
(so nothing is still attached to the OVN ports/RBD clones being removed),
then networking, then storage. Every step is idempotent (ignores "already
gone" errors) so teardown can be safely retried after a partial failure.
"""
from __future__ import annotations

from core.state import store
from core.types import MissionSpec, MissionState
from clients import ovn_client
from services import compute, networking, storage as storage_service
from workers.deploy import _storage_context_from_cluster_cfg


def teardown_mission(
    mission_id: str,
    mission: MissionSpec,
    hosts: dict,
    cluster_cfg: dict,
) -> None:
    store.update_state(mission_id, MissionState.DESTROYING)
    storage_ctx = _storage_context_from_cluster_cfg(cluster_cfg)

    try:
        for vm_name, vm in mission.vms.items():
            host_name = mission.placement[vm_name]
            host_address = hosts[host_name].address
            compute.destroy_vm(vm_name, host_address)
            store.log_step(mission_id, f"destroy_vm:{vm_name}", "ok", host_name)

        api = ovn_client.connect(cluster_cfg["ovn"]["nb_connection"])
        networking.teardown_mission_networking(api, mission)
        store.log_step(mission_id, "networking_teardown", "ok", "")

        storage_service.teardown_mission_storage(mission, storage_ctx)
        store.log_step(mission_id, "storage_teardown", "ok", "")

        store.update_state(mission_id, MissionState.DESTROYED)

    except Exception as exc:  # noqa: BLE001
        store.fail(mission_id, "teardown", exc)
