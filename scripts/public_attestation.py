"""Prove this repository's GITHUB_TOKEN can read public source attestations."""

from scripts.release import SOURCE, fail, github, record, sequence

# Existing v1.17.0 Windows archive, not a simulated v2 publication.
DIGEST = "503a7649a97c5afb07aa51703b94066c9f63b5eabc25c55a6860eabd5298d559"


def main() -> None:
    response = record(
        github(f"repos/{SOURCE}/attestations/sha256:{DIGEST}"), "attestation response"
    )
    attestations = sequence(response.get("attestations"), "attestations")
    if not attestations:
        fail("Public source attestation is unavailable")
    for item in attestations:
        bundle = record(record(item, "attestation").get("bundle"), "bundle")
        if bundle.get("mediaType") != "application/vnd.dev.sigstore.bundle.v0.3+json":
            fail("Unexpected attestation bundle")
    print(f"Read {len(attestations)} public source attestations with receiver token")


if __name__ == "__main__":
    main()
