"""Publish the prepared hook revision with centrally signed commit and tag."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.release import DESTINATION, VERSION, fail, github, record, run, text


def publish(root: Path, key_id: str) -> None:
    release = record(
        json.loads((root / "release.json").read_text(encoding="utf-8")),
        "prepared release",
    )
    if release.get("status") != "released":
        fail("No verified release was prepared")
    version = text(release.get("version"), VERSION, "release version")
    tag = f"v{version}"
    key = text(key_id, r"[A-Fa-f0-9]{16,40}", "signing key ID")
    if (
        os.environ.get("GITHUB_REPOSITORY") != DESTINATION
        or os.environ.get("GITHUB_REF") != "refs/heads/master"
    ):
        fail("Publication is restricted to the hook repository's master workflow")

    def git(*args: str) -> str:
        return run("git", *args, cwd=root).decode().strip()

    base = git("rev-parse", "HEAD")
    remote = git("ls-remote", "origin", "refs/heads/master").split()[0]
    if remote != base:
        fail("Default branch advanced; rerun against its current revision")
    git("config", "user.name", "Kaj Kowalski")
    git("config", "user.email", "info@kajkowalski.nl")
    key_bytes = run("gpg-sign", "public-key", "--key-id", key)
    run("gpg", "--batch", "--import", data=key_bytes)
    existing = git("tag", "--list", tag)
    changed = git(
        "status", "--porcelain", "--", "release.json", ".pre-commit-hooks.yaml"
    )
    if existing:
        if (
            changed
            or git("show", f"{tag}:release.json")
            != (root / "release.json").read_text().strip()
        ):
            fail("An existing hook tag cannot be changed")
        git("verify-tag", tag)
        commit = git("rev-parse", f"{tag}^{{commit}}")
    else:
        if changed:
            git("add", "--", "release.json", ".pre-commit-hooks.yaml")
            # The only newly created local commit is signed before any remote write.
            git("-c", "commit.gpgsign=false", "commit", "-m", f"chore(release): {tag}")
            run("gpg-sign", "sign-commit", "--base", base, "--key-id", key, cwd=root)
        commit = git("rev-parse", "HEAD")
        tagger = git("var", "GIT_COMMITTER_IDENT")
        payload = f"object {commit}\ntype commit\ntag {tag}\ntagger {tagger}\n\n{tag}\n".encode()
        signature = run("gpg-sign", "sign", "--key-id", key, data=payload)
        if not signature.startswith(b"-----BEGIN PGP SIGNATURE-----"):
            fail("Signing service did not return an OpenPGP signature")
        tag_object = (
            run("git", "mktag", cwd=root, data=payload + signature).decode().strip()
        )
        git("update-ref", f"refs/tags/{tag}", tag_object, "0" * 40)
        git("verify-tag", tag)
        git(
            "-c",
            "push.followTags=false",
            "push",
            "--atomic",
            "origin",
            "HEAD:refs/heads/master",
            f"refs/tags/{tag}",
        )
    # A failed release creation is recoverable without rewriting the immutable tag.
    releases = github(f"repos/{DESTINATION}/releases?per_page=100")
    if not isinstance(releases, list):
        fail("Invalid destination release list")
    matching = [
        record(item, "destination release")
        for item in releases
        if isinstance(item, dict) and item.get("tag_name") == tag
    ]
    if matching:
        if (
            len(matching) != 1
            or matching[0].get("draft") is not False
            or matching[0].get("prerelease") is not False
        ):
            fail("Existing destination release is not a published stable release")
    else:
        run(
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            DESTINATION,
            "--verify-tag",
            "--title",
            tag,
            "--notes",
            f"Hooks for actionlint {tag}.\n\nhttps://github.com/kjanat/actionlint/releases/tag/{tag}",
        )
    print(json.dumps({"tag": tag, "commit": commit, "published": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    options = parser.parse_args()
    publish(options.root, options.key_id)
