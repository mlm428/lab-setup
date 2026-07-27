# Code Review: Linux-Native Virtualized Compute Cluster Prototype

**Scope:** full repository (`bootstrap/`, `management/`, `rescue/`, `config/`, `tools/`) —
approximately 9,000 lines of Python across 60+ modules, plus YAML config,
Jinja2 templates, and shell scripts.
**Method:** manual line-by-line reading of every module (not just the test
suite), cross-referencing config against code, and live execution of
representative scenarios to confirm or refute suspected issues before
recording them below. Three issues found during this pass were fixed in
place (see "Findings — Fixed During This Review"); the test suite (240
tests) was extended and re-verified after each fix.
**Not in scope:** anything that requires real infrastructure to observe
(a live OVN/Ceph/libvirt cluster) — see "What This Review Could and
Couldn't Verify."

---

## 1. Executive summary

The system is **logically sound and internally consistent** as of this
review, with one significant defect found and corrected (OVN network
isolation between same-name mission deployments — see §4.1). No other
defect found rises to that level of severity; the rest are either
genuine gaps that need an explicit decision before real deployment (§4.2)
or are cosmetic/completeness items (§4.3).

**Is it ready for prototype deployment?** Yes, *conditional on* the
infrastructure prerequisites in §6 being satisfied and the two
un-resolved items in §4.2 (Ceph client identity, external network uplink)
being explicitly decided one way or the other before go-live — not
because they're likely to cause a *silent* failure, but because both are
the kind of thing that fails loudly and confusingly on day one if nobody
has thought about them yet.

**Test coverage:** 240 tests across three suites (211 management, 15
bootstrap, 14 rescue), all passing at the time of this review. Everything
in `core/` and `services/` that doesn't require real
libvirt/OVN/Ceph/fastapi is exercised directly; everything that does is
exercised with those clients mocked at the boundary. See §7 for exactly
what that does and doesn't prove.

---

## 2. Architecture, as-built

```
config/                 single source of truth (hosts, cluster, storage, networks)
    │
    ├── bootstrap/       Deliverable A — one-time host setup (idempotent, re-runnable)
    │     bootstrap.py orchestrates modules/{packages,libvirt_checks,networking,storage,gpu,cockpit,validation}
    │
    ├── rescue/          remote 1-to-many orchestration OF bootstrap/, from an external node
    │     orchestrate.py rsyncs + SSHes into each host, centralizes logs
    │
    └── management/      Deliverable B — persistent mission provisioning service
          api/           FastAPI routes + Pydantic schemas (thin; written to spec, not run here)
          core/          pure domain logic: types, macs, placement, xml_render, state, serialization
          clients/       guarded thin wrappers: libvirt, ovsdbapp (OVN), rados/rbd (Ceph), nmcli
          services/      orchestration glue between core/ and clients/
          workers/       the actual deploy/teardown background-task workflows
          mission_defs/  mission YAML files + schema docs
```

The most important structural decision in this codebase is the
**core/services/clients split**: `core/` contains every non-trivial
algorithm (MAC resolution, GPU-profile placement, XML rendering, the
mission state machine) as plain dataclasses and pure functions with zero
dependency on fastapi, pydantic, or any infrastructure client library.
`clients/` wraps the actual infrastructure calls behind guarded imports
(`try: import libvirt / except ImportError: libvirt = None`) so the
whole codebase imports cleanly with or without those packages installed.
`services/` is the (thin) glue between the two. This is why 211 tests
could be written and run in a sandbox with no real hypervisor, OVN
deployment, or Ceph cluster available — and it's the right shape for a
codebase whose hardest logic (MAC/GPU cross-deployment contention) needs
to be provably correct independent of any particular infrastructure
backend.

---

## 3. Key assumptions (consolidated)

These are scattered across module docstrings and the README; this section
pulls them into one place as requested.

### Architectural / deployment model
- A **mission** (a named YAML spec) and a **deployment** (one specific
  provisioning run of that spec, with its own `mission_id`) are distinct.
  The same mission name can be deployed more than once concurrently; only
  `mission_id` is guaranteed unique. (This distinction is now correctly
  enforced everywhere after the fix in §4.1 — it was not, before.)
