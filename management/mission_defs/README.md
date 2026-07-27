# Creating mission definitions

Start from `template.yaml` in this directory -- copy it, rename it, and
edit the copy. This file explains the schema and the reasoning behind its
less-obvious parts. `mission_alpha.yaml` (40 VMs) and `mission_bravo.yaml`
(4 VMs) in this same directory are real, generated examples you can also
read for reference -- see `../../tools/generate_mission.py` if you want to
regenerate or adapt them programmatically instead of hand-editing.

## Top-level fields

| Field | Required | Meaning |
|---|---|---|
| `mission` | yes | Unique mission name. Scopes every OVN switch/router/port name so multiple missions (or multiple concurrent *deployments* of the same mission) never collide. |
| `file_version` | no | **This file's own** revision tag (e.g. `"1.0.0"`, `"Beta"`). Recorded in deployment status/step logs for audit purposes -- lets you tell which version of a mission definition a given running deployment came from. Bump it whenever you meaningfully change a mission file you're maintaining over time. |
| `networks` | yes | `{name: vlan_id}`. Every VM interface must reference a name defined here. |
| `vms` | yes | `{vm_name: <VM entry>}` -- see below. |
| `placement` | yes | `{vm_name: host_name}`. Must cover every VM in `vms`, exactly -- no more, no fewer. `host_name` must exist in `config/hosts.yaml`. |

There is deliberately **no separate placement file** -- `placement:` lives
only here, in the mission definition, as the single source of truth. An
earlier draft of this project kept a duplicate copy in a separate
`placement/` directory; that was removed because it meant two places to
update and a real risk of them drifting out of sync.

## VM entry fields

| Field | Required | Meaning |
|---|---|---|
| `type` | yes | `linked_clone` (disk-backed, cloned from a golden image) or `pxe` (diskless, network boot). |
| `image` | only for `linked_clone` | Golden image name -- must be a key in `config/storage.yaml`'s `golden.images` catalog. Forbidden for `pxe`. |
| `image_revision` | no | Which revision of `image` to clone from (e.g. `"Alpha"`, `"Beta"`) -- must be a key under that image's `revisions:` in the catalog. Omit to use the catalog's `default_revision`. Forbidden for `pxe`. |
| `cpu` | yes | vCPU count. |
| `memory` | yes | MiB. |
| `gpu` | no | `{profile: "<name>"}` -- see "GPU profiles" below. Omit entirely for no GPU. |
| `interfaces` | yes | `{network_name: {mac_suffix: "..."} }` -- see "MAC addresses" below. Any number of interfaces is supported (1, 8, or more) -- there's no fixed NIC count; every VM simply gets one interface per entry here, in order. |

## MAC addresses: why only three octets, and why they live per-interface

**Operator requirement**: guest software running inside mission VMs has
MAC addresses hardcoded into its licensing/configuration. The real MAC
each interface needs is therefore something *you* determine and supply --
this system will never invent one for an interface that needs to match a
specific external expectation.

Each interface's `mac_suffix` is **only the last three octets** (e.g.
`"10:00:00"`), written directly under that interface so it's easy to see
which value belongs to which NIC at a glance. The **first three octets**
(an OUI-like prefix) are deliberately **not configured anywhere** -- a
fresh, random prefix is generated automatically for each individual
*deployment* of this mission file (every time you `POST` it, or otherwise
provision it), and applied uniformly to every interface in that one
deployment.

This means:

```
deployment 1 of Mission-Alpha:  AA:BB:CC:10:00:00   (random prefix + your configured suffix)
deployment 2 of Mission-Alpha:  XX:YY:ZZ:10:00:00   (different random prefix, same suffix)
```

You can deploy the exact same mission file twice (or more) concurrently,
fully isolated from each other at the MAC layer, while any in-guest
tooling that substring-matches on `10:00:00` still finds the right
interface inside *its own* deployment's isolated L2 domain (each
deployment also gets its own isolated OVN logical switches regardless --
the MAC prefix randomization is an additional, independent layer of
separation on top of that).

