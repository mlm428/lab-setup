"""
Assembles the runtime configuration workers/deploy.py and
workers/teardown.py need (OVN connection info, runtime storage backend,
golden image source/catalog) from config/hosts.yaml and
config/storage.yaml -- the single sources of truth shared with
bootstrap/. Centralized here so the API layer and the workers agree on
exactly one way to build this, rather than each parsing YAML slightly
differently.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core import config_loader
from core.xml_render import StorageContext
from services.storage import GoldenImageSource


@dataclass
class DeploymentConfig:
    """Everything workers/deploy.py and workers/teardown.py need beyond the mission spec itself and the host inventory.

    Attributes:
        ovn_nb_connection: OVN Northbound DB connection string (e.g. "tcp:compute01.cluster.local:6641").
        ovn_integration_bridge: Host-side OVS bridge every VM NIC binds to (normally "br-int").
        runtime: Runtime (per-VM clone disk) storage backend/connection info.
        golden: Golden (source) image backend/connection info + catalog.
        management_ssh_user: Non-root SSH user the management service uses
            to reach every compute host's libvirt socket (see
            config/hosts.yaml's management_ssh_user and
            clients/libvirt_client.py:connect's docstring for why this is
            deliberately not "root").
    """
    ovn_nb_connection: str
    ovn_integration_bridge: str
    runtime: StorageContext
    golden: GoldenImageSource
    management_ssh_user: str = "root"


def load_deployment_config(config_dir: Path | None = None) -> DeploymentConfig:
    """
    Build a DeploymentConfig from config/hosts.yaml + config/storage.yaml.

    Args:
        config_dir: Override for the config/ directory (defaults to the
            repo-root config/).

    Returns:
        A populated DeploymentConfig.
    """
    hosts_cfg = config_loader.load_hosts_config(config_dir)
    storage_cfg = config_loader.load_storage_config(config_dir)

    ovn_central = hosts_cfg["ovn_central"]

    runtime_cfg = storage_cfg["runtime"]
    if runtime_cfg["backend"] == "ceph_rbd":
        ceph = runtime_cfg["ceph"]
        runtime = StorageContext(
            backend="ceph_rbd",
            ceph_pool=ceph["pool"],
            ceph_client_id=ceph["client_id"],
            ceph_secret_uuid=ceph["libvirt_secret_uuid"],
            ceph_monitors=[{"name": m["name"], "port": m["port"]} for m in hosts_cfg.get("ceph_monitors", [])],
        )
    else:
        runtime = StorageContext(backend="local_qcow2", local_qcow2_dir=runtime_cfg["local_qcow2"]["pool_path"])

    golden_cfg = storage_cfg["golden"]
    golden = GoldenImageSource(
        source=golden_cfg["source"],
        ceph_rbd_local_pool=golden_cfg.get("ceph_rbd_local", {}).get("pool", "golden-images"),
        remote_conf_path=golden_cfg.get("ceph_rbd_remote", {}).get("conf_path", "/etc/ceph/golden.ceph.conf"),
        remote_client_id=golden_cfg.get("ceph_rbd_remote", {}).get("client_id", "golden-reader"),
        remote_pool=golden_cfg.get("ceph_rbd_remote", {}).get("pool", "golden-images"),
        mount_path=golden_cfg.get("mount", {}).get("path", "/mnt/golden-images"),
        images=golden_cfg.get("images", {}),
    )

    return DeploymentConfig(
        ovn_nb_connection=ovn_central["nb_connection"],
        ovn_integration_bridge=ovn_central.get("integration_bridge", "br-int"),
        runtime=runtime,
        golden=golden,
        management_ssh_user=hosts_cfg.get("management_ssh_user", "root"),
    )