- Bootstrap (Deliverable A) and the management service (Deliverable B)
  have **separate lifecycles**: bootstrap runs once per host (from an
  external rescue node, per operator requirement) and is safely
  re-runnable; the management service runs continuously afterward,
  against already-bootstrapped hosts, and is a completely separate
  process/deployment concern (own systemd unit, own persistence).
- `config/` is read **fresh on every operation** (mission registration,
  bootstrap run, health scan) — there is no caching layer. Editing
  `config/hosts.yaml` takes effect on the *next* API call, not
  retroactively for missions already running.
- Placement (which host each VM lands on) is **static and pre-computed**
  — either by hand or via `tools/generate_mission.py`'s reference greedy
  bin-packer. There is no dynamic scheduler or rebalancing at runtime;
  the design doc scopes this as intentional for a prototype.
- A mission cannot be **mutated in place**. Changing a running mission's
  VM specs requires tearing it down and redeploying, not a partial update.

### Networking
- GPU passthrough is **mediated-device (mdev) / profile-based** (NVIDIA
  MIG for H100, vGPU for L4), not raw PCI passthrough. MIG/vGPU slice
  *creation* is assumed to be a manual, one-time, driver-specific
  operator step; this automation only *discovers* slices that already
  exist (`bootstrap/scripts/enumerate_mdev_gpus.sh`).
- The OVN Northbound DB connection is **plaintext TCP**
  (`tcp:host:6641`), not TLS/SSL. This assumes the management network
  itself is trusted/isolated — reasonable for a prototype, worth
  revisiting for production (OVN does support `ssl:` connections).
- Every OVN logical switch/router/port name is now scoped by the unique
  `mission_id` (fixed this review — see §4.1), so concurrent deployments
  of the same mission are genuinely isolated at the network layer, not
  just distinguishable by name.

### Storage
- Golden (source) images and runtime (per-VM clone) disks are
  **deliberately separate stores** — possibly different pools, different
  Ceph clusters, or a plain filesystem mount for golden. A true
  space-efficient copy-on-write clone is only possible when both share a
  cluster (`ceph_rbd_local`); the other two sourcing modes
  (`ceph_rbd_remote`, `mount`) are documented, deliberate full copies.
- Ceph cephx keyring files are assumed to already be correctly placed
  under `/etc/ceph/` on every host **before** `bootstrap.py` runs — this
  automation does not distribute or manage keyring material.

### Management service
- Runs as a **single systemd unit on one host** — no HA, no clustering,
  matching the design doc's explicit scoping of HA as future work.
- Mission state persistence (SQLite, opt-in via `MISSION_DB_PATH`)
  assumes **exactly one process** ever writes to that database file at a
  time. SQLite is not a multi-writer database; this could not be extended
  to a clustered management service without switching to a real
  client-server database first.
- Startup reconciliation (comparing the database against live libvirt
  domains) can only attribute a running VM to a mission if that VM's
  domain XML carries this system's own `<metadata>` block — a VM created
  by an older version of this project (before that block existed), or by
  any other tooling, is invisible to reconciliation and simply ignored.