Random prefixes are checked against every other *currently active*
deployment's prefix before being assigned, so two concurrent deployments
can never collide (see `core/macs.py` and
`core/state.py:MissionStore.create_with_mac_resolution`).

**If you leave `mac_suffix` unset** (omit it, or write `null` /
`{}`) for a given interface, a random suffix is generated for it instead.
This is fine for an interface nothing outside the VM needs to recognize
by MAC -- but since it's random, it is **not** guaranteed to be the same
on a redeploy. Only rely on a stable suffix you explicitly set yourself.

**Duplicate suffixes are rejected**, both within one VM (two interfaces
on the same VM can't share a suffix) and across different VMs in the same
mission (since the prefix is identical for the whole deployment, two
VMs sharing a suffix would end up with the same full MAC) -- you'll get a
clear validation error rather than a silent conflict.

## GPU profiles (NVIDIA MIG / vGPU)

`gpu: {profile: "<name>"}` requests a specific, already-partitioned GPU
slice -- an NVIDIA H100 MIG instance (e.g. `"H100-MIG-3g.40gb"`) or an
NVIDIA L4 vGPU instance (e.g. `"L4-vGPU-4Q"`). The profile name must match
an entry in at least one host's `config/hosts.yaml` `gpu_devices:` list.
Deployment matches this VM to a specific free slice with that exact
profile at provisioning time; if none is free anywhere, the deployment
fails with a clear error rather than silently proceeding without a GPU or
picking a different profile than requested.

This check is atomic and cluster-wide: it's resolved once, at
registration (`POST /missions`), against every *other currently-active*
deployment's own GPU reservations -- not just this mission's own
VMs against total host capacity. Submitting a second mission (or a second
deployment of the same one) that would need a slice already claimed by a
still-running deployment gets a `409 Conflict` immediately, rather than
both being accepted and colliding later.

Creating the MIG/vGPU slices themselves (`nvidia-smi mig -cgi ...` for
MIG, or the NVIDIA vGPU Manager for L4) is a manual, one-time, host-side
step this automation does not perform -- see
`../../bootstrap/scripts/enumerate_mdev_gpus.sh`, which lists whatever
mediated devices already exist on a host so you can copy their UUIDs into
`config/hosts.yaml`.

## Golden images and revisions

`image` + `image_revision` together select a specific, versioned artifact
from `config/storage.yaml`'s `golden.images` catalog -- e.g.:

```yaml
# config/storage.yaml
golden:
  images:
    rhel9-db-golden:
      default_revision: Alpha
      revisions:
        Alpha:
          filename: rhel9-db-golden-alpha.qcow2
          snapshot: golden-snap-alpha
        Beta:
          filename: rhel9-db-golden-beta.qcow2
          snapshot: golden-snap-beta
```

A mission can pin a specific revision (`image_revision: Beta`) or omit it
to always track whatever the catalog's `default_revision` currently is.
The resolved revision is recorded in the deployment's step log, so you
can always tell which build a given deployment actually cloned from.

Golden images are stored **separately** from runtime (per-VM clone)
disks -- see `config/storage.yaml`'s `golden.source` setting, which
supports golden images living on the same Ceph cluster in a different
pool, on an entirely separate/remote Ceph cluster, or on a plain
filesystem mount. Runtime VM disks always live in the pool configured
under `runtime:`, regardless of where golden images come from.

## Placement and capacity

`placement:` is static in this prototype -- you (or
`tools/generate_mission.py`'s simple reference bin-packer) decide which
host each VM lands on. Whatever you provide, deployment always validates
it against `config/hosts.yaml`'s CPU/memory/GPU-profile capacity before
provisioning anything (`core/placement.py`); an infeasible placement is
rejected with a clear list of which host/resource would be oversubscribed
before any resource is created.

## Validating a mission file without deploying it

```bash
cd management
python3 -c "
from pathlib import Path
from services.missions import load_mission_yaml, load_host_inventory
from core.placement import validate_placement

mission = load_mission_yaml(Path('mission_defs/your_mission.yaml'))
report = validate_placement(mission, load_host_inventory())
print('OK' if report.ok else report.as_dict())
"
```
