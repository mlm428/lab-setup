"""
Shared helpers for bootstrap modules: subprocess execution with consistent
logging and --dry-run support, plus a couple of small idempotency helpers.

This file isn't called out by name in the design doc's suggested layout,
but every module in that doc's own snippets calls `subprocess.run([...],
check=True)` directly and repeats the same pattern -- we centralize it here
so every module logs consistently and honors --dry-run without duplicating
that logic ten times over.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Sequence


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger("bootstrap")


class CommandError(RuntimeError):
    """Raised when a required host command fails and check=True was set."""

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"command failed ({returncode}): {' '.join(cmd)}\n{stderr.strip()}"
        )


@dataclass
class RunContext:
    """
    Carries cross-cutting execution options through every module function
    so bootstrap.py can flip one flag (--dry-run) and have it honored
    everywhere, instead of threading a bool through every function
    signature by hand.
    """

    dry_run: bool = False
    verbose: bool = False
    # Accumulates a step-by-step audit trail bootstrap.py prints/serializes
    # at the end of a run -- mirrors the design doc's "all steps log
    # progress; errors are reported clearly" requirement.
    audit: list = field(default_factory=list)

    def record(self, step: str, status: str, detail: str = "") -> None:
        self.audit.append({"step": step, "status": status, "detail": detail})


def run(
    ctx: RunContext,
    cmd: Sequence[str],
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a host command, honoring RunContext.dry_run. Always logs the
    command. Raises CommandError on non-zero exit when check=True (mirrors
    subprocess.run(check=True) but with our own logging/audit trail).
    """
    printable = " ".join(cmd)
    if ctx.dry_run:
        log.info("[dry-run] %s", printable)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    log.debug("running: %s", printable)
    try:
        result = subprocess.run(
            list(cmd),
            input=input_text,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # Binary not on PATH -- treat as a clean failed command rather than
        # an uncaught crash. Real RHEL hosts will have these tools once
        # packages.install_base_packages() has run; this guard mainly
        # matters for --check-only against a not-yet-bootstrapped host, or
        # for exercising this code on a non-RHEL dev machine.
        result = subprocess.CompletedProcess(
            cmd, 127, stdout="", stderr=f"{cmd[0]}: command not found"
        )
    if check and result.returncode != 0:
        raise CommandError(cmd, result.returncode, result.stderr)
    return result


def command_exists(binary: str) -> bool:
    return shutil.which(binary) is not None


def service_is_active(ctx: RunContext, service: str) -> bool:
    if ctx.dry_run:
        return True
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service], capture_output=True, text=True
        )
    except FileNotFoundError:
        return False
    return result.stdout.strip() == "active"