### Air-gap / offline operation
- `dnf install` (bootstrap's package step) needs no code changes to work
  offline — it uses whatever repo is configured, online or off. The gap
  is tooling to *get packages there* (addressed by
  `fetch_offline_packages.sh` / `setup_local_repo.sh` for **RPM**
  packages specifically).
- DNS resolution for the `*.cluster.local` hostnames used throughout
  `config/hosts.yaml`, and NTP time synchronization across all hosts, are
  both assumed to already exist — neither is automated by this project,
  and both matter more than usual here (cephx auth tickets and OVN/
  libvirt operations are timestamp-sensitive).

---

## 4. Findings

### 4.1 Fixed during this review

**OVN logical network objects were named by `mission.name` alone, not by
the unique `mission_id`.** Two concurrent deployments of the *same*
mission definition (identical `mission:` name in the YAML — exactly the
scenario the per-deployment MAC-prefix scheme was built for) would
therefore request the *same* OVN logical switch/router/port names. Since
every `ls_add`/`lr_add`/`lsp_add` call uses `may_exist=True` for
idempotency, the second deployment's calls would silently no-op and
reuse the first deployment's actual OVN objects — putting both
deployments' VMs on the same logical broadcast domain, not in "its own
segmented network" as required. The MAC-prefix randomization would still
prevent address collisions and enable substring matching, but genuine
network isolation between the two deployments would not exist.

Verified live before and after the fix (see conversation history for the
exact before/after switch names). Fixed by threading `mission_id` through
`core/xml_render.py`'s `switch_name`/`router_name`/`router_port_name`/
`port_name`, `services/networking.py`'s three functions, and
`services/validation.py`'s `validate_networks` — all now take
`mission_id` explicitly rather than deriving uniqueness from
`mission.name` alone. Regression tests added at both the naming-function
level (`test_xml_render.py`) and the service level
(`test_provision.py::test_two_deployments_of_same_mission_get_distinct_switches`).

**The management service assumed direct root SSH to every compute host**
(`qemu+ssh://root@{host}/system`, hardcoded), which conflicts with this
project's own STIG guidance elsewhere (`rescue/README.md` explicitly
notes STIG profiles commonly set `PermitRootLogin no`). Fixed by making
the SSH user configurable (`config/hosts.yaml`'s new
`management_ssh_user`, threaded through `clients/libvirt_client.py`,
`services/compute.py`, `services/validation.py`,
`services/cluster_health.py`, `services/reconciliation.py`, both
workers, and the API layer), defaulting to a named service account
(`mission-mgmt`) rather than root. This account needs SSH key trust from
the management host plus membership in the `libvirt` group on every
compute host (libvirtd's default polkit rules grant that group full
local socket access) — no `sudo` required, unlike the bootstrap-time
rescue-node account.

**`GET /missions/{id}` didn't expose `resolved_macs` or `gpu_allocations`,
and `GET /hosts` showed total declared GPU capacity rather than
currently-available capacity** (it never subtracted what other active
deployments already hold). Both fixed: `MissionStatusResponse` now
includes both fields; `HostSpec.available_profiles()` gained an
`exclude_uuids` parameter, and `GET /hosts` now passes the cluster's
currently-active GPU allocations to it.

**`bootstrap.py` didn't check `ensure_iommu_kernel_args`'s return value**,
so a host that just had IOMMU kernel args added (requiring a reboot
before they take effect) would fall through to final validation and fail
with a generic "host failed final validation" rather than a clear
"reboot required" message. Fixed with an explicit, distinct log message
and audit record when a reboot is needed.

### 4.2 Open — need an explicit decision before real deployment

**Ceph client identity is not threaded through to the actual connection.**
`config/storage.yaml` records a `client_id` per pool (e.g. `libvirt`,
`golden-reader`), and that value *is* correctly used for the VM's own
disk auth in the rendered libvirt XML (`<auth username='...'>`). But
`clients/ceph_client.py`'s actual librados connections
(`rados.Rados(conffile=conf_path)`) and the CLI-based paths (`rbd
--conf ...`, `qemu-img convert ... :conf=...`) never pass an explicit
`name=`/`--id` for which cephx identity to authenticate as — they
silently fall back to whatever `ceph.conf`'s own defaults resolve to
(typically `client.admin`, if nothing else is configured). This isn't
necessarily *wrong* — many real Ceph deployments handle this correctly
via a `[client.NAME]` stanza in `ceph.conf` itself — but it means the
`client_id`/`keyring_path` fields in `storage.yaml` are currently more
documentation than enforced configuration. **Recommendation:** either (a)
confirm your `ceph.conf` files are set up so the default identity has
exactly the intended, minimal permissions for each connection's purpose,
or (b) thread `client_id` through explicitly (a contained change to
`cluster_connection()` and its callers) before relying on
least-privilege Ceph access boundaries between, say, the golden-image
reader and the runtime pool writer.

