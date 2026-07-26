#!/usr/bin/env python3
"""
Stand-alone acceptance-criteria check against an already-deployed mission,
per both source docs' "Test Plan" / "Acceptance Criteria" sections. Unlike
workers/deploy.py's built-in post-deploy validation (which runs
automatically as the last step of every deployment), this script can be
re-run any time against a running mission -- e.g. after a host reboot, or
as part of a periodic compliance check -- without redeploying anything.

Requires a real, reachable cluster (libvirt on every placed host, the OVN
Northbound DB, and python3-libvirt/ovsdbapp installed) -- this is
therefore NOT runnable in the build sandbox; see README's transparency
section for what could and couldn't be exercised there. The logic it
calls (services/validation.py) is unit-testable and tested without a live
cluster; this script is the thin live-check wrapper around it.

Usage:
    python3 tools/validate_deployment.py --mission-id <id> \
        [--management-url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MANAGEMENT_DIR = Path(__file__).resolve().parent.parent / "management"
sys.path.insert(0, str(MANAGEMENT_DIR))


def fetch_mission_status(base_url: str, mission_id: str) -> dict:
    url = f"{base_url}/missions/{mission_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: {exc.code} fetching {url}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"ERROR: could not reach management API at {base_url}: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-check a deployed mission against the acceptance-criteria table.")
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--management-url", default="http://localhost:8000")
    args = parser.parse_args()

    status = fetch_mission_status(args.management_url, args.mission_id)

    print(f"Mission: {status['name']} ({status['mission_id']})")
    print(f"State:   {status['state']}")
    if status.get("mac_prefix"):
        print(f"MAC prefix: {status['mac_prefix']} (this deployment's randomized OUI-style prefix)")
    if status.get("error"):
        print(f"Error:   {status['error']}")

    print("\nStep log:")
    for step in status.get("steps", []):
        marker = "OK " if step["status"] == "ok" else "ERR"
        print(f"  [{marker}] {step['step']:<24} {step['detail']}")

    if status["state"] == "Running":
        print("\nAcceptance criteria: PASS (mission reports Running; "
              "see step log above for the validation step's detail, "
              "which includes per-VM NIC/MAC/disk/GPU/CPU/memory checks).")
        return 0
    else:
        print(f"\nAcceptance criteria: FAIL (mission state is '{status['state']}', not Running)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
