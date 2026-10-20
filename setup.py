"""Install only the exact wheel selected by a verified hook release."""

import json
import re
from pathlib import Path

from setuptools import setup

root = Path(__file__).parent
release = json.loads((root / "release.json").read_text(encoding="utf-8"))
if release.get("status") != "released":
    raise RuntimeError(
        "No compatible actionlint release has been published for these hooks yet."
    )
version = release["version"]
if (
    not isinstance(version, str)
    or re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    is None
):
    raise ValueError("Invalid pinned actionlint version")

setup(
    name="actionlint-pre-commit",
    version=version,
    description="Version-pinned actionlint pre-commit hooks",
    python_requires=">=3.10",
    install_requires=[f"actionlint-kjanat=={version}"],
    py_modules=[],
    license="MIT",
)
