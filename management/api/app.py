"""
FastAPI application entry point (Deliverable B). Run with:

    uvicorn api.app:app --host 0.0.0.0 --port 8000

from the management/ directory, with requirements.txt installed in the
service's venv (fastapi/uvicorn/pydantic/jinja2/pyyaml/ovsdbapp) and
python3-libvirt/python3-rados/python3-rbd available on PATH for the
system Python those bindings need (see requirements.txt's header note on
why those three are dnf, not pip, packages).
"""
from __future__ import annotations

from fastapi import FastAPI

from api.routes import router

app = FastAPI(
    title="Mission Compute Cluster Management API",
    description=(
        "Deliverable B: provisions VMware-replacement 'missions' (VM sets "
        "with NICs, storage, and placement) on a RHEL/KVM/libvirt/OVS-OVN/"
        "Ceph cluster."
    ),
    version="0.1.0",
)

app.include_router(router)


@app.get("/")
async def root():
    return {"service": "mission-compute-cluster-management", "docs": "/docs"}