**`br-ex` (the external/provider OVS bridge) is created by
`bootstrap.py`, but nothing ever attaches a physical NIC to it.**
`bootstrap/modules/networking.py:attach_physical_to_external_bridge`
exists and is correctly implemented, but it's never called from
`bootstrap.py`'s orchestration, and there's no config field (e.g. a
per-host `uplink_interface: eth1`) to drive it even if it were. This
means external/provider network connectivity requires a manual step
after bootstrap on every host that needs it. **Recommendation:** add an
optional `uplink_interface` field to each host's `config/hosts.yaml`
entry, and call this function from `bootstrap.py` when it's set.

### 4.3 Minor / completeness

- **Unused functions**, present for design-doc parity or as building
  blocks for features not yet wired in: `clients/nm_client.py` (entire
  module — host-network changes at *deploy* time aren't currently a
  thing any mission does), `clients/ovn_client.py:count_ports_on_switch`,
  `bootstrap/modules/util.py:command_exists`,
  `bootstrap/modules/libvirt_checks.py:verify_virsh_responsive`. None of
  these are harmful; they're either genuinely not needed yet or represent
  incomplete wiring. Worth a cleanup pass or explicit "not yet used, kept
  for X" comments so a future reader doesn't wonder if they're dead code
  by mistake (some already have that context in their docstrings; not all).
