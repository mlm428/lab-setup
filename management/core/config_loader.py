"""
Loaders for the top-level config/ directory -- the single source of truth
for host inventory, cluster settings, storage settings, and the network
catalog, shared by BOTH bootstrap/ and management/. An earlier version of
this project kept separate copies of this data under bootstrap/config/ and
management/inventory/ that had to be hand-synced; that duplication has
been removed in favor of one location, loaded from here.

Kept dependency-free (stdlib + PyYAML only), like the rest of core/, so
it's testable without fastapi/pydantic/libvirt/ovsdbapp/rados installed.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# repo_root/config -- this file lives at repo_root/management/core/config_loader.py
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_yaml(path: Path) -> dict:
    """
    Read and parse one YAML file.

    Args:
        path: Absolute or relative filesystem path to a YAML document.

    Returns:
        The parsed document as a dict.

    Raises:
        FileNotFoundError: if `path` does not exist.
        yaml.YAMLError: if `path` is not valid YAML.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_hosts_config(config_dir: Path | None = None) -> dict:
    """Load config/hosts.yaml (host inventory, Ceph monitors, OVN central)."""
    return _load_yaml((config_dir or CONFIG_DIR) / "hosts.yaml")


def load_cluster_config(config_dir: Path | None = None) -> dict:
    """Load config/cluster.yaml (packages, services, kernel/GRUB args, STIG profile)."""
    return _load_yaml((config_dir or CONFIG_DIR) / "cluster.yaml")


def load_storage_config(config_dir: Path | None = None) -> dict:
    """Load config/storage.yaml (runtime pool + golden image source/catalog)."""
    return _load_yaml((config_dir or CONFIG_DIR) / "storage.yaml")


def load_networks_config(config_dir: Path | None = None) -> dict:
    """Load config/networks.yaml (the predefined mission-overlay network catalog)."""
    return _load_yaml((config_dir or CONFIG_DIR) / "networks.yaml")
