#!/usr/bin/env python3
"""
Remote bootstrap orchestrator: runs bootstrap.py against 1-to-many
airgapped, freshly-STIG'd target hosts from a single external
"rescue/setup" node -- per operator requirement: "my intention is to
have a single rescue/setup image/system running the bootstrap services to
support the remote configuration of the 1-to-many hosts."

bootstrap.py itself still only knows how to configure the ONE host it
runs on (see bootstrap/bootstrap.py) -- this script is what turns that
into a 1-to-many operation, without changing bootstrap.py at all. For
each selected host, it:

  1. rsyncs this repo's bootstrap/ and config/ directories to the host
     (over the rescue node's own network access to it -- itself airgapped
     from the wider internet, but reachable from the rescue node on the
     management network)
  2. runs `sudo bootstrap.py --host <name> ...` there over SSH
  3. streams that host's output back to this terminal AND writes a copy
     to rescue/logs/<host>-<timestamp>.log -- so every host's run is
     captured centrally for review/troubleshooting in one place, per
     operator request, in addition to whatever bootstrap.py already logs
     locally on the host / to its own console.

See rescue/README.md for the STIG'd-host prerequisites this assumes
(SSH reachability, sudo, python3) and for the offline-package-repo
scripts (bootstrap/scripts/fetch_offline_packages.sh,
bootstrap/scripts/setup_local_repo.sh) that get packages onto hosts with
no internet access at all.

Usage:
    ./rescue/orchestrate.py --hosts compute01,compute02 [--dry-run] [--parallel]
    ./rescue/orchestrate.py --all [--dry-run] [--skip-gpu]
    ./rescue/orchestrate.py --all --ssh-user admin --remote-dir /opt/bootstrap
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = Path(__file__).resolve().parent / "logs"

MANAGEMENT_DIR = REPO_ROOT / "management"
sys.path.insert(0, str(MANAGEMENT_DIR))
from core import config_loader  # noqa: E402


@dataclass
class RemoteBootstrapResult:
    """Outcome of one host's remote bootstrap run."""
    host: str
    success: bool
    returncode: int | None
    log_path: Path
    error: str = ""


def build_sync_commands(host_address: str, ssh_user: str, remote_dir: str, dry_run: bool = False) -> list[list[str]]:
    """
    Build the rsync commands that copy bootstrap/ and config/ to a target
    host, each into its own matching subdirectory under `remote_dir` (so
    the copied bootstrap.py's own default `--config-dir ../config`
    resolves correctly on the remote side, unchanged).

    Args:
        host_address: Target host's real network address.
        ssh_user: Remote SSH user to sync as.
        remote_dir: Destination directory on the target host.
        dry_run: If True, adds rsync's own --dry-run flag (only affects
            the file sync itself -- pass --dry-run to the returned
            bootstrap.py command separately for that).

    Returns:
        Two rsync argv lists (bootstrap/, then config/), ready for subprocess.run.
    """
    commands = []
    for local_name in ("bootstrap", "config"):
        cmd = ["rsync", "-az", "--delete"]
        if dry_run:
            cmd.append("--dry-run")
        cmd += [f"{REPO_ROOT / local_name}/", f"{ssh_user}@{host_address}:{remote_dir}/{local_name}/"]
        commands.append(cmd)
    return commands


def build_ssh_bootstrap_command(host_name: str, host_address: str, ssh_user: str, remote_dir: str, dry_run: bool, skip_gpu: bool) -> list[str]:
    """
    Build the SSH command that runs bootstrap.py on the target host,
    exactly as if an operator had logged in and run it locally.

    Args:
        host_name: The `--host` value bootstrap.py needs (its
            config/hosts.yaml inventory key, not necessarily its network address).
        host_address: Real network address to SSH into.
        ssh_user: Remote SSH user (bootstrap.py itself is invoked via sudo
            regardless, since it changes system config).
        remote_dir: Directory bootstrap/ + config/ were synced into (see build_sync_commands).
        dry_run: Passed through as bootstrap.py's own --dry-run.
        skip_gpu: Passed through as bootstrap.py's own --skip-gpu.

    Returns:
        A full ["ssh", "user@host", "remote command string"] argv list.
    """
    remote_parts = ["sudo", f"{remote_dir}/bootstrap/bootstrap.py", "--host", host_name]
    if dry_run:
        remote_parts.append("--dry-run")
    if skip_gpu:
        remote_parts.append("--skip-gpu")
    return ["ssh", f"{ssh_user}@{host_address}", " ".join(remote_parts)]


