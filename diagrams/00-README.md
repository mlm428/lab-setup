# Architecture diagram package

Thirteen diagrams, in [Mermaid](https://mermaid.js.org) format (`.mmd`),
covering this project end to end at both a high level (the two
deliverables individually and together) and a low level (the components,
data model, and mechanisms underneath them). Written to be read in the
order below — each builds on the last.

## Viewing these files

`.mmd` files are plain text; render them with any of:
- [mermaid.live](https://mermaid.live) — paste the file contents
- VS Code's "Markdown Preview Mermaid Support" extension, or most modern
  Markdown editors/wikis (Obsidian, GitLab, many others) — paste into a
  ` ```mermaid ` fenced code block in any `.md` file
- `mmdc` (mermaid-cli), if you want PNG/SVG exports for a slide deck:
  `mmdc -i 07-networking-ovn-isolation.mmd -o 07.svg`

## Reading order

### Part 1 — High level: the system and the two deliverables

| # | Diagram | What it shows |
|---|---|---|
| 01 | `01-system-context.mmd` | The whole system in one picture: operator, rescue node, compute cluster, management service, and the external infra (Ceph, OVN, golden image source) it all talks to. Start here. |
| 02 | `02-deliverable-a-bootstrap-lifecycle.mmd` | **Deliverable A** end to end: the rescue node's remote orchestration wrapping `bootstrap.py`'s own module sequence on each host, through to a ready host (or a clear "reboot required" / "failed" outcome). |
| 03 | `03-deliverable-b-mission-lifecycle.mmd` | **Deliverable B** end to end: a mission from `POST /missions` through registration, background deployment, running, and teardown — including the automatic-rollback path on failure. |
| 04 | `04-combined-end-to-end-lifecycle.mmd` | How A and B fit together as one continuous timeline: prerequisites → bootstrap every host (A) → install and start the management service (B) → the repeating mission deploy/observe/teardown cycle. |

### Part 2 — Low level: components and mechanisms

| # | Diagram | What it shows |
|---|---|---|
| 05 | `05-component-architecture.mmd` | The `api/ → workers/ → services/ → core/ + clients/` layering inside Deliverable B, and why `core/` has zero infrastructure dependencies (this is what let most of this project be unit-tested without a real cluster). |
| 06 | `06-mission-deployment-sequence.mmd` | A sequence diagram of one `POST /missions` call: every function call from the API layer down through atomic MAC/GPU reservation, into the background deploy workflow, down to the real `libvirt`/OVN/Ceph calls. |
| 07 | `07-networking-ovn-isolation.mmd` | **Networking deep dive.** How two concurrent deployments of the identical mission get genuinely separate OVN logical switches/routers (scoped by `mission_id`, not just the mission's name) — see `CODE_REVIEW.md` §4.1 for why this specific point mattered. |
| 08 | `08-mac-address-scheme.mmd` | **Networking deep dive, continued.** The MAC prefix (random, per-deployment) + suffix (operator-supplied, stable) composition scheme, and why it needs the isolation in #07 to actually deliver "totally network isolated" deployments. |
| 09 | `09-storage-architecture.mmd` | **Storage deep dive.** Golden (source) images vs. the runtime pool, and the three ways a golden image can be sourced (same-cluster clone, cross-cluster copy, mount copy) — and why only one of the three is a true thin clone. |
| 10 | `10-gpu-allocation-flow.mmd` | **Compute/GPU deep dive.** How a VM's requested MIG/vGPU profile gets matched to a specific, free mediated-device UUID, checked atomically against every other currently-active deployment — the mechanism behind the cross-mission GPU contention fix. |
| 11 | `11-mission-state-machine.mmd` | Every state a mission deployment can be in and what moves it between them, including the `RollingBack`/`RolledBack` states added for automatic failure recovery. |
| 12 | `12-deployment-topology.mmd` | Where each piece actually runs, physically: the rescue node's transient role, the management host, every compute host's role/GPU assignment, and the shared OVN/Ceph infrastructure — plus the offline-package flow for air-gapped operation. |
| 13 | `13-data-model.mmd` | The core dataclasses (`MissionSpec`, `VMSpec`, `HostSpec`, `MissionStatus`, etc.) and how they relate — the actual shape of the domain model everything else in `core/` operates on. |

## How this maps to the codebase

If you want to go from a diagram straight to the code:
- Diagrams 01–04 (lifecycle) → `README.md`'s "System architecture" and
  "Deployment model" sections, `bootstrap/bootstrap.py`,
  `rescue/orchestrate.py`, `management/workers/{deploy,teardown}.py`.
- Diagram 05 (components) → the `management/{core,services,clients,
  workers,api}/` directory structure itself.
- Diagram 06 (sequence) → `management/api/routes.py:create_mission`
  through `management/workers/deploy.py:deploy_mission`.
- Diagrams 07–08 (networking/MAC) → `management/core/xml_render.py`,
  `management/core/macs.py`, `management/services/networking.py`.
- Diagram 09 (storage) → `management/services/storage.py`,
  `management/clients/ceph_client.py`, `config/storage.yaml`.
- Diagram 10 (GPU) → `management/core/placement.py:reserve_gpu_devices`,
  `management/core/state.py:MissionStore.register_deployment`.
- Diagram 11 (state machine) → `management/core/types.py:MissionState`.
- Diagram 12 (topology) → `config/hosts.yaml`, `rescue/README.md`,
  `management/mission-management.service`.
- Diagram 13 (data model) → `management/core/types.py`.
