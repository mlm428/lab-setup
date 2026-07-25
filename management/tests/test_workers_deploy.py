"""
Tests workers/deploy.py's and workers/teardown.py's orchestration logic --
the four-step "Create Networks / Clone Disks / Define VMs / Validate"
workflow from the design doc -- by mocking out the services layer
(networking/storage/compute/validation) rather than the real
libvirt/OVN/Ceph backends those services eventually call. This isolates
exactly what this file is responsible for: calling the right steps in the
right order, threading MAC/placement/GPU data between them correctly, and
transitioning mission state (Pending -> ...-> Running, or -> Error) the
way the design doc specifies ("abort on first failure").

core/xml_render.py and core/macs.py (the actual hard logic) are already
covered end-to-end by test_xml_render.py, test_macs.py, and
test_mission_pipeline.py without any mocking -- this file is specifically
about deploy.py/teardown.py's control flow.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import MissionStore
from core.types import HostSpec, MissionSpec, MissionState, VMSpec, VMType
import workers.deploy as deploy_mod
import workers.teardown as teardown_mod


def make_mission():
    vms = {
        "db01": VMSpec(
            name="db01", type=VMType.LINKED_CLONE, cpu=4, memory_mb=16384,
            interfaces=["control", "storage"], image="rhel9-db-golden",
        ),
        "render01": VMSpec(
            name="render01", type=VMType.PXE, cpu=8, memory_mb=32768,
            interfaces=["control", "storage"], gpu=True,
        ),
    }
    return MissionSpec(
        name="Mission-Test",
        networks={"control": 100, "storage": 110},
        vms=vms,
        placement={"db01": "compute01", "render01": "compute01"},
    )


def make_hosts():
    return {
        "compute01": HostSpec(
            name="compute01", cpus=64, memory_mb=262144, gpus=2,
            address="compute01.cluster.local", gpu_pci_addresses=["0000:81:00.0", "0000:82:00.0"],
        ),
    }


CLUSTER_CFG = {
    "ovn": {"nb_connection": "tcp:fake:6641", "integration_bridge": "br-int"},
    "storage": {
        "backend": "ceph_rbd",
        "ceph": {
            "conf_path": "/etc/ceph/ceph.conf",
            "client_id": "libvirt",
            "pool": "mission-images",
            "secret_uuid": "5e4b8c1a-9f2d-4e3a-8b7a-1c2d3e4f5a6b",
            "monitors": [{"name": "ceph-mon1", "port": 6789}],
        },
    },
}


class TestDeployMissionHappyPath(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()
        deploy_mod.store = self.store  # point the module-level singleton at a fresh store per test

    def test_full_success_transitions_to_running(self):
        mission = make_mission()
        hosts = make_hosts()
        status = self.store.create(mission.name, spec=mission)

        fake_api = mock.Mock()
        assigned_macs = {
            "db01": ["52:54:00:00:00:00", "52:54:00:00:00:01"],
            "render01": ["52:54:00:00:01:00", "52:54:00:00:01:01"],
        }

        with mock.patch.object(deploy_mod.ovn_client, "connect", return_value=fake_api), \
             mock.patch.object(deploy_mod.networking, "provision_networks") as m_prov_net, \
             mock.patch.object(deploy_mod.networking, "add_vm_ports", return_value=assigned_macs) as m_add_ports, \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage") as m_storage, \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm") as m_define, \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=[]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value = mock.Mock(ok=True, as_dict=lambda: {"ok": True, "issues": []})

            deploy_mod.deploy_mission(status.mission_id, mission, hosts, CLUSTER_CFG)

        m_prov_net.assert_called_once_with(fake_api, mission)
        m_add_ports.assert_called_once_with(fake_api, mission)
        m_storage.assert_called_once()
        self.assertEqual(m_define.call_count, 2)  # one per VM

        # The GPU VM must have received exactly one PCI address, pulled
        # from compute01's pool; the non-GPU VM must have received None.
        calls_by_vm = {c.kwargs["vm_name"]: c.kwargs for c in m_define.call_args_list}
        self.assertIsNone(calls_by_vm["db01"]["gpu_pci_addresses"])
        self.assertEqual(calls_by_vm["render01"]["gpu_pci_addresses"], ["0000:81:00.0"])
        self.assertEqual(calls_by_vm["db01"]["macs"], assigned_macs["db01"])
        self.assertEqual(calls_by_vm["render01"]["macs"], assigned_macs["render01"])

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.RUNNING)

    def test_no_free_gpu_marks_mission_error(self):
        mission = make_mission()
        hosts = make_hosts()
        hosts["compute01"].gpu_pci_addresses = []  # no GPUs actually available
        status = self.store.create(mission.name, spec=mission)

        with mock.patch.object(deploy_mod.ovn_client, "connect", return_value=mock.Mock()), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports", return_value={
                 "db01": ["a", "b"], "render01": ["c", "d"],
             }), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage"), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm") as m_define:
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, CLUSTER_CFG)

        m_define.assert_called_once()  # db01 (no GPU, processed first) is legitimately defined
        # before render01's missing-GPU failure aborts the run -- "abort on
        # first failure" per the design doc means partial state can exist,
        # not that nothing runs before the failing step.
        self.assertEqual(m_define.call_args.kwargs["vm_name"], "db01")
        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("no free GPU", final.error)

    def test_validation_failure_marks_mission_error(self):
        mission = make_mission()
        hosts = make_hosts()
        status = self.store.create(mission.name, spec=mission)

        with mock.patch.object(deploy_mod.ovn_client, "connect", return_value=mock.Mock()), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports", return_value={
                 "db01": ["a", "b"], "render01": ["c", "d"],
             }), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage"), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm"), \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=["control"]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value = mock.Mock(ok=True, as_dict=lambda: {"ok": True, "issues": []})
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, CLUSTER_CFG)

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("missing_networks", final.error)


class TestTeardownMission(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()
        teardown_mod.store = self.store

    def test_teardown_destroys_vms_networks_and_storage(self):
        mission = make_mission()
        hosts = make_hosts()
        status = self.store.create(mission.name, spec=mission)

        with mock.patch.object(teardown_mod.compute, "destroy_vm") as m_destroy, \
             mock.patch.object(teardown_mod.ovn_client, "connect", return_value=mock.Mock()), \
             mock.patch.object(teardown_mod.networking, "teardown_mission_networking") as m_net_teardown, \
             mock.patch.object(teardown_mod.storage_service, "teardown_mission_storage") as m_storage_teardown:
            teardown_mod.teardown_mission(status.mission_id, mission, hosts, CLUSTER_CFG)

        self.assertEqual(m_destroy.call_count, 2)
        m_net_teardown.assert_called_once()
        m_storage_teardown.assert_called_once()
        self.assertEqual(self.store.get(status.mission_id).state, MissionState.DESTROYED)

    def test_teardown_failure_marks_error_not_destroyed(self):
        mission = make_mission()
        hosts = make_hosts()
        status = self.store.create(mission.name, spec=mission)

        with mock.patch.object(teardown_mod.compute, "destroy_vm", side_effect=RuntimeError("host unreachable")):
            teardown_mod.teardown_mission(status.mission_id, mission, hosts, CLUSTER_CFG)

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("host unreachable", final.error)


if __name__ == "__main__":
    unittest.main()
