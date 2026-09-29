"""Dependency hygiene: declared ranges, requirements mirror, compatible pair."""

from __future__ import annotations

import importlib.metadata as md
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_dependencies() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_requirements_txt_mirrors_pyproject() -> None:
    declared = {Requirement(dep).name for dep in _pyproject_dependencies()}
    listed = {
        Requirement(line.split("#")[0].strip()).name
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert declared <= listed, f"requirements.txt is missing: {declared - listed}"


def test_declared_ranges_accept_installed_versions() -> None:
    """Every runtime dependency must be satisfied by what is actually installed."""
    for dep in _pyproject_dependencies():
        requirement = Requirement(dep)
        installed = md.version(requirement.name)
        assert requirement.specifier.contains(installed, prereleases=True), (
            f"{requirement.name} {installed} does not satisfy {requirement.specifier}"
        )


def test_fastapi_and_starlette_are_a_compatible_pair() -> None:
    """Regression: fastapi 0.110 + starlette 1.x is not importable (Router kwargs)."""
    fastapi_version = md.version("fastapi")
    starlette_version = md.version("starlette")
    fastapi_requires = md.requires("fastapi") or []
    starlette_req = next(
        (Requirement(req) for req in fastapi_requires if Requirement(req).name == "starlette"),
        None,
    )
    assert starlette_req is not None, "fastapi should declare its starlette range"
    assert starlette_req.specifier.contains(starlette_version, prereleases=True), (
        f"installed starlette {starlette_version} violates fastapi {fastapi_version} "
        f"({starlette_req.specifier}); pick a compatible pair instead of upgrading blindly"
    )


def test_tokenizer_dependency_is_declared_for_reproducible_metrics() -> None:
    """Regression: the tokenizer must be declared, not optional.

    Leaving jieba optional made the evaluation baseline environment-dependent
    (clean environment without it scored 30/35 where 35/35 was reported).
    """
    declared = {Requirement(dep).name.lower() for dep in _pyproject_dependencies()}
    assert "jieba" in declared
    assert md.version("jieba")


def test_build_metadata_and_dev_extra_are_self_contained() -> None:
    data = _pyproject()
    build_names = {Requirement(item).name.lower() for item in data["build-system"]["requires"]}
    dev_names = {Requirement(item).name.lower() for item in data["project"]["optional-dependencies"]["dev"]}
    assert {"setuptools", "wheel"} <= build_names
    assert {"pytest", "mypy", "ruff", "build", "wheel"} <= dev_names
    assert data["project"]["license"] == "MIT"
    assert data["project"]["license-files"] == ["LICENSE"]


def test_constraints_pin_only_declared_core_dependencies_to_compatible_versions() -> None:
    declared = {Requirement(item).name.lower(): Requirement(item) for item in _pyproject_dependencies()}
    constrained = [
        Requirement(line)
        for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert {item.name.lower() for item in constrained} >= {
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic-settings",
        "httpx",
    }
    assert {item.name.lower() for item in constrained} <= set(declared)
    for constraint in constrained:
        versions = list(constraint.specifier)
        assert len(versions) == 1 and versions[0].operator == "=="
        assert declared[constraint.name.lower()].specifier.contains(versions[0].version)
