"""
REST endpoints for the mission management service.

    POST    /missions              MissionDefinition   Create + deploy a new mission (async, 202)
    GET     /missions/{id}         (none)               Get one mission deployment's status/details
    DELETE  /missions/{id}         (none)               Tear down one mission deployment (async, 202)
    GET     /missions              (none)               List every tracked mission deployment
    GET     /hosts                 (none)               Host inventory summary (capacity + GPU profiles)
    GET     /health                (none)               Lightweight liveness check (service is up, inventory loads)
    GET     /cluster/health        (none)               Full infrastructure scan (see below)

GET /cluster/health vs GET /health: /health only confirms the management
service itself is up and can load its config -- it does not touch any
compute host, OVN, or Ceph. /cluster/health is a full, live scan: it opens
a libvirt connection to every host in the inventory, checks OVN and Ceph
reachability, and rolls up every tracked mission deployment by state, per
operator request for "a scan/search through the cluster to validate the
infrastructure ... simple rollup for a good pass/fail ... but then also
provide more detailed status."

API VERSIONING: API_VERSION below is a simple string bumped whenever a
request/response schema or an endpoint's behavior changes in a
backward-incompatible way (e.g. the interfaces: schema change, or the
gpu: bool -> gpu: {profile} change this project went through). It's
surfaced in every response via the `X-API-Version` header (see app.py)
and in GET /health, so a client can always tell which contract it's
talking to. This is a lightweight, in-process convention -- see
README.md's "API and versioning" section for the full history and for
why this project didn't adopt URL path versioning (e.g. /v1/missions)
given its current single-consumer, prototype scope.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from api.models import (
    ClusterHealthResponse,
    HealthResponse,
    HostHealth,
    HostSummary,
    MissionCreateResponse,
    MissionDefinition,
    MissionStatusResponse,
    StepLogEntryModel,
)
from services import missions as missions_service
from services.cluster_config import load_deployment_config
from services.cluster_health import scan_cluster
from workers.deploy import deploy_mission
from workers.teardown import teardown_mission

API_VERSION = "1.1.0"

router = APIRouter()


def _status_to_response(status) -> MissionStatusResponse:
    """Convert a core.types.MissionStatus into its API response model."""
    return MissionStatusResponse(
        mission_id=status.mission_id,
        name=status.name,
        state=status.state.value,
        mac_prefix=status.mac_prefix,
        steps=[StepLogEntryModel(step=s.step, status=s.status, detail=s.detail) for s in status.steps],
        error=status.error,
    )


@router.post("/missions", response_model=MissionCreateResponse, status_code=202)
async def create_mission(mission_def: MissionDefinition, background_tasks: BackgroundTasks):
    """
    Register and asynchronously deploy a new mission. Returns 202 with the
    new deployment's id and its randomly-assigned MAC prefix as soon as
    placement validation, MAC resolution, and GPU device reservation all
    succeed; actual provisioning (networks/storage/VMs/validation)
    continues in the background -- poll GET /missions/{id} for progress.

    Error responses: 422 if the mission is malformed or its placement
    doesn't fit host capacity; 409 if the mission is well-formed but the
    cluster's currently-free MAC-prefix space or GPU device slices are
    already claimed by other active deployments (retryable once
    something else finishes or is torn down).
    """
    mission = mission_def.to_core()
    hosts = missions_service.load_host_inventory()

    try:
        status = missions_service.register_mission(mission, hosts)
    except missions_service.MissionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # e.g. a duplicate mac_suffix across VMs -- see core/macs.py
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Resource contention against OTHER currently-active deployments
        # (every MAC prefix slot exhausted, or every matching GPU device
        # already reserved elsewhere -- see
        # core/placement.py:reserve_gpu_devices and
        # core/macs.py:random_prefix). Distinct from the two cases above:
        # this mission's request is well-formed and would fit in
        # isolation, it's just that the cluster's free capacity is
        # currently claimed by something else -- 409 Conflict, not 422.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    deployment_cfg = load_deployment_config()
    background_tasks.add_task(deploy_mission, status.mission_id, mission, hosts, deployment_cfg)
    return MissionCreateResponse(mission_id=status.mission_id, mac_prefix=status.mac_prefix)


@router.get("/missions", response_model=list[MissionStatusResponse])
async def list_missions():
    """List every tracked mission deployment, regardless of state."""
    return [_status_to_response(s) for s in missions_service.list_missions()]


@router.get("/missions/{mission_id}", response_model=MissionStatusResponse)
async def get_mission(mission_id: str):
    """Get one mission deployment's current status, step log, and (if failed) error."""
    status = missions_service.get_mission(mission_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"mission {mission_id} not found")
    return _status_to_response(status)


@router.delete("/missions/{mission_id}", status_code=202)
async def delete_mission(mission_id: str, background_tasks: BackgroundTasks):
    """
    Asynchronously tear down one mission deployment (VMs, then OVN
    networking, then runtime storage). Uses the MissionSpec retained on
    the deployment's status record from its original POST -- no request
    body needed.
    """
    status = missions_service.get_mission(mission_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"mission {mission_id} not found")
    if status.spec is None:
        raise HTTPException(status_code=409, detail="mission has no stored spec to tear down")

    hosts = missions_service.load_host_inventory()
    deployment_cfg = load_deployment_config()
    background_tasks.add_task(teardown_mission, mission_id, status.spec, hosts, deployment_cfg)
    return {"mission_id": mission_id, "state": "Destroying"}


@router.get("/hosts", response_model=list[HostSummary])
async def list_hosts():
    """List the cluster's host inventory: capacity and available GPU profile slice counts."""
    hosts = missions_service.load_host_inventory()
    return [
        HostSummary(name=h.name, cpus=h.cpus, memory_mb=h.memory_mb, gpu_profiles=h.available_profiles())
        for h in hosts.values()
    ]


@router.get("/health", response_model=HealthResponse)
async def health():
    """Lightweight liveness check: the service is up and its host inventory config loads. Does not touch any host, OVN, or Ceph -- see GET /cluster/health for that."""
    hosts = missions_service.load_host_inventory()
    return HealthResponse(status="ok", hosts=len(hosts))


@router.get("/cluster/health", response_model=ClusterHealthResponse)
async def cluster_health():
    """
    Full, live infrastructure scan: opens a libvirt connection to every
    host in the inventory, checks OVN and Ceph reachability, and rolls up
    every tracked mission deployment by state. See this module's
    docstring for how this differs from GET /health.
    """
    hosts = missions_service.load_host_inventory()
    deployment_cfg = load_deployment_config()
    ceph_conf_path = "/etc/ceph/ceph.conf" if deployment_cfg.runtime.backend == "ceph_rbd" else None

    result = scan_cluster(hosts, deployment_cfg.ovn_nb_connection, ceph_conf_path)
    return ClusterHealthResponse(
        ok=result["ok"],
        ovn_reachable=result["ovn_reachable"],
        ceph_reachable=result["ceph_reachable"],
        hosts=[HostHealth(**h) for h in result["hosts"]],
        missions_total=result["missions_total"],
        missions_by_state=result["missions_by_state"],
        detail=result["detail"],
    )
