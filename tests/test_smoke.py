"""Smoke tests: prove the package installs and imports end-to-end."""

import importlib

import pkm_sidecar

# Modules reserved by the scaffold (PRD §6). All must be importable as empty
# stubs so later subsystems can fill them in without import churn.
RESERVED_MODULES = [
    "pkm_sidecar.cli",
    "pkm_sidecar.config",
    "pkm_sidecar.logging_config",
    "pkm_sidecar.app",
    "pkm_sidecar.security",
    "pkm_sidecar.joplin_client",
    "pkm_sidecar.indexer",
    "pkm_sidecar.markdown_extract",
    "pkm_sidecar.db",
    "pkm_sidecar.repositories",
    "pkm_sidecar.services",
    "pkm_sidecar.views",
    "pkm_sidecar.dashboard",
    "pkm_sidecar.models",
    "pkm_sidecar.errors",
]


def test_version_is_set() -> None:
    assert pkm_sidecar.__version__ == "0.2.2"


def test_reserved_modules_importable() -> None:
    for name in RESERVED_MODULES:
        assert importlib.import_module(name) is not None


def test_cli_app_exists() -> None:
    from pkm_sidecar.cli import app

    assert app is not None


def test_schema_sql_is_packaged() -> None:
    from importlib.resources import files

    schema = files("pkm_sidecar").joinpath("schema.sql")
    assert schema.is_file()
