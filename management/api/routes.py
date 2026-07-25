"""
REST endpoints, per the design doc's example API table:

    POST    /missions        MissionDefinition   Create a new mission (async)
    GET     /missions/{id}   (none)              Get mission status/details
    DELETE  /missions/{id}   (none)              Delete (teardown) mission
    GET     /missions        (none)              List all missions

Plus GET /hosts and GET /health, small additions the doc's prose mentions
("Defines REST endpoints (/missions, /images, /networks, etc.)") but
doesn't spell out -- useful for a Cockpit-less sanity check of what the
service sees as its host inventory.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from api.models import (
    HealthResponse,
    HostSummary,
    MissionCreateResponse,
    MissionDefinition,
    MissionStatusResponse,
    StepLogEntryModel,
)
from services import missions as missions_service
from workers.deploy import deploy_mission
from workers.teardown import teardown_mission

router = APIRouter()


def _load_cluster_cfg() -> dict:
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "inventory" / "cluster.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _status_to_response(status) -> MissionStatusResponse:
    return MissionStatusResponse(
        mission_id=status.mission_id,
        name=status.name,
        state=status.state.value,
        steps=[StepLogEntryModel(step=s.step, status=s.status, detail=s.detail) for s in status.steps],
        error=status.error,
    )


@router.post("/missions", response_model=MissionCreateResponse, status_code=202)
async def create_mission(mission_def: MissionDefinition, background_tasks: BackgroundTasks):
    mission = mission_def.to_core()
    hosts = missions_service.load_host_inventory()

    try:
        status = missions_service.register_mission(mission, hosts)
    except missions_service.MissionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    cluster_cfg = _load_cluster_cfg()
    background_tasks.add_task(deploy_mission, status.mission_id, mission, hosts, cluster_cfg)
    return MissionCreateResponse(mission_id=status.mission_id)


@router.get("/missions", response_model=list[MissionStatusResponse])
async def list_missions():
    return [_status_to_response(s) for s in missions_service.list_missions()]


@router.get("/missions/{mission_id}", response_model=MissionStatusResponse)
async def get_mission(mission_id: str):
    status = missions_service.get_mission(mission_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"mission {mission_id} not found")
    return _status_to_response(status)


@router.delete("/missions/{mission_id}", status_code=202)
async def delete_mission(mission_id: str, background_tasks: BackgroundTasks):
    status = missions_service.get_mission(mission_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"mission {mission_id} not found")
    if status.spec is None:
        # Only reachable if a MissionStatus was created without a spec
        # (defensive branch -- create_mission always passes one via
        # register_mission).
        raise HTTPException(status_code=409, detail="mission has no stored spec to tear down")

    hosts = missions_service.load_host_inventory()
    cluster_cfg = _load_cluster_cfg()
    background_tasks.add_task(teardown_mission, mission_id, status.spec, hosts, cluster_cfg)
    return {"mission_id": mission_id, "state": "Destroying"}


@router.get("/hosts", response_model=list[HostSummary])
async def list_hosts():
    hosts = missions_service.load_host_inventory()
    return [
        HostSummary(name=h.name, cpus=h.cpus, memory_mb=h.memory_mb, gpus=h.gpus)
        for h in hosts.values()
    ]


@router.get("/health", response_model=HealthResponse)
async def health():
    hosts = missions_service.load_host_inventory()
    return HealthResponse(status="ok", hosts=len(hosts))
