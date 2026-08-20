"""requirements.txt must not drift from pyproject.toml.

Keeping both files is a convenience for people who expect `pip install -r`, but
two declarations of the same thing silently diverge — someone adds a dependency
to pyproject, the Docker build keeps working, and `pip install -r` breaks for
everyone else weeks later. This test is the thing that stops that.

pyproject.toml is the source of truth. If this fails, fix requirements.txt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _normalise(spec: str) -> str:
    """'uvicorn[standard]>=0.30' -> 'uvicorn'. Compare names, not versions."""
    return re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _read_requirements(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Skip comments, blanks, and `-r other.txt` includes.
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        names.add(_normalise(line))
    return names


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_runtime_requirements_match_pyproject(pyproject):
    declared = {_normalise(d) for d in pyproject["project"]["dependencies"]}
    listed = _read_requirements(ROOT / "requirements.txt")

    missing = declared - listed
    extra = listed - declared

    assert not missing, f"in pyproject but not requirements.txt: {sorted(missing)}"
    assert not extra, f"in requirements.txt but not pyproject: {sorted(extra)}"


def test_dev_requirements_match_pyproject(pyproject):
    declared = {_normalise(d) for d in pyproject["project"]["optional-dependencies"]["dev"]}
    listed = _read_requirements(ROOT / "requirements-dev.txt") - _read_requirements(
        ROOT / "requirements.txt"
    )

    missing = declared - listed
    assert not missing, f"in pyproject [dev] but not requirements-dev.txt: {sorted(missing)}"


def test_version_floors_agree(pyproject):
    """A floor raised in one file and not the other is the subtler failure."""
    def floors(specs) -> dict[str, str]:
        out = {}
        for spec in specs:
            match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?>=([\d.]+)", spec.strip())
            if match:
                out[match.group(1).lower().replace("_", "-")] = match.group(2)
        return out

    declared = floors(pyproject["project"]["dependencies"])
    listed = floors(
        line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    )

    mismatched = {
        name: (declared[name], listed[name])
        for name in declared.keys() & listed.keys()
        if declared[name] != listed[name]
    }
    assert not mismatched, f"version floors differ (pyproject, requirements.txt): {mismatched}"


def test_no_pinned_versions_in_runtime_requirements():
    """`==` here would force exact versions on anything installing alongside
    MemDream. Pin in a deployment lock file instead."""
    pinned = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.strip().startswith("#")
    ]
    assert not pinned, f"exact pins in requirements.txt: {pinned}"
