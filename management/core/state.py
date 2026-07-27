"""
Mission status store: an in-memory dict (for fast reads within this
process's lifetime) backed by an optional SQLite file (for durability
across restarts). SQLite is used deliberately -- it's part of the Python
standard library (`sqlite3`), so persistence works fully offline with no
new dependency, consistent with this whole project's airgap-friendly
design. Every mutation is written through to SQLite synchronously, inside
the same lock acquisition as the in-memory update, so the two never
disagree.

The design doc explicitly scopes full HA as future work ("High-
availability of services... single-node service model" is listed as
unspecified/future work) -- this closes the specific, narrower gap of
"a restart loses all mission history", not full multi-node HA. See
services/reconciliation.py for the complementary piece: a live cluster
scan at startup that catches drift a database alone can't (e.g. a VM
deleted directly via virsh, bypassing this service entirely).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid as uuid_lib
from pathlib import Path
from typing import Callable, Optional

from .serialization import mission_status_from_jsonable, mission_status_to_jsonable
from .types import MissionSpec, MissionStatus, MissionState, TERMINAL_FREEING_STATES


class MissionStore:
    """Thread-safe, process-local store of every mission deployment's live status, optionally persisted to SQLite."""

    def __init__(self, db_path: "str | Path | None" = None) -> None:
        """
        Args:
            db_path: If given, mission state is persisted to a SQLite
                database at this path (created if it doesn't exist) and
                reloaded from it immediately, so in-flight mission state
                survives a process restart. If None (the default), this
                store is in-memory only -- appropriate for tests, and for
                any caller that doesn't need persistence.
        """
        self._lock = threading.Lock()
        self._missions: dict[str, MissionStatus] = {}
        self._conn: Optional[sqlite3.Connection] = None

        if db_path is not None:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._ensure_schema()
            self._load_from_db()

    def _ensure_schema(self) -> None:
        """Create the missions table if this is a fresh database file."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS missions (
                mission_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

    def _load_from_db(self) -> None:
        """Populate the in-memory dict from every row already in the database (called once, at construction)."""
        cursor = self._conn.execute("SELECT data_json FROM missions")
        for (data_json,) in cursor.fetchall():
            status = mission_status_from_jsonable(json.loads(data_json))
            self._missions[status.mission_id] = status

    def _persist(self, status: MissionStatus) -> None:
        """Write-through: called under self._lock, immediately after every in-memory mutation. A no-op if this store has no db_path configured."""
        if self._conn is None:
            return
        data_json = json.dumps(mission_status_to_jsonable(status))
        self._conn.execute(
            """
            INSERT INTO missions (mission_id, data_json, updated_at) VALUES (?, ?, datetime('now'))
            ON CONFLICT(mission_id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at
            """,
            (status.mission_id, data_json),
        )
        self._conn.commit()

    def _delete_persisted(self, mission_id: str) -> None:
        """Remove one mission's row from SQLite (mirrors remove()). A no-op if this store has no db_path configured."""
        if self._conn is None:
            return
        self._conn.execute("DELETE FROM missions WHERE mission_id = ?", (mission_id,))
        self._conn.commit()

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
            self._persist(status)
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
            nothing is registered (or persisted).
        """
        with self._lock:
            active_prefixes = {
                m.mac_prefix
                for m in self._missions.values()
                if m.mac_prefix and m.state not in TERMINAL_FREEING_STATES
            }
            prefix, resolved_macs = mac_resolve_fn(spec, active_prefixes)

            active_gpu_uuids = {
                uuid
                for m in self._missions.values()
                if m.state not in TERMINAL_FREEING_STATES
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
            self._persist(status)
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
        Every MAC prefix currently reserved by a deployment not yet in a
        terminal-freeing state (see core.types.TERMINAL_FREEING_STATES --
        Destroyed, or RolledBack after a failed deploy's automatic cleanup).
        Exposed for status/health reporting; register_deployment()
        computes this itself internally (under lock) rather than calling
        this method, to avoid a check-then-act race.
        """
        with self._lock:
            return {
                m.mac_prefix
                for m in self._missions.values()
                if m.mac_prefix and m.state not in TERMINAL_FREEING_STATES
            }

    def active_gpu_allocations(self) -> set[str]:
        """
        Every GPU mdev UUID currently reserved by a deployment not yet in a
        terminal-freeing state (see core.types.TERMINAL_FREEING_STATES).
        Exposed for status/health reporting; register_deployment()
        computes this itself internally (under lock) rather than calling
        this method, to avoid a check-then-act race.
        """
        with self._lock:
            return {
                uuid
                for m in self._missions.values()
                if m.state not in TERMINAL_FREEING_STATES
                for uuid in m.gpu_allocations.values()
            }

    def update_state(self, mission_id: str, state: MissionState) -> None:
        """Transition a mission deployment to a new lifecycle state."""
        with self._lock:
            status = self._missions.get(mission_id)
            if status:
                status.state = state
                self._persist(status)

    def log_step(self, mission_id: str, step: str, status: str, detail: str = "") -> None:
        """Append one entry to a mission deployment's step audit log."""
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.log(step, status, detail)
                self._persist(m)

    def fail(self, mission_id: str, step: str, error: Exception) -> None:
        """Transition a mission deployment to Error and record what failed."""
        with self._lock:
            m = self._missions.get(mission_id)
            if m:
                m.fail(step, error)
                self._persist(m)

    def adopt(self, status: MissionStatus) -> None:
        """
        Register an already-built MissionStatus wholesale, as-is --
        unlike create()/register_deployment(), this doesn't generate a new
        mission_id or resolve anything; `status.mission_id` is used
        exactly as given. Used by services/reconciliation.py to record a
        mission discovered running live on the cluster with no prior
        database record for it (see that module's docstring).

        Args:
            status: A fully-formed MissionStatus to store.

        Returns:
            None. Overwrites any existing record with the same mission_id.
        """
        with self._lock:
            self._missions[status.mission_id] = status
            self._persist(status)

    def remove(self, mission_id: str) -> None:
        """Drop a mission deployment from the store entirely (frees its MAC prefix and GPU allocations, and deletes its persisted row if any)."""
        with self._lock:
            self._missions.pop(mission_id, None)
            self._delete_persisted(mission_id)


# Process-wide singleton used by the FastAPI routes and background workers,
# mirroring the design doc's "Inventory/State: Maintains ... tracks
# provisioned missions (could be in-memory or persisted)".
#
# Persistence is an explicit opt-in via the MISSION_DB_PATH environment
# variable (see management/mission-management.service, which sets it for
# a real deployment) -- deliberately NOT defaulted to a hardcoded path,
# so merely importing this module never has an import-time side effect
# of creating a file on disk (which would be surprising in a test run, and
# would fail outright in a read-only environment). Set MISSION_DB_PATH to
# enable durability across restarts; leave it unset for the same
# in-memory-only behavior this store has always had.
import os  # noqa: E402

_db_path_env = os.environ.get("MISSION_DB_PATH")
store = MissionStore(db_path=_db_path_env if _db_path_env else None)
