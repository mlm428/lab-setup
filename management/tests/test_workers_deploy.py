"""
Tests workers/deploy.py's and workers/teardown.py's orchestration logic --
the four-step "Create Networks / Clone Disks / Define VMs / Validate"
workflow from the design doc -- by mocking out the services layer
(networking/storage/compute/validation) rather than the real
libvirt/OVN/Ceph backends those services eventually call. This isolates
exactly what this file is responsible for: calling the right steps in the
right order, threading MAC/placement/GPU-allocation data between them
correctly, and transitioning mission state (Pending -> ... -> Running, or
-> Error) the way the design doc specifies ("abort on first failure").

GPU device allocation itself (matching a VM's requested profile to a
specific free mdev UUID, checked across every other currently-active
deployment) now happens at REGISTRATION time
(core/state.py:MissionStore.register_deployment,
core/placement.py:reserve_gpu_devices) -- deploy_mission only reads the
already-resolved allocation from the mission's status and attaches it.
That resolution logic is covered by test_placement.py, test_state.py, and
test_missions_service.py; this file covers deploy_mission's own defensive
fallback (a GPU VM with no allocation recorded, which should never happen
given registration's own check, but is guarded against anyway) and its
control flow generally.

core/xml_render.py and core/macs.py (the actual hard logic) are already
covered end-to-end by test_xml_render.py, test_macs.py, and
test_mission_pipeline.py without any mocking.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.placement import reserve_gpu_devices
from core.state import MissionStore
from core.types import GpuDeviceSpec, HostSpec, InterfaceSpec, MissionSpec, MissionState, VMSpec, VMType
from core.xml_render import StorageContext
from services.cluster_config import DeploymentConfig
from services.storage import GoldenImageSource
import workers.deploy as deploy_mod
import workers.teardown as teardown_mod


def ifaces(*names):
    return {n: InterfaceSpec(network=n, mac_suffix=None) for n in names}


def make_deployment_cfg():
    return DeploymentConfig(
        ovn_nb_connection="tcp:host:6641",
        ovn_integration_bridge="br-int",
        runtime=StorageContext(backend="ceph_rbd", ceph_pool="mission-runtime"),
        golden=GoldenImageSource(source="ceph_rbd_local", images={}),
    )


class TestDeployMission(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()
        self.store_patch = mock.patch.object(deploy_mod, "store", self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def _register(self, mission, hosts, prefix="aa:bb:cc"):
        """Register a mission the same way services.missions.register_mission does -- real MAC + GPU resolution, so deploy_mission gets a fully realistic, pre-resolved status."""
        def fake_mac_resolver(m, existing):
            return prefix, {name: [f"{prefix}:{i:02x}:{j:02x}:00" for j in range(len(vm.interfaces))] for i, (name, vm) in enumerate(m.vms.items())}
        return self.store.register_deployment(mission.name, mission, hosts, fake_mac_resolver, reserve_gpu_devices)

    def test_happy_path_reaches_running(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}
        status = self._register(mission, hosts)

        with mock.patch.object(deploy_mod, "ovn_client") as m_ovn, \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports"), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage", return_value={}), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm"), \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=[]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value.ok = True
            m_validate.return_value.as_dict.return_value = {"ok": True, "issues": []}
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        self.assertEqual(self.store.get(status.mission_id).state, MissionState.RUNNING)

    def test_failure_in_networking_marks_error_and_stops(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}
        status = self._register(mission, hosts)

        with mock.patch.object(deploy_mod, "ovn_client") as m_ovn, \
             mock.patch.object(deploy_mod.networking, "provision_networks", side_effect=RuntimeError("OVN down")), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage") as m_storage:
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("OVN down", final.error)
        m_storage.assert_not_called()  # abort on first failure -- storage step never reached

    def test_validation_failure_marks_error(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}
        status = self._register(mission, hosts)

        with mock.patch.object(deploy_mod, "ovn_client"), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports"), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage", return_value={}), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm"), \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=[]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value.ok = False
            m_validate.return_value.as_dict.return_value = {"ok": False, "issues": [{"vm": "vm1", "check": "nic_count", "detail": "mismatch"}]}
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        self.assertEqual(self.store.get(status.mission_id).state, MissionState.ERROR)

    def test_gpu_vm_uses_allocation_resolved_at_registration(self):
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"render01": vm}, placement={"render01": "h1"})
        hosts = {
            "h1": HostSpec(
                name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local",
                gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="uuid-abc")],
            )
        }
        status = self._register(mission, hosts)
        self.assertEqual(status.gpu_allocations, {"render01": "uuid-abc"})  # resolved at registration, not deploy time

        with mock.patch.object(deploy_mod, "ovn_client"), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports"), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage", return_value={}), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm") as m_define, \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=[]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value.ok = True
            m_validate.return_value.as_dict.return_value = {"ok": True, "issues": []}
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        m_define.assert_called_once()
        self.assertEqual(m_define.call_args.kwargs["gpu_mdev_uuid"], "uuid-abc")
        self.assertEqual(self.store.get(status.mission_id).state, MissionState.RUNNING)

    def test_no_matching_gpu_profile_fails_at_registration_not_deploy(self):
        # The "no free slice" failure now surfaces at registration time
        # (reserve_gpu_devices raises), before deploy_mission is ever
        # invoked -- confirming _register (== services.missions.register_mission's
        # real code path) itself raises, rather than silently producing a
        # status deploy_mission would later choke on.
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"render01": vm}, placement={"render01": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local", gpu_devices=[])}

        with self.assertRaises(RuntimeError):
            self._register(mission, hosts)
        self.assertEqual(self.store.list(), [])

    def test_deploy_mission_defensive_check_if_allocation_somehow_missing(self):
        # Defense-in-depth: if a GPU VM's status ever reached deploy_mission
        # with no recorded allocation (which registration's own
        # reserve_gpu_devices call should always prevent), deploy_mission
        # must fail loudly and clearly rather than passing gpu_mdev_uuid=None
        # to compute.define_and_start_vm silently.
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="H100-MIG-3g.40gb")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"render01": vm}, placement={"render01": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local", gpu_devices=[GpuDeviceSpec(profile="H100-MIG-3g.40gb", mdev_uuid="uuid-abc")])}

        status = self._register(mission, hosts)
        status.gpu_allocations = {}  # simulate the "should never happen" case directly

        with mock.patch.object(deploy_mod, "ovn_client"), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports"), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage", return_value={}):
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("render01", final.error)

    def test_two_gpu_vms_same_host_get_different_mdev_uuids(self):
        vm1 = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="X")
        vm2 = VMSpec(name="render02", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="X")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"render01": vm1, "render02": vm2}, placement={"render01": "h1", "render02": "h1"})
        hosts = {
            "h1": HostSpec(
                name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local",
                gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="uuid-1"), GpuDeviceSpec(profile="X", mdev_uuid="uuid-2")],
            )
        }
        status = self._register(mission, hosts)
        self.assertEqual(set(status.gpu_allocations.values()), {"uuid-1", "uuid-2"})

        with mock.patch.object(deploy_mod, "ovn_client"), \
             mock.patch.object(deploy_mod.networking, "provision_networks"), \
             mock.patch.object(deploy_mod.networking, "add_vm_ports"), \
             mock.patch.object(deploy_mod.storage_service, "provision_mission_storage", return_value={}), \
             mock.patch.object(deploy_mod.compute, "define_and_start_vm") as m_define, \
             mock.patch.object(deploy_mod.validation, "validate_networks", return_value=[]), \
             mock.patch.object(deploy_mod.validation, "validate_mission_deployment") as m_validate:
            m_validate.return_value.ok = True
            m_validate.return_value.as_dict.return_value = {"ok": True, "issues": []}
            deploy_mod.deploy_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        used_uuids = {call.kwargs["gpu_mdev_uuid"] for call in m_define.call_args_list}
        self.assertEqual(used_uuids, {"uuid-1", "uuid-2"})

    def test_two_missions_competing_for_last_gpu_slice_second_registration_fails(self):
        # End-to-end regression test for the cross-deployment GPU
        # contention bug: two DIFFERENT missions, only one physical slice
        # between them -- the second registration must fail cleanly
        # rather than both succeeding and colliding at deploy time.
        vm = VMSpec(name="render01", type=VMType.PXE, cpu=8, memory_mb=32768, interfaces=ifaces("control"), gpu_profile="X")
        mission_a = MissionSpec(name="Mission-A", networks={"control": 100}, vms={"render01": vm}, placement={"render01": "h1"})
        mission_b = MissionSpec(name="Mission-B", networks={"control": 100}, vms={"render01": vm}, placement={"render01": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="only-uuid")])}

        status_a = self._register(mission_a, hosts)
        self.assertEqual(status_a.gpu_allocations, {"render01": "only-uuid"})

        with self.assertRaises(RuntimeError):
            self._register(mission_b, hosts)


class TestTeardownMission(unittest.TestCase):
    def setUp(self):
        self.store = MissionStore()
        self.store_patch = mock.patch.object(teardown_mod, "store", self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def test_happy_path_reaches_destroyed(self):
        vm = VMSpec(name="vm1", type=VMType.LINKED_CLONE, cpu=1, memory_mb=1024, interfaces=ifaces("control"), image="golden")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        status = self.store.create("M", mission)
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}

        with mock.patch.object(teardown_mod.compute, "destroy_vm") as m_destroy, \
             mock.patch.object(teardown_mod, "ovn_client"), \
             mock.patch.object(teardown_mod.networking, "teardown_mission_networking"), \
             mock.patch.object(teardown_mod.storage_service, "teardown_mission_storage") as m_storage_teardown:
            teardown_mod.teardown_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        m_destroy.assert_called_once_with("vm1", "h1.cluster.local")
        m_storage_teardown.assert_called_once()
        self.assertEqual(self.store.get(status.mission_id).state, MissionState.DESTROYED)

    def test_destroyed_mission_frees_mac_prefix_and_gpu_allocations(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"), gpu_profile="X")
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local", gpu_devices=[GpuDeviceSpec(profile="X", mdev_uuid="uuid-1")])}
        status = self.store.register_deployment(
            "M", mission, hosts,
            lambda m, existing: ("aa:bb:cc", {"vm1": ["aa:bb:cc:00:00:00"]}),
            reserve_gpu_devices,
        )
        self.assertEqual(self.store.active_mac_prefixes(), {"aa:bb:cc"})
        self.assertEqual(self.store.active_gpu_allocations(), {"uuid-1"})

        with mock.patch.object(teardown_mod.compute, "destroy_vm"), \
             mock.patch.object(teardown_mod, "ovn_client"), \
             mock.patch.object(teardown_mod.networking, "teardown_mission_networking"), \
             mock.patch.object(teardown_mod.storage_service, "teardown_mission_storage"):
            teardown_mod.teardown_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        self.assertEqual(self.store.active_mac_prefixes(), set())
        self.assertEqual(self.store.active_gpu_allocations(), set())

    def test_failure_marks_error(self):
        vm = VMSpec(name="vm1", type=VMType.PXE, cpu=1, memory_mb=1024, interfaces=ifaces("control"))
        mission = MissionSpec(name="M", networks={"control": 100}, vms={"vm1": vm}, placement={"vm1": "h1"})
        status = self.store.create("M", mission)
        hosts = {"h1": HostSpec(name="h1", cpus=64, memory_mb=262144, address="h1.cluster.local")}

        with mock.patch.object(teardown_mod.compute, "destroy_vm", side_effect=RuntimeError("libvirt error")):
            teardown_mod.teardown_mission(status.mission_id, mission, hosts, make_deployment_cfg())

        final = self.store.get(status.mission_id)
        self.assertEqual(final.state, MissionState.ERROR)
        self.assertIn("libvirt error", final.error)


if __name__ == "__main__":
    unittest.main()
