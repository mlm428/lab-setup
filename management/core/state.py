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
from typing import Callable, Optional

from .types import MissionSpec, MissionStatus, MissionState


class MissionStore:
    """Thread-safe, process-local store of every mission deployment's live status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._missions: dict[str, MissionStatus] = {}

    def create(self, name: str, spec: "MissionSpec | None" = None) -> MissionStatus:
        """
        Register a new mission deployment with no MAC prefix assigned yet.

        Prefer register_deployment() for real deployments (it assigns the
        MAC prefix and GPU allocations atomically, race-free against
        concurrent submissions); this simpler method exists for tests and
        for any caller that doesn't need MAC/GPU resolution at all.

        Args:
            name: Mission name (from its MissionSpec).
            spec: The originating MissionSpec, retained for later teardown.

        Returns:
            The newly created MissionStatus (state=Pending).
        """
        mission_id = str(uuid_lib.uuid4())
        status = MissionStatus(mission_id=mission_id, name=name, spec=spec)
        with self._lock:
            self._missions[mission_id] = status
        return status

    def register_deployment(
        self,
        name: str,
        spec: MissionSpec,
        hosts: dict,
        mac_resolve_fn: Callable[[MissionSpec, set[str]], tuple[str, dict[str, list[str]]]],
        gpu_resolve_fn: Callable[[MissionSpec, dict, set[str]], dict[str, str]],
    ) -> MissionStatus:
        """
        Atomically resolve this deployment's MAC prefix AND its GPU
        device allocations -- both checked against every other
        currently-active deployment's reservations -- then register the
        new mission. Everything happens under one lock acquisition, so
        two concurrent submissions can never be assigned colliding MAC
        prefixes or colliding physical GPU slices (a check-then-act race
        would be possible if "what's already reserved" and "reserve this
        new one" were separate lock acquisitions for either resource).

        Args:
            name: Mission name (from its MissionSpec).
            spec: The MissionSpec being deployed.
            hosts: Full host inventory (needed by gpu_resolve_fn to look
                up each host's available GPU device slices).
            mac_resolve_fn: Called as `mac_resolve_fn(spec,
                active_prefixes)`, expected to return (prefix, {vm_name:
                [mac, ...]}) -- normally core.macs.resolve_mission_macs.
            gpu_resolve_fn: Called as `gpu_resolve_fn(spec, hosts,
                active_gpu_uuids)`, expected to return {vm_name:
                mdev_uuid} -- normally core.placement.reserve_gpu_devices.
                Both are injected callables (rather than importing
                core.macs/core.placement directly here) purely so tests
                can substitute deterministic/failing resolvers.

        Returns:
            The newly created MissionStatus, with mac_prefix,
            resolved_macs, and gpu_allocations already populated.

        Raises:
            Whatever `mac_resolve_fn` or `gpu_resolve_fn` raises (e.g.
            ValueError for a duplicate mac_suffix, RuntimeError if no
            free prefix or GPU slice could be found) -- in that case
            nothing is registered.
        """
        with self._lock:
            active_prefixes = {
                m.mac_prefix
                for m in self._missions.values()
                if m.mac_prefix and m.state != MissionState.DESTROYED
            }
            prefix, resolved_macs = mac_resolve_fn(spec, active_prefixes)

            active_gpu_uuids = {
                uuid
                for m in self._missions.values()
                if m.state != MissionState.DESTROYED
                for uuid in m.gpu_allocations.values()
            }
            gpu_allocations = gpu_resolve_fn(spec, hosts, active_gpu_uuids)

            mission_id = str(uuid_lib.uuid4())
            status = MissionStatus(
                mission_id=mission_id, name=name, spec=spec,
                mac_prefix=prefix, resolved_macs=resolved_macs,
                gpu_allocations=gpu_allocations,
            )
            self._missions[mission_id] = status
            return status

    def get(self, mission_id: str) -> Optional[MissionStatus]:
        """Look up a mission deployment by id, or None if it doesn't exist."""
        with self._lock:
            return self._missions.get(mission_id)

    def list(self) -> list[MissionStatus]:
        """Return every tracked mission deployment (any state)."""
        with self._lock:
            return list(self._missions.values())

    def active_mac_prefixes(self) -> set[str]:
        """
        Every MAC prefix currently reserved by a non-Destroyed deployment.
        Exposed for status/health reporting; register_deployment()
        computes this itself internally (under lock) rather than calling
        this method, to avoid a check-then-act race.
        """
        with self._lock:
            return {
                m.mac_prefix
                for m in self._missions.values()
                if m.mac_prefix and m.state != MissionState.DESTROYED
            }

    def active_gpu_allocations(self) -> set[str]:
        """
        Every GPU mdev UUID currently reserved by a non-Destroyed
        deployment. Exposed for status/health reporting;
        register_deployment() computes this itself internally (under
        lock) rather than calling this method, to avoid a check-then-act race.
        """
        with self._lock:
            return {
                uuid
                for m in self._missions.values()
                if m.state != MissionState.DESTROYED
                for uuid in m.gpu_allocations.values()
            }

    def update_state(self, mission_id: str, state: MissionState) -> None:
        """Transition a mission deployment to a new lifecycle state."""
        with self._lock:
            status = self._missions.get(mission_id)
            if status:
                status.state = state

    def log_step(self, mission_id: str, step: str, status: str, detail: str = "") -> None:
        """Append one entry to a mission deployment's step audit log."""
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.log(step, status, detail)

    def fail(self, mission_id: str, step: str, error: Exception) -> None:
        """Transition a mission deployment to Error and record what failed."""
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.fail(step, error)

    def remove(self, mission_id: str) -> None:
        """Drop a mission deployment from the store entirely (frees its MAC prefix)."""
        with self._lock:
            self._missions.pop(mission_id, None)


# Process-wide singleton used by the FastAPI routes and background workers,
# mirroring the design doc's "Inventory/State: Maintains ... tracks
# provisioned missions (could be in-memory or persisted)".
store = MissionStore()
