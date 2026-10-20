"""Verify upstream publication before preparing or publishing immutable hook tags.

All remote inputs are data. This module never checks out or runs upstream source.
The source candidate checks follow actionlint's MIT-licensed release-candidate.mjs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

SOURCE = "kjanat/actionlint"
SOURCE_ID = "1326162482"
DESTINATION = "kjanat/actionlint-pre-commit"
PREPARE_WORKFLOW = ".github/workflows/release-prepare.yml"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
SHA = r"[a-f0-9]{40}"
DIGEST = r"[a-f0-9]{64}"
HOOKS = """- id: actionlint
  name: Lint GitHub Actions workflow files
  language: python
  entry: actionlint pre-commit
  pass_filenames: false
  always_run: true
  minimum_pre_commit_version: "3.0.0"
- id: actionlint-system
  name: Lint GitHub Actions workflow files (system)
  language: system
  entry: actionlint pre-commit
  pass_filenames: false
  always_run: true
"""


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        fail(f"Invalid {label}")
    return dict(value)


def text(value: object, pattern: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        fail(f"Invalid {label}")
    return value


def number(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        fail(f"Invalid {label}")
    return value


def sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"Invalid {label}")
    return value


@dataclass(frozen=True)
class Request:
    release_id: int
    source_sha: str
    operation_id: str

    @classmethod
    def parse(
        cls, repository_id: str, release_id: str, source_sha: str, operation_id: str
    ) -> Request:
        if repository_id != SOURCE_ID:
            fail("Source repository is not authorized")
        return cls(
            int(text(release_id, r"[1-9][0-9]*", "release ID")),
            text(source_sha, SHA, "source commit"),
            text(operation_id, r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", "operation ID"),
        )


def run(
    *args: str,
    cwd: Path | None = None,
    data: bytes | None = None,
    timeout: float = 120,
    limit: int = 2 * 1024 * 1024,
) -> bytes:
    """Capture bounded metadata/signatures, terminating stalled or noisy commands."""
    if data is not None and len(data) > limit:
        fail("Command input exceeds the metadata limit")
    with tempfile.TemporaryFile() as source:
        if data is not None:
            source.write(data)
        source.seek(0)
        with subprocess.Popen(
            args,
            cwd=cwd,
            stdin=source,
            stdout=subprocess.PIPE,
            start_new_session=os.name != "nt",
        ) as process:
            timed_out = threading.Event()
            termination_lock = threading.Lock()
            termination_sent = False

            def terminate() -> None:
                nonlocal termination_sent
                with termination_lock:
                    if termination_sent or process.poll() is not None:
                        return
                    # A killed, unreaped process can still report no exit status.
                    # Never signal its group a second time during final cleanup.
                    termination_sent = True
                    try:
                        if os.name == "nt":
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            def expire() -> None:
                timed_out.set()
                terminate()

            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                output = bytearray()
                if process.stdout is None:
                    fail("Command output pipe is unavailable")
                while chunk := process.stdout.read1(65536):
                    if len(output) + len(chunk) > limit:
                        terminate()
                        fail("Command output exceeds the metadata limit")
                    output.extend(chunk)
                code = process.wait()
                if timed_out.is_set():
                    raise subprocess.TimeoutExpired(args, timeout)
                if code:
                    raise subprocess.CalledProcessError(code, args)
                return bytes(output)
            finally:
                timer.cancel()
                terminate()
                timer.join()


def github(path: str) -> object:
    return json.loads(run("gh", "api", path))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_release(
    value: object, request: Request
) -> tuple[str, dict[str, dict[str, object]]]:
    release = record(value, "release")
    tag = text(release.get("tag_name"), "v" + VERSION, "stable release tag")
    if (
        release.get("id") != request.release_id
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not release.get("published_at")
    ):
        fail("Release is not the requested published stable release")
    assets: dict[str, dict[str, object]] = {}
    for item in sequence(release.get("assets"), "release assets"):
        asset = record(item, "release asset")
        name = text(asset.get("name"), r"[A-Za-z0-9][A-Za-z0-9._-]*", "asset name")
        number(asset.get("id"), "asset ID")
        if name in assets or asset.get("state") != "uploaded":
            fail("Duplicate or incomplete release asset")
        assets[name] = asset
    if "release-candidate.json" not in assets:
        fail("Release predates the required verified candidate format")
    return tag[1:], assets


def verify_tag(
    ref_value: object, tag_value: object, request: Request, version: str
) -> None:
    ref = record(ref_value, "tag reference")
    obj = record(ref.get("object"), "tag reference object")
    tag = record(tag_value, "annotated tag")
    commit = record(tag.get("object"), "tag commit")
    verification = record(tag.get("verification"), "tag verification")
    if (
        obj.get("type") != "tag"
        or obj.get("sha") != tag.get("sha")
        or tag.get("tag") != "v" + version
        or commit.get("type") != "commit"
        or commit.get("sha") != request.source_sha
        or verification.get("verified") is not True
        or not isinstance(verification.get("signature"), str)
        or "-----BEGIN PGP SIGNATURE-----" not in verification["signature"]
    ):
        fail("Release needs a verified GPG annotated tag at the requested commit")


def release_evidence(
    release: object,
    ref: object,
    request: Request,
) -> dict[str, object]:
    """Only identity-bearing fields; download counters are deliberately excluded."""
    version, assets = verify_release(release, request)
    tag = record(record(ref, "tag reference").get("object"), "tag object")
    if tag.get("type") != "tag":
        fail("Release tag is no longer annotated")
    return {
        "tag_object_sha": text(tag.get("sha"), SHA, "tag object SHA"),
        "version": version,
        "assets": [
            {
                "name": name,
                "id": number(asset.get("id"), "asset ID"),
                "size": number(asset.get("size"), "asset size"),
                "digest": text(
                    asset.get("digest"), "sha256:" + DIGEST, "GitHub asset digest"
                ),
            }
            for name, asset in sorted(assets.items())
        ],
    }


def recheck_source(
    request: Request,
    expected: dict[str, object],
    manifest_digest: str,
) -> None:
    repository = record(github(f"repos/{SOURCE}"), "source repository")
    if repository.get("id") != int(SOURCE_ID):
        fail("Source repository identity changed during verification")
    release = github(f"repos/{SOURCE}/releases/{request.release_id}")
    version, assets = verify_release(release, request)
    ref = github(f"repos/{SOURCE}/git/ref/tags/v{version}")
    actual = release_evidence(release, ref, request)
    if actual != expected:
        fail("Source release, tag or assets changed during verification")
    verify_tag(
        ref,
        github(f"repos/{SOURCE}/git/tags/{actual['tag_object_sha']}"),
        request,
        version,
    )
    if sha256(fetch_asset(assets["release-candidate.json"])) != manifest_digest:
        fail("Source manifest bytes changed during verification")


def verify_manifest(
    value: object, request: Request, version: str
) -> dict[str, dict[str, object]]:
    manifest = record(value, "candidate manifest")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("repository") != SOURCE
        or manifest.get("version") != version
        or manifest.get("tag") != "v" + version
        or manifest.get("candidate") != request.source_sha
        or manifest.get("workflow") != PREPARE_WORKFLOW
        or manifest.get("bundle") != "release-candidate.bundle"
    ):
        fail("Candidate identity differs from the requested release")
    text(manifest.get("source"), SHA, "candidate source")
    number(manifest.get("run_id"), "preparation run ID")
    number(manifest.get("run_attempt"), "preparation run attempt")
    assets: dict[str, dict[str, object]] = {}
    for item in sequence(manifest.get("assets"), "candidate assets"):
        asset = record(item, "candidate asset")
        name = text(
            asset.get("name"), r"[A-Za-z0-9][A-Za-z0-9._-]*", "candidate asset name"
        )
        text(asset.get("sha256"), DIGEST, "asset digest")
        if type(asset.get("size")) is not int or asset["size"] < 0:
            fail("Invalid asset size")
        if name in assets or name == "release-candidate.json":
            fail("Duplicate or recursive candidate asset")
        assets[name] = asset
    if f"actionlint-container_{version}.digest" not in assets:
        fail("Candidate has no container digest")
    if not any(name.endswith(".whl") for name in assets):
        fail("Candidate has no wheels")
    return assets


def verify_run(value: object, manifest: dict[str, object]) -> None:
    workflow = record(value, "preparation run")
    repository = record(workflow.get("repository"), "preparation repository")
    expected = {
        "id": manifest["run_id"],
        "run_attempt": manifest["run_attempt"],
        "head_sha": manifest["source"],
        "head_branch": "master",
        "path": PREPARE_WORKFLOW,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }
    if repository.get("id") != int(SOURCE_ID) or repository.get("full_name") != SOURCE:
        fail("Preparation run belongs to another repository")
    if any(workflow.get(key) != value for key, value in expected.items()):
        fail("Candidate was not prepared by the successful trusted workflow attempt")


def verify_bytes(data: bytes, expected: dict[str, object]) -> None:
    if len(data) != expected["size"] or sha256(data) != expected["sha256"]:
        fail("Released asset differs from the approved candidate")


def verify_pypi(
    value: object, version: str, assets: dict[str, dict[str, object]]
) -> list[dict[str, str]]:
    response = record(value, "PyPI response")
    info = record(response.get("info"), "PyPI package")
    if info.get("name") != "actionlint-kjanat" or info.get("version") != version:
        fail("PyPI package identity differs")
    published: dict[str, dict[str, object]] = {}
    for item in sequence(response.get("urls"), "PyPI files"):
        artifact = record(item, "PyPI file")
        name = text(
            artifact.get("filename"), r"[A-Za-z0-9][A-Za-z0-9._-]*", "wheel filename"
        )
        if name in published:
            fail("Duplicate PyPI file")
        published[name] = artifact
    if set(published) != {name for name in assets if name.endswith(".whl")}:
        fail("PyPI contains missing or unapproved release files")
    wheels = []
    for name, expected in assets.items():
        if not name.endswith(".whl"):
            continue
        if not name.startswith(f"actionlint_kjanat-{version}-"):
            fail("Candidate wheel has the wrong package or version")
        artifact = published.get(name)
        if (
            artifact is None
            or artifact.get("yanked") is not False
            or artifact.get("packagetype") != "bdist_wheel"
        ):
            fail("A required wheel is missing or yanked on PyPI")
        digest = record(artifact.get("digests"), "PyPI wheel digests")
        if (
            digest.get("sha256") != expected["sha256"]
            or artifact.get("size") != expected["size"]
        ):
            fail("PyPI wheel differs from the approved candidate")
        wheels.append({"filename": name, "sha256": str(expected["sha256"])})
    # The hook must install on all three promised desktop/CI platforms.
    for platform in ("win_amd64", "manylinux_2_17_x86_64", "macosx_"):
        if not any(platform in wheel["filename"] for wheel in wheels):
            fail(f"Candidate is missing a required platform wheel: {platform}")
    return sorted(wheels, key=lambda wheel: wheel["filename"])


def fetch_pypi(version: str) -> object:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/actionlint-kjanat/{version}/json"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        fail("PyPI response exceeds limit")
    return json.loads(data)


def fetch_asset(asset: dict[str, object]) -> bytes:
    size = number(asset.get("size"), "release asset size")
    if size > 2 * 1024 * 1024:
        fail("Release metadata asset exceeds limit")
    data = run(
        "gh",
        "api",
        f"repos/{SOURCE}/releases/assets/{asset['id']}",
        "-H",
        "Accept: application/octet-stream",
    )
    if len(data) != size:
        fail("Release metadata asset size changed")
    return data


def resolve(request: Request, directory: Path) -> dict[str, object]:
    repository = record(github(f"repos/{SOURCE}"), "source repository")
    if repository.get("id") != int(SOURCE_ID):
        fail("Source repository identity changed")
    source_release = github(f"repos/{SOURCE}/releases/{request.release_id}")
    version, published = verify_release(source_release, request)
    if int(version.split(".")[0]) < 2:
        fail("These hooks require actionlint v2 with the pre-commit subcommand")
    ref = record(github(f"repos/{SOURCE}/git/ref/tags/v{version}"), "tag reference")
    tag_object = record(ref.get("object"), "tag object")
    tag_sha = text(tag_object.get("sha"), SHA, "tag object SHA")
    verify_tag(ref, github(f"repos/{SOURCE}/git/tags/{tag_sha}"), request, version)
    evidence = release_evidence(source_release, ref, request)
    manifest_bytes = fetch_asset(published["release-candidate.json"])
    manifest = record(json.loads(manifest_bytes), "candidate manifest")
    assets = verify_manifest(manifest, request, version)
    if set(published) != set(assets) | {"release-candidate.json"}:
        fail("Published assets differ from the candidate")
    for name, asset in assets.items():
        if published[name].get("size") != asset["size"] or published[name].get(
            "digest"
        ) != "sha256:" + str(asset["sha256"]):
            fail("GitHub asset identity differs from the approved candidate")
    verify_run(
        github(
            f"repos/{SOURCE}/actions/runs/{manifest['run_id']}/attempts/{manifest['run_attempt']}"
        ),
        manifest,
    )
    attested = directory / "release-candidate.json"
    attested.write_bytes(manifest_bytes)
    run(
        "gh",
        "attestation",
        "verify",
        str(attested),
        "--repo",
        SOURCE,
        "--signer-workflow",
        f"{SOURCE}/.github/workflows/release.yml",
        "--source-digest",
        request.source_sha,
        "--source-ref",
        f"refs/tags/v{version}",
        "--deny-self-hosted-runners",
    )
    digest_name = f"actionlint-container_{version}.digest"
    digest_bytes = fetch_asset(published[digest_name])
    verify_bytes(digest_bytes, assets[digest_name])
    image_digest = text(
        digest_bytes.decode("ascii").strip(), "sha256:" + DIGEST, "container digest"
    )
    actual = run("skopeo", "inspect", "--raw", f"docker://ghcr.io/{SOURCE}:{version}")
    if "sha256:" + sha256(actual) != image_digest:
        fail("Published container differs from the tested release image")
    wheels = verify_pypi(fetch_pypi(version), version, assets)
    recheck_source(request, evidence, sha256(manifest_bytes))
    return {
        "status": "released",
        "version": version,
        "source_repository_id": SOURCE_ID,
        "release_id": request.release_id,
        "source_sha": request.source_sha,
        "manifest_sha256": sha256(manifest_bytes),
        "source_evidence": evidence,
        "image": f"ghcr.io/{SOURCE}@{image_digest}",
        "wheels": wheels,
    }


def needs_update(root: Path, release: dict[str, object]) -> bool:
    previous = record(
        json.loads((root / "release.json").read_text(encoding="utf-8")),
        "previous release",
    )
    if previous.get("status") not in ("unreleased", "released"):
        fail("Invalid previous release state")
    if previous.get("status") == "released":
        current = text(previous.get("version"), VERSION, "previous version")
        incoming = text(release.get("version"), VERSION, "incoming version")
        if tuple(map(int, incoming.split("."))) < tuple(map(int, current.split("."))):
            fail("Refusing a release downgrade")
        if current == incoming:
            if previous != release:
                fail("An immutable hook release cannot change its source or artifacts")
            return False
    return True


def prepare(root: Path, release: dict[str, object]) -> bool:
    if not needs_update(root, release):
        return False
    (root / "release.json").write_text(
        json.dumps(release, indent=2) + "\n", encoding="utf-8"
    )
    (root / ".pre-commit-hooks.yaml").write_text(
        HOOKS
        + f"""- id: actionlint-docker
  name: Lint GitHub Actions workflow files (Docker)
  language: docker_image
  entry: {release["image"]} pre-commit
  pass_filenames: false
  always_run: true
""",
        encoding="utf-8",
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repository-id", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    request = Request.parse(
        args.source_repository_id, args.release_id, args.source_sha, args.operation_id
    )
    with tempfile.TemporaryDirectory(prefix="actionlint-hook-release-") as temporary:
        release = resolve(request, Path(temporary))
    changed = needs_update(args.root, release)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "operation_id": request.operation_id,
                    "changed": changed,
                    "validated": release,
                },
                indent=2,
            )
        )
        return
    changed = prepare(args.root, release)
    print(
        json.dumps(
            {
                "operation_id": request.operation_id,
                "changed": changed,
                "version": release["version"],
            }
        )
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(
                f"version={release['version']}\nchanged={str(changed).lower()}\n"
            )


if __name__ == "__main__":
    main()