def run_remote_bootstrap(
    host_name: str,
    host_address: str,
    ssh_user: str = "root",
    remote_dir: str = "/opt/mission-cluster-bootstrap",
    dry_run: bool = False,
    skip_gpu: bool = False,
) -> RemoteBootstrapResult:
    """
    Sync bootstrap/ + config/ to one host and run bootstrap.py there over
    SSH, capturing full output to a per-host, per-run log file under
    rescue/logs/ in addition to streaming it to this process's own
    stdout/stderr.

    Args:
        host_name: config/hosts.yaml inventory key for this host.
        host_address: Real network address to sync/SSH to.
        ssh_user: Remote SSH user.
        remote_dir: Where to place bootstrap/ + config/ on the target host.
        dry_run: Passed through to bootstrap.py's own --dry-run (does NOT
            skip the rsync step -- files are always actually copied, only
            bootstrap.py's own system-modifying actions are simulated).
        skip_gpu: Passed through to bootstrap.py's own --skip-gpu.

    Returns:
        A RemoteBootstrapResult recording success/failure and the log file path.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"{host_name}-{timestamp}.log"

    log_lines = [f"=== rescue/orchestrate.py: remote bootstrap for {host_name} ({host_address}) ===", f"started: {timestamp}", ""]

    try:
        for sync_cmd in build_sync_commands(host_address, ssh_user, remote_dir):
            log_lines.append(f"$ {' '.join(sync_cmd)}")
            sync_result = subprocess.run(sync_cmd, capture_output=True, text=True)
            log_lines.append(sync_result.stdout)
            if sync_result.returncode != 0:
                log_lines.append(f"--- stderr ---\n{sync_result.stderr}")
                _write_log(log_path, log_lines)
                return RemoteBootstrapResult(host_name, False, sync_result.returncode, log_path, error="rsync failed")

        ssh_cmd = build_ssh_bootstrap_command(host_name, host_address, ssh_user, remote_dir, dry_run, skip_gpu)
        log_lines.append(f"$ {' '.join(ssh_cmd)}")
        result = subprocess.run(ssh_cmd, capture_output=True, text=True)
        log_lines.append("--- stdout ---")
        log_lines.append(result.stdout)
        log_lines.append("--- stderr ---")
        log_lines.append(result.stderr)
        _write_log(log_path, log_lines)

        print(f"[{host_name}] {'OK' if result.returncode == 0 else 'FAILED (rc=' + str(result.returncode) + ')'} -- full log: {log_path}")
        return RemoteBootstrapResult(host_name, result.returncode == 0, result.returncode, log_path)

    except Exception as exc:  # noqa: BLE001 - one host's failure must not abort the others
        log_lines.append(f"EXCEPTION: {exc}")
        _write_log(log_path, log_lines)
        print(f"[{host_name}] FAILED -- {exc}")
        return RemoteBootstrapResult(host_name, False, None, log_path, error=str(exc))


def _write_log(log_path: Path, lines: list[str]) -> None:
    """Write this host's full captured output to its centralized log file."""
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def orchestrate(
    hosts: dict,
    ssh_user: str,
    remote_dir: str,
    dry_run: bool,
    skip_gpu: bool,
    parallel: bool,
) -> list[RemoteBootstrapResult]:
    """
    Run remote bootstrap against every given host, sequentially or in
    parallel, and print a final pass/fail summary.

    Args:
        hosts: {host_name: HostSpec-like object with .address} to bootstrap.
        ssh_user: Remote SSH user.
        remote_dir: Remote sync destination.
        dry_run: Passed through to bootstrap.py.
        skip_gpu: Passed through to bootstrap.py.
        parallel: If True, runs all hosts concurrently (thread per host);
            otherwise runs one at a time, in inventory order.

    Returns:
        One RemoteBootstrapResult per host.
    """
    results: list[RemoteBootstrapResult] = []

    if parallel:
        with ThreadPoolExecutor(max_workers=min(8, len(hosts) or 1)) as executor:
            futures = {
                executor.submit(run_remote_bootstrap, name, h["address"], ssh_user, remote_dir, dry_run, skip_gpu): name
                for name, h in hosts.items()
            }
            for future in as_completed(futures):
                results.append(future.result())
    else:
        for name, h in hosts.items():
            results.append(run_remote_bootstrap(name, h["address"], ssh_user, remote_dir, dry_run, skip_gpu))

    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    print(f"\n=== Summary: {len(succeeded)}/{len(results)} hosts succeeded ===")
    for r in failed:
        print(f"  FAILED: {r.host} (log: {r.log_path})")
    print(f"All logs: {LOGS_DIR}/")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--hosts", help="Comma-separated host names (must match config/hosts.yaml keys)")
    target.add_argument("--all", action="store_true", help="Bootstrap every host in config/hosts.yaml")
    parser.add_argument("--ssh-user", default="root", help="Remote SSH user (default: root)")
    parser.add_argument("--remote-dir", default="/opt/mission-cluster-bootstrap", help="Where to sync bootstrap/+config/ on each target host")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run through to bootstrap.py on every host (files are still synced)")
    parser.add_argument("--skip-gpu", action="store_true", help="Pass --skip-gpu through to bootstrap.py on every host")
    parser.add_argument("--parallel", action="store_true", help="Bootstrap all selected hosts concurrently instead of one at a time")
    args = parser.parse_args()

    hosts_cfg = config_loader.load_hosts_config()["hosts"]
    if args.all:
        selected = hosts_cfg
    else:
        names = [n.strip() for n in args.hosts.split(",")]
        unknown = [n for n in names if n not in hosts_cfg]
        if unknown:
            print(f"ERROR: unknown host(s) not in config/hosts.yaml: {unknown}", file=sys.stderr)
            sys.exit(1)
        selected = {n: hosts_cfg[n] for n in names}

    results = orchestrate(selected, args.ssh_user, args.remote_dir, args.dry_run, args.skip_gpu, args.parallel)
    sys.exit(0 if all(r.success for r in results) else 1)


if __name__ == "__main__":
    main()
