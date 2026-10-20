"""Exercise pre-commit using a real host binary packaged as a local test wheel."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from scripts.release import HOOKS

PLUGIN_URL = "https://github.com/kjanat/actionlint-plugin-shellcheck/releases/download/v0.1.1/plugin.wasm"
PLUGIN_DIGEST = "c33638bb1fac6167e4dcd3a5fb7f59fcaa639c93d2287c4dda07f2beea3522b4"
TEST_VERSION = "0.0.0"


def command(args: list[str], cwd: Path, env: dict[str, str], expected: int = 0) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"{args[0:4]} exited {result.returncode}, expected {expected}:\n{result.stdout}"
        )
    return result.stdout


def wheel(binary: Path, directory: Path) -> None:
    """Test-only wheel, using the production launcher's process semantics."""
    tag = "win_amd64" if os.name == "nt" else "any"
    info = f"actionlint_kjanat-{TEST_VERSION}.dist-info"
    filename = "actionlint.exe" if os.name == "nt" else "actionlint"
    files = {
        "actionlint_kjanat/__init__.py": (
            "import os, subprocess, sys\n"
            "def main():\n"
            f"    binary = os.path.join(os.path.dirname(__file__), 'bin', '{filename}')\n"
            "    args = [binary, *sys.argv[1:]]\n"
            "    if os.name == 'nt':\n"
            "        sys.exit(subprocess.call(args))\n"
            "    os.execv(binary, args)\n"
        ).encode(),
        f"actionlint_kjanat/bin/{filename}": binary.read_bytes(),
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: actionlint-kjanat\nVersion: {TEST_VERSION}\n".encode(),
        f"{info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: hook-integration-test\nRoot-Is-Purelib: false\nTag: py3-none-{tag}\n".encode(),
        f"{info}/entry_points.txt": b"[console_scripts]\nactionlint = actionlint_kjanat:main\n",
    }
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    for name, data in files.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = records.getvalue().encode()
    with zipfile.ZipFile(
        directory / f"actionlint_kjanat-{TEST_VERSION}-py3-none-{tag}.whl",
        "w",
        zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, data in files.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = 0o100755 << 16 if "/bin/" in name else 0o100644 << 16
            archive.writestr(entry, data)


def exercise(binary: Path, hook_root: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="actionlint-hook-integration-"
    ) as temporary:
        base = Path(temporary)
        package = base / "hooks"
        package.mkdir()
        for filename in ("pyproject.toml", "setup.py", "MANIFEST.in", "LICENSE"):
            shutil.copyfile(hook_root / filename, package / filename)
        (package / ".pre-commit-hooks.yaml").write_text(HOOKS, encoding="utf-8")
        (package / "release.json").write_text(
            json.dumps({"status": "released", "version": TEST_VERSION}),
            encoding="utf-8",
        )
        wheels = base / "wheels"
        wheels.mkdir()
        wheel(binary, wheels)
        env = dict(os.environ)
        env.update(
            {
                "PRE_COMMIT_HOME": str(base / "pre-commit-cache"),
                "XDG_CACHE_HOME": str(base / "cache"),
                "LOCALAPPDATA": str(base / "local"),
                "PIP_FIND_LINKS": str(wheels),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(base / "empty-git-config"),
            }
        )
        (base / "empty-git-config").write_text("", encoding="utf-8")
        command(["git", "init", "-b", "master"], package, env)
        command(["git", "add", "."], package, env)
        command(
            [
                "git",
                "-c",
                "user.name=Hook test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "Local integration fixture",
            ],
            package,
            env,
        )
        revision = command(["git", "rev-parse", "HEAD"], package, env).strip()
        project = base / "project"
        project.mkdir()
        command(["git", "init", "-b", "master"], project, env)
        configuration = {
            "repos": [
                {"repo": str(package), "rev": revision, "hooks": [{"id": "actionlint"}]}
            ],
        }
        (project / ".pre-commit-config.yaml").write_text(
            json.dumps(configuration), encoding="utf-8"
        )
        precommit = [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            "actionlint",
            "--all-files",
            "--verbose",
        ]
        # Empty repositories must succeed, including when no filenames are supplied.
        command(precommit, project, env)
        workflows = project / ".github" / "workflows"
        workflows.mkdir(parents=True)
        config = project / ".github" / "actionlint.yaml"
        config.write_text(
            json.dumps(
                {
                    "plugins": [f"{PLUGIN_URL}#sha256={PLUGIN_DIGEST}"],
                    "lint": {"rules": {"plugins": {"shellcheck-rs": "error"}}},
                }
            ),
            encoding="utf-8",
        )
        workflow = workflows / "ci.yml"
        workflow.write_text(
            "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo $GITHUB_SHA\n",
            encoding="utf-8",
        )
        command(["git", "add", "."], project, env)
        cold = command(precommit, project, env, 1)
        if "SC2086" not in cold:
            raise RuntimeError("Real Wasm plugin did not produce SC2086:\n" + cold)
        # Same bytes/digest, impossible origin: only the warm artifact cache can pass.
        config.write_text(
            json.dumps(
                {
                    "plugins": [
                        f"https://127.0.0.1:1/plugin.wasm#sha256={PLUGIN_DIGEST}"
                    ],
                    "lint": {"rules": {"plugins": {"shellcheck-rs": "error"}}},
                }
            ),
            encoding="utf-8",
        )
        warm = command(precommit, project, env, 1)
        if "SC2086" not in warm:
            raise RuntimeError(
                "Warm Wasm cache did not preserve the diagnostic:\n" + warm
            )
        # A deleted referenced reusable workflow must recheck its unchanged caller.
        config.write_text("plugins: []\n", encoding="utf-8")
        workflow.write_text(
            "on: push\njobs:\n  test:\n    uses: ./.github/workflows/reusable.yml\n",
            encoding="utf-8",
        )
        reusable = workflows / "reusable.yml"
        reusable.write_text(
            "on: workflow_call\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
            encoding="utf-8",
        )
        command(["git", "add", "."], project, env)
        command(precommit, project, env)
        command(
            [
                "git",
                "-c",
                "user.name=Hook test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "Workflow fixture",
            ],
            project,
            env,
        )
        command(["git", "rm", ".github/workflows/reusable.yml"], project, env)
        deleted = command(
            [sys.executable, "-m", "pre_commit", "run", "actionlint", "--verbose"],
            project,
            env,
            1,
        )
        if "reusable.yml" not in deleted or "Skipped" in deleted:
            raise RuntimeError(
                "Deletion-only commit did not recheck the caller:\n" + deleted
            )
        print(
            "PASS: actual wheel install, empty repo, Wasm diagnostic, warm cache, deletion-only caller check"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--hook-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    exercise(args.binary.resolve(), args.hook_root.resolve())
