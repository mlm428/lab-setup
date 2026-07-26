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
    """Get (or create, on first call) a console logger with a consistent timestamp/level/name/message format."""
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
        """Append one entry to this run's audit trail (step name, "ok"/"skipped"/"failed", free-form detail)."""
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

    Args:
        ctx: Run context -- if ctx.dry_run, the command is logged but
            never actually executed, and a synthetic success result is
            returned instead.
        cmd: Argv list (never a shell string -- no shell=True anywhere in
            this module, by design).
        check: If True (default), raise CommandError on non-zero exit.
            Pass False for commands where a non-zero exit is an expected,
            handled outcome (e.g. an idempotency probe).
        input_text: Optional stdin to pass to the command.

    Returns:
        The completed subprocess.CompletedProcess (or a synthetic
        zero-exit one, in a dry run).

    Raises:
        CommandError: if check=True and the command exits non-zero.
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
    """True if `binary` is found on PATH."""
    return shutil.which(binary) is not None


def service_is_active(ctx: RunContext, service: str) -> bool:
    """
    Check whether a systemd unit is currently active.

    Args:
        ctx: Run context -- in a dry run, always returns True (nothing
            has actually been started yet, so callers that branch on
            "already active, skip" correctly skip re-issuing the enable
            command during a rehearsal).
        service: systemd unit name (e.g. "libvirtd", "cockpit.socket").

    Returns:
        True if `systemctl is-active` reports "active", False otherwise
        (including if systemctl itself isn't found).
    """
    if ctx.dry_run:
        return True
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service], capture_output=True, text=True
        )
    except FileNotFoundError:
        return False
    return result.stdout.strip() == "active"
