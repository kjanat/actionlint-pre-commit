import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.release import (
    HOOKS,
    PREPARE_WORKFLOW,
    SOURCE,
    SOURCE_ID,
    Request,
    prepare,
    recheck_source,
    release_evidence,
    resolve,
    run,
    sha256,
    verify_bytes,
    verify_manifest,
    verify_pypi,
    verify_release,
    verify_run,
    verify_tag,
)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.request = Request.parse(SOURCE_ID, "42", "a" * 40, "test-operation")
        self.version = "2.0.0"
        self.assets = [
            {
                "name": f"actionlint-container_{self.version}.digest",
                "size": 72,
                "sha256": "b" * 64,
            },
            *[
                {
                    "name": f"actionlint_kjanat-{self.version}-py3-none-{platform}.whl",
                    "size": 12,
                    "sha256": "c" * 64,
                }
                for platform in (
                    "win_amd64",
                    "manylinux_2_17_x86_64",
                    "macosx_11_0_arm64",
                )
            ],
        ]
        self.manifest = {
            "schema_version": 1,
            "repository": SOURCE,
            "version": self.version,
            "tag": "v" + self.version,
            "candidate": self.request.source_sha,
            "source": "d" * 40,
            "workflow": PREPARE_WORKFLOW,
            "bundle": "release-candidate.bundle",
            "run_id": 55,
            "run_attempt": 2,
            "assets": self.assets,
        }
        self.release = {
            "id": 42,
            "tag_name": "v" + self.version,
            "draft": False,
            "prerelease": False,
            "published_at": "2026-10-20T00:00:00Z",
            "assets": [
                {
                    "id": index + 1,
                    "name": item["name"],
                    "state": "uploaded",
                    "size": item["size"],
                    "digest": "sha256:" + item["sha256"],
                }
                for index, item in enumerate(self.assets)
            ]
            + [
                {
                    "id": 8,
                    "name": "release-candidate.json",
                    "state": "uploaded",
                    "size": 1,
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        }

    def test_source_identity_and_inputs_rejected(self):
        for repo, release, sha, operation in [
            ("1", "42", "a" * 40, "ok"),
            (SOURCE_ID, "../42", "a" * 40, "ok"),
            (SOURCE_ID, "42", "master", "ok"),
            (SOURCE_ID, "42", "a" * 40, "bad\noutput=true"),
        ]:
            with (
                self.subTest(repo=repo, release=release, sha=sha, operation=operation),
                self.assertRaises(ValueError),
            ):
                Request.parse(repo, release, sha, operation)

    def test_published_stable_release_only(self):
        self.assertEqual(verify_release(self.release, self.request)[0], self.version)
        for field, value in [
            ("id", 43),
            ("draft", True),
            ("prerelease", True),
            ("published_at", None),
            ("tag_name", "v2.0.0-beta.1"),
        ]:
            altered = {**self.release, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_release(altered, self.request)

    def test_requires_gpg_tag_at_exact_commit(self):
        ref = {"object": {"type": "tag", "sha": "b" * 40}}
        tag = {
            "sha": "b" * 40,
            "tag": "v2.0.0",
            "object": {"type": "commit", "sha": "a" * 40},
            "verification": {
                "verified": True,
                "signature": "-----BEGIN PGP SIGNATURE-----\nfixture",
            },
        }
        verify_tag(ref, tag, self.request, self.version)
        for change in [
            {"object": {"type": "commit", "sha": "e" * 40}},
            {
                "verification": {
                    "verified": False,
                    "signature": "-----BEGIN PGP SIGNATURE-----",
                }
            },
            {
                "verification": {
                    "verified": True,
                    "signature": "-----BEGIN SSH SIGNATURE-----",
                }
            },
        ]:
            with self.assertRaises(ValueError):
                verify_tag(ref, {**tag, **change}, self.request, self.version)

    def test_candidate_identity_and_duplicate_assets(self):
        verify_manifest(self.manifest, self.request, self.version)
        for field, value in [
            ("repository", "attacker/actionlint"),
            ("candidate", "e" * 40),
            ("workflow", ".github/workflows/untrusted.yml"),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_manifest(
                    {**self.manifest, field: value}, self.request, self.version
                )
        with self.assertRaises(ValueError):
            verify_manifest(
                {**self.manifest, "assets": self.assets + [self.assets[0]]},
                self.request,
                self.version,
            )

    def test_successful_exact_workflow_attempt(self):
        workflow = {
            "id": 55,
            "run_attempt": 2,
            "head_sha": "d" * 40,
            "head_branch": "master",
            "path": PREPARE_WORKFLOW,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": int(SOURCE_ID), "full_name": SOURCE},
        }
        verify_run(workflow, self.manifest)
        for field, value in [
            ("conclusion", "failure"),
            ("run_attempt", 1),
            ("event", "pull_request"),
            ("head_sha", "e" * 40),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_run({**workflow, field: value}, self.manifest)

    def test_artifact_tampering(self):
        expected = {"size": 3, "sha256": sha256(b"yes")}
        verify_bytes(b"yes", expected)
        for value in (b"bad", b"yes\n"):
            with self.assertRaises(ValueError):
                verify_bytes(value, expected)

    def test_pypi_exact_bytes_all_platforms_and_not_yanked(self):
        assets = verify_manifest(self.manifest, self.request, self.version)
        response = {
            "info": {"name": "actionlint-kjanat", "version": self.version},
            "urls": [
                {
                    "filename": name,
                    "size": item["size"],
                    "digests": {"sha256": item["sha256"]},
                    "yanked": False,
                    "packagetype": "bdist_wheel",
                }
                for name, item in assets.items()
                if name.endswith(".whl")
            ],
        }
        self.assertEqual(len(verify_pypi(response, self.version, assets)), 3)
        for field, value in [
            ("yanked", True),
            ("size", 13),
            ("digests", {"sha256": "f" * 64}),
        ]:
            altered = copy.deepcopy(response)
            altered["urls"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_pypi(altered, self.version, assets)
        with self.assertRaises(ValueError):
            verify_pypi(
                {**response, "urls": response["urls"][1:]}, self.version, assets
            )
        with self.assertRaises(ValueError):
            verify_pypi(
                {
                    **response,
                    "urls": [
                        *response["urls"],
                        {
                            **response["urls"][0],
                            "filename": "actionlint_kjanat-2.0.0-py3-none-any.whl",
                        },
                    ],
                },
                self.version,
                assets,
            )

    def test_prepare_replay_identity_drift_and_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text('{"status":"unreleased"}')
            (root / ".pre-commit-hooks.yaml").write_text(HOOKS)
            release = {
                "status": "released",
                "version": "2.0.0",
                "image": "ghcr.io/kjanat/actionlint@sha256:" + "a" * 64,
            }
            self.assertTrue(prepare(root, release))
            self.assertFalse(prepare(root, release))
            self.assertIn(
                release["image"], (root / ".pre-commit-hooks.yaml").read_text()
            )
            for changed in (
                {**release, "image": "different"},
                {**release, "version": "1.9.0"},
            ):
                with self.assertRaises(ValueError):
                    prepare(root, changed)
            self.assertEqual(json.loads((root / "release.json").read_text()), release)

    def test_full_resolution_requires_trusted_manifest_and_published_image_bytes(self):
        image = (
            b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json"}'
        )
        image_digest = ("sha256:" + sha256(image) + "\n").encode()
        self.assets[0].update(size=len(image_digest), sha256=sha256(image_digest))
        self.release["assets"][0].update(
            size=len(image_digest), digest="sha256:" + sha256(image_digest)
        )
        manifest_bytes = json.dumps(self.manifest).encode()
        self.release["assets"][-1].update(
            size=len(manifest_bytes), digest="sha256:" + sha256(manifest_bytes)
        )
        ref = {"object": {"type": "tag", "sha": "b" * 40}}
        tag = {
            "sha": "b" * 40,
            "tag": "v2.0.0",
            "object": {"type": "commit", "sha": "a" * 40},
            "verification": {
                "verified": True,
                "signature": "-----BEGIN PGP SIGNATURE-----\nfixture",
            },
        }
        run = {
            "id": 55,
            "run_attempt": 2,
            "head_sha": "d" * 40,
            "head_branch": "master",
            "path": PREPARE_WORKFLOW,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "repository": {"id": int(SOURCE_ID), "full_name": SOURCE},
        }
        responses = {
            f"repos/{SOURCE}": {"id": int(SOURCE_ID)},
            f"repos/{SOURCE}/releases/42": self.release,
            f"repos/{SOURCE}/git/ref/tags/v2.0.0": ref,
            f"repos/{SOURCE}/git/tags/{'b' * 40}": tag,
            f"repos/{SOURCE}/actions/runs/55/attempts/2": run,
        }
        pypi = {
            "info": {"name": "actionlint-kjanat", "version": self.version},
            "urls": [
                {
                    "filename": item["name"],
                    "size": item["size"],
                    "digests": {"sha256": item["sha256"]},
                    "yanked": False,
                    "packagetype": "bdist_wheel",
                }
                for item in self.assets
                if item["name"].endswith(".whl")
            ],
        }
        for bad in (
            "none",
            "trusted-manifest",
            "image",
            "source-id",
            "unpublished-wheel",
        ):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def fake_run(*args, failure=bad, **kwargs):
                    if args[:3] == ("gh", "attestation", "verify"):
                        self.assertEqual(Path(args[3]).read_bytes(), manifest_bytes)
                        self.assertIn("--deny-self-hosted-runners", args)
                        self.assertEqual(
                            args[args.index("--source-digest") + 1],
                            self.request.source_sha,
                        )
                        self.assertEqual(
                            args[args.index("--signer-workflow") + 1],
                            f"{SOURCE}/.github/workflows/release.yml",
                        )
                        if failure == "trusted-manifest":
                            raise subprocess.CalledProcessError(1, args)
                        return b""
                    if args[:3] == ("skopeo", "inspect", "--raw"):
                        return b"tampered" if failure == "image" else image
                    self.fail(f"Unexpected command: {args}")

                changed_responses = copy.deepcopy(responses)
                if bad == "source-id":
                    changed_responses[f"repos/{SOURCE}"] = {"id": 99}
                published_wheels = copy.deepcopy(pypi)
                if bad == "unpublished-wheel":
                    published_wheels["urls"].pop()
                with (
                    patch(
                        "scripts.release.github",
                        side_effect=changed_responses.__getitem__,
                    ),
                    patch(
                        "scripts.release.fetch_asset",
                        side_effect=lambda item: (
                            manifest_bytes
                            if item["name"] == "release-candidate.json"
                            else image_digest
                        ),
                    ),
                    patch("scripts.release.fetch_pypi", return_value=published_wheels),
                    patch("scripts.release.run", side_effect=fake_run),
                ):
                    if bad == "none":
                        result = resolve(self.request, root)
                        self.assertEqual(
                            result["image"], f"ghcr.io/{SOURCE}@sha256:{sha256(image)}"
                        )
                        self.assertEqual(
                            result["manifest_sha256"], sha256(manifest_bytes)
                        )
                    else:
                        with self.assertRaises(
                            (ValueError, subprocess.CalledProcessError)
                        ):
                            resolve(self.request, root)

    def test_recheck_rejects_source_changes(self):
        ref = {"object": {"type": "tag", "sha": "b" * 40}}
        tag = {
            "sha": "b" * 40,
            "tag": "v2.0.0",
            "object": {"type": "commit", "sha": "a" * 40},
            "verification": {
                "verified": True,
                "signature": "-----BEGIN PGP SIGNATURE-----\nfixture",
            },
        }
        expected = release_evidence(self.release, ref, self.request)
        for mutation in ("none", "asset-id", "digest", "tag", "draft", "repo", "bytes"):
            responses = {
                f"repos/{SOURCE}": {"id": int(SOURCE_ID)},
                f"repos/{SOURCE}/releases/42": copy.deepcopy(self.release),
                f"repos/{SOURCE}/git/ref/tags/v2.0.0": copy.deepcopy(ref),
                f"repos/{SOURCE}/git/tags/{'b' * 40}": tag,
            }
            changed = responses[f"repos/{SOURCE}/releases/42"]
            if mutation == "asset-id":
                changed["assets"][0]["id"] = 99
            elif mutation == "digest":
                changed["assets"][0]["digest"] = "sha256:" + "f" * 64
            elif mutation == "draft":
                changed["draft"] = True
            elif mutation == "repo":
                responses[f"repos/{SOURCE}"]["id"] = 1
            elif mutation == "tag":
                responses[f"repos/{SOURCE}/git/ref/tags/v2.0.0"]["object"]["sha"] = (
                    "e" * 40
                )
            with (
                self.subTest(mutation=mutation),
                patch("scripts.release.github", side_effect=responses.__getitem__),
                patch(
                    "scripts.release.fetch_asset",
                    return_value=b"changed" if mutation == "bytes" else b"manifest",
                ),
            ):
                if mutation == "none":
                    recheck_source(self.request, expected, sha256(b"manifest"))
                else:
                    with self.assertRaises(ValueError):
                        recheck_source(self.request, expected, sha256(b"manifest"))

    def test_command_output_is_bounded(self):
        self.assertEqual(
            run(sys.executable, "-c", "print('ok', end='')", limit=2), b"ok"
        )
        with self.assertRaisesRegex(ValueError, "output exceeds"):
            run(sys.executable, "-c", "print('x'*1000000)", limit=1024)

    def test_command_timeout_input_and_failure(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run(sys.executable, "-c", "import time; time.sleep(60)", timeout=0.2)
        self.assertEqual(
            run(
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                data=b"ok",
            ),
            b"ok",
        )
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            run(sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(failure.exception.returncode, 7)


if __name__ == "__main__":
    unittest.main()
