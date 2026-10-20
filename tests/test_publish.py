import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.publish import publish
from scripts.release import DESTINATION


class PublishTests(unittest.TestCase):
    def exercise(self, scenario):
        """Model only Git/signing boundaries; all publication decisions run for real."""
        calls = []
        release = {"status": "released", "version": "2.0.0", "source_sha": "a" * 40}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text(json.dumps(release), encoding="utf-8")
            (root / ".pre-commit-hooks.yaml").write_text("fixture", encoding="utf-8")

            def execute(*args, cwd=None, data=None):
                calls.append((args, data))
                if args[:2] == ("git", "rev-parse"):
                    return ("b" * 40 + "\n").encode()
                if args[:2] == ("git", "ls-remote"):
                    sha = "c" * 40 if scenario == "advanced" else "b" * 40
                    return f"{sha}\trefs/heads/master\n".encode()
                if args[:3] == ("git", "tag", "--list"):
                    return b"v2.0.0\n" if scenario in ("retry", "retag") else b""
                if args[:2] == ("git", "status"):
                    return b"" if scenario == "retry" else b" M release.json\n"
                if args[:2] == ("git", "show"):
                    return json.dumps(release).encode()
                if args[:2] == ("git", "var"):
                    return b"Test <test@example.invalid> 1 +0000\n"
                if args[:2] == ("gpg-sign", "public-key"):
                    return b"public-key fixture"
                if (
                    args[:2] == ("gpg-sign", "sign-commit")
                    and scenario == "signing-failed"
                ):
                    raise subprocess.CalledProcessError(1, args)
                if args[:2] == ("gpg-sign", "sign"):
                    if scenario == "bad-signature":
                        return b"not an OpenPGP signature"
                    self.assertEqual(
                        data,
                        f"object {'b' * 40}\ntype commit\ntag v2.0.0\ntagger Test <test@example.invalid> 1 +0000\n\nv2.0.0\n".encode(),
                    )
                    return b"-----BEGIN PGP SIGNATURE-----\nfixture"
                if args[:2] == ("git", "mktag"):
                    return ("c" * 40 + "\n").encode()
                if args[:2] == ("git", "verify-tag") and scenario == "verify-failed":
                    raise subprocess.CalledProcessError(1, args)
                if (
                    args[:3] == ("gh", "release", "create")
                    and scenario == "create-failed"
                ):
                    raise subprocess.CalledProcessError(1, args)
                return b""

            env = {
                "GITHUB_REPOSITORY": DESTINATION,
                "GITHUB_REF": "refs/heads/master"
                if scenario != "wrong-branch"
                else "refs/heads/untrusted",
            }
            existing = (
                [{"tag_name": "v2.0.0", "draft": False, "prerelease": False}]
                if scenario == "retry"
                else []
            )
            with (
                patch.dict("os.environ", env),
                patch("scripts.publish.run", side_effect=execute),
                patch("scripts.publish.github", return_value=existing),
            ):
                if scenario in ("new", "retry"):
                    publish(root, "A" * 16)
                else:
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        publish(root, "A" * 16)
        return calls

    def test_new_publication_signs_before_atomic_push(self):
        calls = self.exercise("new")
        commands = [args for args, _ in calls]
        push = next(args for args in commands if "push" in args)
        self.assertIn("--atomic", push)
        self.assertIn("HEAD:refs/heads/master", push)
        self.assertIn("refs/tags/v2.0.0", push)
        verification = next(
            args for args in commands if args[:2] == ("git", "verify-tag")
        )
        self.assertLess(commands.index(verification), commands.index(push))
        self.assertTrue(
            any(args[:2] == ("gpg-sign", "sign-commit") for args in commands)
        )

    def test_failures_never_push_unsigned_or_changed_tags(self):
        for scenario in (
            "wrong-branch",
            "advanced",
            "retag",
            "signing-failed",
            "bad-signature",
            "verify-failed",
        ):
            with self.subTest(scenario=scenario):
                calls = self.exercise(scenario)
                self.assertFalse(any("push" in args for args, _ in calls))
                self.assertFalse(
                    any(args[:3] == ("gh", "release", "create") for args, _ in calls)
                )

    def test_published_replay_does_not_recreate_or_push(self):
        calls = self.exercise("retry")
        self.assertFalse(any("push" in args for args, _ in calls))
        self.assertFalse(
            any(args[:3] == ("gh", "release", "create") for args, _ in calls)
        )
        self.assertTrue(any(args[:2] == ("git", "verify-tag") for args, _ in calls))

    def test_release_creation_failure_leaves_atomic_tag_retryable(self):
        calls = self.exercise("create-failed")
        self.assertEqual(sum("push" in args for args, _ in calls), 1)
        self.assertFalse(
            any("--force" in args or "--delete" in args for args, _ in calls)
        )


if __name__ == "__main__":
    unittest.main()