- `config/hosts.yaml`'s per-host `role` and `numa_nodes` fields, and the
  entirety of `config/networks.yaml`'s network catalog, are **not read by
  any code path** — they're operator/documentation reference only. This
  is fine, but worth being explicit about so nobody assumes editing
  `networks.yaml` constrains what a mission's own `networks:` section can
  contain (it doesn't — each mission defines its networks independently).
- **`DELETE /missions/{id}` doesn't check whether the mission is already
  in a terminal state** before re-triggering teardown. This is safe (every
  teardown step is idempotent) but slightly wasteful, and could produce
  confusing duplicate log/step entries if called twice in quick
  succession, or genuinely race against an in-flight automatic rollback
  for the same mission_id (both are background tasks with no mutual
  exclusion beyond what the idempotent underlying calls provide).
- A mission deleted the instant after `POST /missions` returns (before
  its background deploy task has actually started) could see its
  `deploy_mission` and `teardown_mission` background tasks **run
  concurrently** with no defined ordering between them. Low real-world
  likelihood, not exercised by the test suite, and not fixed — flagged
  here rather than silently left unmentioned.

---

## 5. What this review could and couldn't verify

**Directly verified, live, during this review** (not just by reading
code): the OVN naming collision bug and its fix, the two-deployment MAC
isolation behavior, cross-mission GPU contention correctly producing
`409` instead of silent double-allocation, mission state surviving a
simulated process restart via SQLite, and reconciliation correctly
attributing a live VM back to its mission via embedded metadata.

**Could not verify** (no real infrastructure in this environment):
anything requiring an actual hypervisor, OVN Northbound DB, or Ceph
cluster — i.e., whether `clients/libvirt_client.py`,
`clients/ovn_client.py`, and `clients/ceph_client.py` work correctly
against real infrastructure, as opposed to being logically consistent
with what those libraries' documented APIs expect. `ovn_client.py`'s
`connect()` in particular follows the standard `ovsdbapp` IDL-connection
pattern, but exact constructor signatures have shifted across `ovsdbapp`
releases — validate against the pinned version in `requirements.txt`
first. Also not verified: MIG/vGPU slice behavior on real NVIDIA
hardware, PXE boot, live migration, and actual STIG compliance scoring.

---

## 6. Readiness checklist for prototype deployment

Given the fixes in §4.1 and the assumptions in §3, this system is ready
to deploy against real infrastructure once the following are true —
none of these are code changes, they're environment prerequisites:

1. Every compute host is bootstrapped (via `rescue/orchestrate.py` or
   locally) and passes `bootstrap.py --check-only`.
2. A dedicated `mission-mgmt` (or whatever you name it) service account
   exists on every compute host, is a member of the `libvirt` group, and
   trusts an SSH key held by whichever host will run the management
   service — matching whatever you set `config/hosts.yaml`'s
   `management_ssh_user` to.
3. Ceph cephx keyrings are correctly placed and, per §4.2, you've
   confirmed the connecting identity actually has the permissions you
   intend for each pool.
4. OVN Northbound/Southbound DBs are reachable at the addresses in
   `config/hosts.yaml`'s `ovn_central`.
5. MIG/vGPU slices are already created on any host running GPU missions,
   and their UUIDs are recorded in `config/hosts.yaml` (see
   `bootstrap/scripts/enumerate_mdev_gpus.sh`).
6. `config/storage.yaml`'s golden image catalog matches images that
   actually exist at the configured source.
7. DNS resolution and NTP are working across every host referenced by
   name in `config/hosts.yaml`.
8. If any host needs external/provider network connectivity, the
   physical uplink NIC is attached to `br-ex` manually (see §4.2).

---

## 7. Recommendations for air-gapped deployment specifically

Beyond the general recommendations above, these are specific to running
this fully disconnected from the internet:

1. **The offline-package tooling only covers RPM/dnf, not the management
   service's Python dependencies.** `bootstrap/scripts/
   fetch_offline_packages.sh` + `setup_local_repo.sh` solve this for
   `bootstrap.py`'s packages, but `management/requirements.txt`
   (fastapi, uvicorn, pydantic, jinja2, pyyaml, ovsdbapp) has no
   equivalent — you'd currently need to `pip download -r
   requirements.txt -d ./offline-wheels` on a connected machine matching
   your target Python version/platform yourself, then `pip install
   --no-index --find-links ./offline-wheels -r requirements.txt` on the
   air-gapped side. This is the single most likely thing to trip someone
   up on day one; worth a companion script mirroring the two RPM-side
   scripts.
2. **Internal PKI, if you want TLS anywhere** (the management API itself,
   OVN's NB/SB connections, Cockpit past its self-signed default) needs
   its own offline CA — nothing in this project currently sets one up or
   assumes one exists.
3. **Time synchronization** matters more in an air-gapped environment
   (no default NTP pool reachable) — an internal NTP server (or `chrony`
   pointed at your own PTP/GPS source) needs to be part of your
   environment's baseline before Ceph/cephx and OVN will behave
   predictably; consider adding a bootstrap module for this if it isn't
   already handled by your STIG image baseline.
4. **Version control for `config/` and `mission_defs/`** isn't currently
   set up (no `.git` in this delivered tree) — in a disconnected
   environment without a cloud audit trail, having local git history for
   exactly what changed in your cluster/host/storage config and mission
   definitions, and when, is worth setting up explicitly as an early
   operational step, independent of this codebase.
5. **The offline package/repo scripts and `rescue/orchestrate.py` are
   the least-exercised parts of this codebase against real conditions**
   (per §5) — budget real time to dry-run the *entire* rescue-node →
   airgapped-host → package-install → bootstrap.py chain end-to-end
   against your actual hardware before treating it as routine, since
   this is the one workflow with the most moving parts unique to your
   deployment model (rather than the more conventionally-shaped
   mission-provisioning API, which is thoroughly unit-tested).

---

## 8. Future work (beyond the air-gap-specific items above)

- Multi-node HA for the management service (explicitly out of scope for
  this prototype, per the design doc) — would require replacing SQLite
  with a real client-server database and adding leader election.
- A real scheduler for placement (currently static/pre-computed) —
  useful once mission count/churn makes hand-placement impractical.
- OVN NB/SB connections over TLS instead of plaintext TCP.
- Thread Ceph client identity through explicitly (§4.2) rather than
  relying on `ceph.conf` defaults.
- Wire up `attach_physical_to_external_bridge` with a config-driven
  uplink interface (§4.2).
- Address the minor concurrency gaps in §4.3 (mutual exclusion between
  concurrent deploy/teardown background tasks for the same mission_id)
  if operational experience shows they matter in practice.
