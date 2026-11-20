"""Publish or verify the tagged checkout with the release workflow's pinned npm."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REGISTRY = "--registry=https://registry.npmjs.org/"
LOOKUP_TIMEOUT = 15
PROPAGATION_TIMEOUT = 120
RETRY_DELAY = 10


class ReleaseError(Exception):
    """A fixed, safe-to-log release failure."""


def npm(*args: str, timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["npm", *args, "--logs-max=0", "--no-update-notifier"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ReleaseError(f"npm {args[0]} timed out") from None
    except OSError:
        raise ReleaseError(f"could not run npm {args[0]}") from None


def decode_json(data: bytes, failure: str):
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        raise ReleaseError(failure) from None


def pack_integrity(name: str, version: str) -> str:
    # npm computes integrity over the same tarball bytes even in dry-run mode.
    result = npm(
        "pack", "--dry-run", "--offline", "--ignore-scripts", "--json", timeout=30
    )
    if result.returncode != 0:
        raise ReleaseError("offline npm pack failed")
    entries = decode_json(result.stdout, "offline npm pack returned invalid JSON")
    if (
        not isinstance(entries, list)
        or len(entries) != 1
        or not isinstance(entries[0], dict)
    ):
        raise ReleaseError("offline npm pack did not return one package")
    entry = entries[0]
    integrity = entry.get("integrity")
    if (
        (entry.get("name"), entry.get("version")) != (name, version)
        or not isinstance(integrity, str)
        or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", integrity)
    ):
        raise ReleaseError("offline npm pack identity or SHA-512 mismatch")
    return integrity


def exact_version_present(expected: dict[str, str], timeout: float) -> bool:
    result = npm(
        "view",
        f"{expected['name']}@{expected['version']}",
        *expected,
        "--json",
        REGISTRY,
        "--prefer-online",
        "--fetch-retries=0",
        timeout=timeout,
    )
    metadata = decode_json(result.stdout, "npm view returned invalid JSON")
    if result.returncode != 0:
        # Pinned npm emits its structured error on stdout, not in stderr text.
        if (
            result.returncode > 0
            and isinstance(metadata, dict)
            and metadata.keys() == {"error"}
        ):
            error = metadata["error"]
            if isinstance(error, dict) and error.get("code") == "E404":
                return False
        raise ReleaseError("npm view failed without a structured E404")
    if metadata != expected:
        raise ReleaseError("npm registry name/version/gitHead/SHA-512 mismatch")
    return True


def verify_or_publish(name: str, version: str, commit: str) -> None:
    expected = {
        "name": name,
        "version": version,
        "gitHead": commit,
        "dist.integrity": pack_integrity(name, version),
    }
    if exact_version_present(expected, LOOKUP_TIMEOUT):
        print("Exact npm release verified; skipping publication.")
        return

    print("Exact npm version absent; attempting trusted publication once.", flush=True)
    try:
        result = npm(
            "publish",
            REGISTRY,
            "--access=public",
            "--provenance",
            "--ignore-scripts",
            "--fetch-retries=0",
            timeout=120,
        )
        if result.returncode != 0:
            raise ReleaseError("npm publish failed")
    except ReleaseError as error:
        raise ReleaseError(
            f"{error}; check the exact npm version before rerunning"
        ) from None

    deadline = time.monotonic() + PROPAGATION_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        present = exact_version_present(expected, min(LOOKUP_TIMEOUT, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if present:
            print("npm publication and exact registry metadata verified.")
            return
        time.sleep(min(RETRY_DELAY, remaining))
    raise ReleaseError("npm did not confirm the exact release within 120 seconds")


def main() -> int:
    try:
        manifest = decode_json(
            Path("package.json").read_bytes(), "invalid release package.json"
        )
        commit = os.environ.get("GITHUB_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ReleaseError("invalid release commit (GITHUB_SHA)")
        if not isinstance(manifest, dict) or not all(
            isinstance(manifest.get(key), str) and manifest[key]
            for key in ("name", "version")
        ):
            raise ReleaseError("invalid release package identity")
        verify_or_publish(manifest["name"], manifest["version"], commit)
    except OSError:
        print("could not read release package.json", file=sys.stderr)
        return 1
    except ReleaseError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
