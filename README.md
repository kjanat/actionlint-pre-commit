# actionlint pre-commit hooks

Dedicated hooks for [actionlint](https://github.com/kjanat/actionlint).
The Python hook installs the exact released native binary from
`actionlint-kjanat`. No Go compilation or separate ShellCheck/Ruff installation.

**Bootstrap checkouts:** `release.json` initially records `status: unreleased`.
Installation deliberately fails until the first verified release supplies a pin.
The existing v1.17.0 release lacks the required `pre-commit` subcommand and verified
release-candidate metadata.

Once a stable hook release exists, use its tag:

```yaml
repos:
  - repo: https://github.com/kjanat/actionlint-pre-commit
    rev: <published stable tag>
    hooks:
      - id: actionlint
```

The hook checks all repository workflows on every commit. Deleting an action or
reusable workflow therefore rechecks its unchanged callers. Repositories without
workflow files pass. Configuration, Wasm plugins, digest pins and caches behave
exactly as they do in the actionlint CLI.

The `actionlint-system` hook uses an already installed compatible actionlint.
Verified releases also provide `actionlint-docker`, pinned to the tested image
digest. These are installation choices for the same command.

## Migrating

Change the old `https://github.com/kjanat/actionlint` repository URL to this one and
select a published tag. `pre-commit autoupdate` cannot change repository URLs.
Existing frozen revisions remain available in the original repository.

Only stable hook release tags belong here. `pre-commit autoupdate` and
`pre-commit autoupdate --freeze` can then select hook releases without encountering
the source repository's historical non-release tags.

## Publication

[Beepboop](https://github.com/kjanat/beepboop) dispatches `release.yml` on `master`.
Inputs:

| Input | Meaning |
| --- | --- |
| `source_repository_id` | Must be actionlint's immutable ID, `1326162482` |
| `release_id` | Published stable GitHub release ID |
| `source_sha` | Exact source tag commit |
| `operation_id` | Router operation identity; also the workflow run name |
| `dry_run` | Validate only; defaults to `false` |

The receiver independently checks the source repository, GPG-signed annotated tag,
release candidate identity, successful preparation workflow attempt, and the
manifest's build attestation from the exact source release workflow, tag and
commit on GitHub-hosted runners. It checks every expected PyPI
wheel's digest and non-yanked status and compares the actual GHCR image bytes with
the candidate's image digest. It executes no source-release scripts.
Immediately before preparing files, it refetches the release, signed tag and
manifest. Changed asset IDs, sizes, digests, tag identity or publication status
abort the update. Metadata subprocesses have a two-minute timeout and 2 MiB
output limit.

Only then does it update the exact Python dependency and Docker digest. Replays
reuse the immutable release; identity changes and version downgrades fail.
Commit and annotated tag are GPG-signed through the central signing service using
GitHub OIDC, then pushed atomically. A failed GitHub release creation can be retried
without changing the existing tag. No private key or cross-repository token lives
in this repository.

Deployment requires `GPG_SIGN_KEY_ID`, an authorized signing-service OIDC subject
restricted to this repository's `master` workflow, and repository permissions for
the local `GITHUB_TOKEN` to push. Missing signing authorization fails publication;
there is no unsigned fallback. The first release also requires an upstream v2
candidate, published wheels and image.

The repository variable is `GPG_SIGN_KEY_ID=AFD5E3EC68371856`. An administrator
must authorize its immutable GitHub OIDC master subject through the signing
service API; this trust is not yet installed:

```http
POST https://gpg.kajkowalski.nl/admin/subjects
Authorization: Bearer <signing-service admin token>
Content-Type: application/json

{
  "name": "actionlint-pre-commit-master",
  "issuer": "https://token.actions.githubusercontent.com",
  "subjectPrefix": "repo:kjanat@6353477/actionlint-pre-commit@1427230928:ref:refs/heads/master",
  "keyIds": ["AFD5E3EC68371856"]
}
```

The subject prefix binds the owner and repository IDs and the master ref.
Read back `/admin/subjects` to verify the installed authorization. Do not widen
the prefix or write the authorization database directly.

The signing client is fixed to v1.2.0 and an independently recorded SHA-256 in the
workflow. Its installation action is intentionally unnecessary.

## Verification

```sh
python -m pip install pre-commit==4.3.0
python -m unittest discover -s tests -v
python -m pre_commit validate-manifest .pre-commit-hooks.yaml
python -m scripts.integration --binary /path/to/actionlint
```

Integration builds a disposable local wheel around the supplied real host binary,
installs this Python hook through pre-commit, and checks an empty repository, an
actual ShellCheck Wasm diagnostic, warm cache with an unreachable origin, and a
deletion-only commit. The test wheel uses `0.0.0` exclusively inside temporary
fixtures; it is never published. CI runs this on Linux, macOS and Windows.
A separate CI job reads an existing public source attestation with this
repository's own `GITHUB_TOKEN`, checking cross-repository API access independently
of the future v2 publication.

After installing the signing authorization, run the live signing probe:

```sh
gh workflow run signing-probe.yml --repo kjanat/actionlint-pre-commit --ref master
```

This manual workflow obtains a real GitHub OIDC token and uses the same pinned
signing client as publication. It signs a temporary commit and annotated tag,
then verifies both with the service's public key in an isolated keyring. It has
no repository write permission, checkout or Git remote; no refs or releases are
published. The run summary records the verified object IDs.

Candidate verification follows the MIT-licensed actionlint release tooling.
The test wheel launcher follows actionlint's existing PyPI launcher's process
behavior; see `LICENSE`.
