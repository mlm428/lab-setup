# The rescue/setup node

This directory holds the tooling for the operator's actual deployment
model: **one external "rescue/setup" node** that configures every cluster
host remotely, rather than logging into each host individually. `bootstrap.py`
itself is unchanged and still only knows how to configure the single host
it's run on (see `../bootstrap/bootstrap.py`) -- `orchestrate.py` here is
what turns that into a 1-to-many operation over SSH, without touching
`bootstrap.py` at all.

```
   rescue/setup node                    compute01, compute02, ... (airgapped,
   (this repo lives here,          SSH   freshly-STIG'd, no initial setup)
   has network access to     ------------>
   the management network,
   but is itself airgapped
   from the wider internet)
```

## Quick start

```bash
# From the rescue node, against every host in config/hosts.yaml:
./rescue/orchestrate.py --all --dry-run          # rehearse first
./rescue/orchestrate.py --all                     # then for real
./rescue/orchestrate.py --hosts compute01,compute02 --parallel
```

Each run rsyncs `bootstrap/` and `config/` to the target host, runs
`sudo bootstrap.py --host <name>` there over SSH, and writes a full copy
of that host's output to `rescue/logs/<host>-<timestamp>.log` -- in
addition to bootstrap.py's own existing per-run console/module logging on
the host itself. Reviewing every host's bootstrap run in one place after
a multi-host run is exactly what this is for.

## STIG'd host prerequisites

The operator's target hosts are described as "off the shelf STIG'd images
with no initial setup." Before `orchestrate.py` can reach one, it needs:

1. **Network reachability** from the rescue node to the host's management
   interface (whatever `address` you'll put in `config/hosts.yaml`).
2. **SSH access** as some user capable of `sudo` -- STIG profiles commonly
   disable direct root SSH login (`PermitRootLogin no`), so plan on a
   named admin user + `--ssh-user` rather than assuming root login works.
   Key-based auth (not password) is the practical requirement in an
   automated flow like this; set that up as part of your image's initial
   STIG-compliant account provisioning, outside this project's scope.
3. **`sudo` with enough privilege** to run `bootstrap.py` (which installs
   packages, edits `/etc/default/grub`, starts systemd services, creates
   libvirt/OVS/OVN resources) -- either passwordless `sudo` for sensitive
   automation, or an `ssh -t` + interactive sudo flow you adapt
   `orchestrate.py`'s `build_ssh_bootstrap_command` for, if your STIG
   posture requires a prompt.
4. **`python3`** already present -- STIG'd RHEL images ship it as part of
   the base OS, but confirm your specific image/profile didn't strip it.
5. **`rsync`** on both ends (the rescue node needs it locally; the target
   host needs an `rsync` binary for the copy to land — most minimal RHEL
   images include it, but airgapped/minimized images sometimes don't; add
   it to the offline package set below if missing).
6. **SELinux / firewalld implications of STIG hardening**: a STIG profile
   typically leaves SELinux enforcing (expected -- this project assumes
   that) and firewalld locked down to a minimal port set. bootstrap.py's
   `validation.py` step checks for the services it needs to be reachable
   but does **not** open firewall ports itself (out of scope for this
   prototype) -- if `ovn-controller`'s Geneve port (UDP 6081), libvirt's
   migration/VNC ports, or Ceph's monitor/OSD ports (6789, 6800-7300) are
   blocked by the host's STIG firewalld zone, add explicit `firewall-cmd
   --permanent --add-port=...` rules for them as part of your STIG
   remediation baseline before running bootstrap.py, or extend
   `bootstrap/modules/networking.py` to do it if you want it automated.

## Getting packages onto airgapped hosts

Neither `bootstrap.py` nor `orchestrate.py` needs any code changes to
work fully offline -- both already just invoke `dnf`/`rsync`/`ssh`
against whatever's configured, with no hardcoded internet endpoints. The
piece that's actually missing in a truly airgapped environment is the
packages themselves. Two scripts under `../bootstrap/scripts/` handle
that:

1. **`fetch_offline_packages.sh`** -- run on any INTERNET-CONNECTED
   machine matching your hosts' RHEL version/arch (a subscription-manager-
   registered VM, a UBI container, etc.). Reads `config/cluster.yaml`'s
   package list and downloads every package plus its full dependency
   chain into a flat directory, ready to transfer into your airgapped
   environment by whatever one-way transfer process you use.

2. **`setup_local_repo.sh`** -- run in the airgapped environment (on the
   rescue node, or per-host) against that transferred directory. Builds
   repo metadata and either serves it over HTTP from the rescue node
   (`--serve`, so you only transfer the RPMs once and every host points
   at the rescue node) or configures a `file://` repo directly on a host
   that already has the RPM directory locally.

After that, `bootstrap.py`'s package installation step
(`bootstrap/modules/packages.py`) works exactly as it does in a connected
environment -- `dnf install` just resolves against whichever repo is
configured, online or off.

## Centralized logs

`rescue/logs/` accumulates one file per host per orchestrate.py run
(`<host>-<timestamp>.log`), holding that host's full rsync + SSH +
bootstrap.py output. This is in addition to, not instead of,
bootstrap.py's own existing logging on the host itself
(`bootstrap/modules/util.py`'s `RunContext` / Python `logging` output,
visible on the host's own console/journal during the run) -- the point of
this directory is having everything in one place to review after a
multi-host run without needing to SSH back into each host individually.
