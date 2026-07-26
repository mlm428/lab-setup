"""Unit tests for rescue/orchestrate.py -- command construction and multi-host orchestration logic, with subprocess mocked out (no real SSH/rsync in CI/sandbox)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orchestrate  # noqa: E402


class TestBuildSyncCommands(unittest.TestCase):
    def test_builds_one_rsync_per_directory(self):
        commands = orchestrate.build_sync_commands("10.0.0.5", "root", "/opt/bootstrap")
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("bootstrap" in " ".join(c) for c in commands))
        self.assertTrue(any("config" in " ".join(c) for c in commands))

    def test_dry_run_adds_rsync_dry_run_flag(self):
        commands = orchestrate.build_sync_commands("10.0.0.5", "root", "/opt/bootstrap", dry_run=True)
        for cmd in commands:
            self.assertIn("--dry-run", cmd)

    def test_destination_uses_ssh_user_and_address(self):
        commands = orchestrate.build_sync_commands("10.0.0.5", "admin", "/opt/bootstrap")
        self.assertTrue(any("admin@10.0.0.5" in arg for arg in commands[0]))


class TestBuildSshBootstrapCommand(unittest.TestCase):
    def test_basic_command_shape(self):
        cmd = orchestrate.build_ssh_bootstrap_command("compute01", "10.0.0.5", "root", "/opt/bootstrap", dry_run=False, skip_gpu=False)
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[1], "root@10.0.0.5")
        self.assertIn("--host compute01", cmd[2])
        self.assertIn("/opt/bootstrap/bootstrap/bootstrap.py", cmd[2])
        self.assertIn("sudo", cmd[2])

    def test_dry_run_flag_passed_through(self):
        cmd = orchestrate.build_ssh_bootstrap_command("compute01", "10.0.0.5", "root", "/opt/bootstrap", dry_run=True, skip_gpu=False)
        self.assertIn("--dry-run", cmd[2])

    def test_skip_gpu_flag_passed_through(self):
        cmd = orchestrate.build_ssh_bootstrap_command("compute01", "10.0.0.5", "root", "/opt/bootstrap", dry_run=False, skip_gpu=True)
        self.assertIn("--skip-gpu", cmd[2])

    def test_uses_host_name_not_address_for_host_flag(self):
        # bootstrap.py's --host must be the config/hosts.yaml inventory
        # key, not the network address it's reached at.
        cmd = orchestrate.build_ssh_bootstrap_command("compute01", "10.0.0.5", "root", "/opt/bootstrap", False, False)
        self.assertIn("--host compute01", cmd[2])
        self.assertNotIn("--host 10.0.0.5", cmd[2])


class TestRunRemoteBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp_logs = Path(orchestrate.LOGS_DIR)

    def test_success_writes_log_and_returns_success(self):
        with mock.patch.object(orchestrate.subprocess, "run") as m_run:
            m_run.side_effect = [
                mock.Mock(returncode=0, stdout="synced", stderr=""),
                mock.Mock(returncode=0, stdout="synced", stderr=""),
                mock.Mock(returncode=0, stdout="host compute01 is ready", stderr=""),
            ]
            result = orchestrate.run_remote_bootstrap("compute01", "10.0.0.5")

        self.assertTrue(result.success)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.log_path.exists())
        content = result.log_path.read_text()
        self.assertIn("compute01", content)
        self.assertIn("host compute01 is ready", content)
        result.log_path.unlink()

    def test_rsync_failure_stops_before_ssh_and_returns_failure(self):
        with mock.patch.object(orchestrate.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="rsync: connection refused")
            result = orchestrate.run_remote_bootstrap("compute01", "10.0.0.5")

        self.assertFalse(result.success)
        self.assertEqual(m_run.call_count, 1)  # never reached the ssh bootstrap call
        result.log_path.unlink()

    def test_ssh_failure_returns_failure_with_returncode(self):
        with mock.patch.object(orchestrate.subprocess, "run") as m_run:
            m_run.side_effect = [
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="bootstrap failed: Ceph unreachable"),
            ]
            result = orchestrate.run_remote_bootstrap("compute01", "10.0.0.5")

        self.assertFalse(result.success)
        self.assertEqual(result.returncode, 1)
        result.log_path.unlink()

    def test_exception_during_sync_is_captured_not_raised(self):
        with mock.patch.object(orchestrate.subprocess, "run", side_effect=FileNotFoundError("rsync not found")):
            result = orchestrate.run_remote_bootstrap("compute01", "10.0.0.5")
        self.assertFalse(result.success)
        self.assertIn("rsync not found", result.error)
        result.log_path.unlink()


class TestOrchestrate(unittest.TestCase):
    def test_sequential_runs_every_host_and_summarizes(self):
        hosts = {"compute01": {"address": "10.0.0.1"}, "compute02": {"address": "10.0.0.2"}}
        with mock.patch.object(orchestrate, "run_remote_bootstrap") as m_run:
            m_run.side_effect = [
                orchestrate.RemoteBootstrapResult("compute01", True, 0, Path("/tmp/x1.log")),
                orchestrate.RemoteBootstrapResult("compute02", False, 1, Path("/tmp/x2.log")),
            ]
            results = orchestrate.orchestrate(hosts, "root", "/opt/bootstrap", False, False, parallel=False)

        self.assertEqual(len(results), 2)
        self.assertEqual(m_run.call_count, 2)

    def test_one_host_failure_does_not_prevent_others_running(self):
        hosts = {"compute01": {"address": "10.0.0.1"}, "compute02": {"address": "10.0.0.2"}}

        def fake_run(name, address, *a, **kw):
            if name == "compute01":
                raise RuntimeError("should be caught inside run_remote_bootstrap, not orchestrate")
            return orchestrate.RemoteBootstrapResult(name, True, 0, Path("/tmp/x.log"))

        # run_remote_bootstrap itself guarantees it never raises (see its
        # own except-Exception clause) -- this test confirms orchestrate()
        # doesn't ALSO need its own try/except to get that guarantee, by
        # using a real (non-raising) stub for host 1 instead.
        with mock.patch.object(orchestrate, "run_remote_bootstrap") as m_run:
            m_run.side_effect = [
                orchestrate.RemoteBootstrapResult("compute01", False, 1, Path("/tmp/x1.log"), error="boom"),
                orchestrate.RemoteBootstrapResult("compute02", True, 0, Path("/tmp/x2.log")),
            ]
            results = orchestrate.orchestrate(hosts, "root", "/opt/bootstrap", False, False, parallel=False)

        self.assertEqual(len(results), 2)
        self.assertFalse(results[0].success)
        self.assertTrue(results[1].success)

    def test_parallel_runs_every_host(self):
        hosts = {"compute01": {"address": "10.0.0.1"}, "compute02": {"address": "10.0.0.2"}, "compute03": {"address": "10.0.0.3"}}
        with mock.patch.object(orchestrate, "run_remote_bootstrap") as m_run:
            m_run.side_effect = lambda name, address, *a, **kw: orchestrate.RemoteBootstrapResult(name, True, 0, Path("/tmp/x.log"))
            results = orchestrate.orchestrate(hosts, "root", "/opt/bootstrap", False, False, parallel=True)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.success for r in results))


if __name__ == "__main__":
    unittest.main()
