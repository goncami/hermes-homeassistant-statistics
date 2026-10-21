"""Known vulnerable aiohttp versions must not satisfy the plugin declaration."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
import pytest


@pytest.mark.parametrize("version,permitted", [
    ("3.9.0", False), ("3.14.2", False), ("3.14.3", False),
    ("3.14.4", True), ("3.14.5", True), ("4.0.0", False),
])
def test_aiohttp_security_floor(version, permitted):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(Requirement(value) for value in project["project"]["dependencies"]
                       if Requirement(value).name == "aiohttp")
    assert requirement.specifier.contains(version) is permitted
