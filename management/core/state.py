"""
In-memory mission status store. The design doc explicitly scopes this as
single-node for the prototype ("High-availability of services... single-
node service model" is listed as unspecified/future work), so a
process-local dict guarded by a lock is sufficient here; swapping in Redis
or a database later only touches this file.
"""
from __future__ import annotations

import threading
import uuid as uuid_lib
from typing import Optional

from .types import MissionSpec, MissionStatus, MissionState


class MissionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._missions: dict[str, MissionStatus] = {}

    def create(self, name: str, spec: "MissionSpec | None" = None) -> MissionStatus:
        mission_id = str(uuid_lib.uuid4())
        status = MissionStatus(mission_id=mission_id, name=name, spec=spec)
        with self._lock:
            self._missions[mission_id] = status
        return status

    def get(self, mission_id: str) -> Optional[MissionStatus]:
        with self._lock:
            return self._missions.get(mission_id)

    def list(self) -> list[MissionStatus]:
        with self._lock:
            return list(self._missions.values())

    def update_state(self, mission_id: str, state: MissionState) -> None:
        with self._lock:
            status = self._missions.get(mission_id)
            if status:
                status.state = state

    def log_step(self, mission_id: str, step: str, status: str, detail: str = "") -> None:
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.log(step, status, detail)

    def fail(self, mission_id: str, step: str, error: Exception) -> None:
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.fail(step, error)

    def remove(self, mission_id: str) -> None:
        with self._lock:
            self._missions.pop(mission_id, None)


# Process-wide singleton used by the FastAPI routes and background workers,
# mirroring the design doc's "Inventory/State: Maintains ... tracks
# provisioned missions (could be in-memory or persisted)".
store = MissionStore()
