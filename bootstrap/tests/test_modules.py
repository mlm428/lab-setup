"""
Unit tests for bootstrap/modules/* logic that does NOT require a real RHEL
host (systemd, libvirt, OVS, Ceph). Uses only the Python standard library
(unittest + unittest.mock) so it runs in any Python 3 environment,
including one with no network access and none of qemu-kvm/libvirt/ovs/ceph
installed -- exactly the constraint this project was built under.

Run with:  python3 -m unittest discover -s bootstrap/tests -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.util import RunContext, run, CommandError, service_is_active
from modules import packages as packages_mod
from modules import validation as validation_mod
from modules import gpu as gpu_mod


class TestRunContextAndRun(unittest.TestCase):
    def test_dry_run_never_executes_and_returns_zero(self):
        ctx = RunContext(dry_run=True)
        result = run(ctx, ["definitely-not-a-real-binary", "--flag"])
        self.assertEqual(result.returncode, 0)

    def test_missing_binary_is_reported_not_raised(self):
        ctx = RunContext(dry_run=False)
        result = run(ctx, ["definitely-not-a-real-binary-xyz"], check=False)
        self.assertEqual(result.returncode, 127)
        self.assertIn("not found", result.stderr)

    def test_missing_binary_with_check_true_raises_command_error(self):
        ctx = RunContext(dry_run=False)
        with self.assertRaises(CommandError):
            run(ctx, ["definitely-not-a-real-binary-xyz"], check=True)

    def test_real_command_success(self):
        ctx = RunContext(dry_run=False)
        result = run(ctx, ["true"])
        self.assertEqual(result.returncode, 0)

    def test_real_command_failure_raises(self):
        ctx = RunContext(dry_run=False)
        with self.assertRaises(CommandError):
            run(ctx, ["false"])

    def test_service_is_active_false_when_systemctl_absent(self):
        ctx = RunContext(dry_run=False)
        # In this sandbox systemctl genuinely doesn't exist; verifies the
        # FileNotFoundError guard rather than crashing the whole run.
        self.assertFalse(service_is_active(ctx, "libvirtd"))

    def test_service_is_active_true_in_dry_run(self):
        ctx = RunContext(dry_run=True)
        self.assertTrue(service_is_active(ctx, "libvirtd"))


class TestPackagesModule(unittest.TestCase):
    def test_flatten_groups(self):
        groups = {"a": ["pkg1", "pkg2"], "b": ["pkg3"]}
        self.assertEqual(
            sorted(packages_mod._flatten(groups)), ["pkg1", "pkg2", "pkg3"]
        )

    def test_installed_packages_returns_set_without_crashing(self):
        # rpm isn't installed in this sandbox either -- confirms the
        # FileNotFoundError fallback path returns an empty set instead of
        # raising.
        result = packages_mod.installed_packages()
        self.assertIsInstance(result, set)

    def test_install_base_packages_skips_when_all_present(self):
        ctx = RunContext(dry_run=False)
        cluster_cfg = {
            "packages": {"grp": ["already-there"]},
            "services": [],
        }
        with mock.patch.object(
            packages_mod, "installed_packages", return_value={"already-there"}
        ):
            packages_mod.install_base_packages(ctx, cluster_cfg)
        statuses = [a["status"] for a in ctx.audit if a["step"] == "install_packages"]
        self.assertEqual(statuses, ["skipped"])

    def test_install_base_packages_installs_missing_only(self):
        ctx = RunContext(dry_run=True)  # dry-run so no real dnf call happens
        cluster_cfg = {
            "packages": {"grp": ["present-pkg", "missing-pkg"]},
            "services": ["some.service"],
        }
        with mock.patch.object(
            packages_mod, "installed_packages", return_value={"present-pkg"}
        ):
            packages_mod.install_base_packages(ctx, cluster_cfg)
        install_events = [a for a in ctx.audit if a["step"] == "install_packages"]
        self.assertEqual(install_events[0]["status"], "ok")
        self.assertIn("1 package", install_events[0]["detail"])


class TestValidationReport(unittest.TestCase):
    def test_report_ok_true_when_all_pass(self):
        report = validation_mod.ValidationReport()
        report.add("a", True)
        report.add("b", True)
        self.assertTrue(report.ok)

    def test_report_ok_false_when_any_fail(self):
        report = validation_mod.ValidationReport()
        report.add("a", True)
        report.add("b", False, "detail here")
        self.assertFalse(report.ok)
        as_dict = report.as_dict()
        self.assertEqual(as_dict["ok"], False)
        self.assertEqual(as_dict["checks"][1]["detail"], "detail here")


class TestGpuIommuArgs(unittest.TestCase):
    def test_adds_missing_args_to_existing_cmdline(self):
        ctx = RunContext(dry_run=False)
        cluster_cfg = {
            "cpu_vendor": "intel",
            "iommu_kernel_args": {"intel": ["intel_iommu=on", "iommu=pt"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            grub_file = Path(tmp) / "grub"
            grub_file.write_text('GRUB_CMDLINE_LINUX="quiet crashkernel=auto"\n')

            with mock.patch("modules.gpu.run") as mock_run, \
                 mock.patch.object(gpu_mod, "log"):
                mock_run.return_value = mock.Mock(returncode=0)
                # Patch the hardcoded path by monkeypatching open via a
                # small wrapper: easier to just temporarily point the
                # function at our tmp file using the same code path.
                import builtins
                real_open = builtins.open

                def fake_open(path, mode="r", encoding=None):
                    if path == "/etc/default/grub":
                        return real_open(grub_file, mode, encoding=encoding)
                    return real_open(path, mode, encoding=encoding)

                with mock.patch.object(builtins, "open", fake_open):
                    changed = gpu_mod.ensure_iommu_kernel_args(ctx, cluster_cfg)

            self.assertTrue(changed)
            content = grub_file.read_text()
            self.assertIn("intel_iommu=on", content)
            self.assertIn("iommu=pt", content)
            self.assertIn("quiet crashkernel=auto", content)

    def test_noop_when_args_already_present(self):
        ctx = RunContext(dry_run=False)
        cluster_cfg = {
            "cpu_vendor": "intel",
            "iommu_kernel_args": {"intel": ["intel_iommu=on", "iommu=pt"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            grub_file = Path(tmp) / "grub"
            grub_file.write_text(
                'GRUB_CMDLINE_LINUX="quiet intel_iommu=on iommu=pt"\n'
            )
            import builtins
            real_open = builtins.open

            def fake_open(path, mode="r", encoding=None):
                if path == "/etc/default/grub":
                    return real_open(grub_file, mode, encoding=encoding)
                return real_open(path, mode, encoding=encoding)

            with mock.patch.object(builtins, "open", fake_open):
                changed = gpu_mod.ensure_iommu_kernel_args(ctx, cluster_cfg)
            self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
