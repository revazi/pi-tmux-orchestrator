"""Bounded, body-free investigation-reuse hints; never verification authority."""

from __future__ import annotations

import hashlib
import os
import selectors
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath

from .constants import KNOWN_ROLES
from .role_contracts import validate_custom_contracts

MAX_INVENTORY_BYTES = 256 * 1024
MAX_WORKTREE_FILES = 4096


def _git(root: Path, *arguments: str, deadline: float) -> bytes:
    if time.monotonic() >= deadline:
        raise ValueError("worktree_inventory_timeout")
    environment = os.environ.copy()
    # Ambient Git overrides must not redirect observation outside the target tree.
    environment = {
        key: value for key, value in environment.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    with subprocess.Popen(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    ) as process:
        try:
            output = _bounded_output(process, deadline=deadline)
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                raise ValueError("worktree_inventory_unavailable")
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _bounded_output(
    process: subprocess.Popen, *, deadline: float | None = None
) -> bytes:
    if deadline is None:
        deadline = time.monotonic() + 2
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ValueError("worktree_inventory_timeout")
            chunk = os.read(process.stdout.fileno(), 8192)
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > MAX_INVENTORY_BYTES:
                raise ValueError("worktree_inventory_oversized")


def _file_stamp(root: Path, name: bytes) -> tuple[int, ...] | None:
    relative = PurePosixPath(os.fsdecode(name))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe_inventory_path")
    path = root / relative
    if path.resolve() != path:
        raise ValueError("symlink_inventory_path")
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("non_regular_inventory_path")
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inventory_stamp(root: Path, deadline: float) -> str:
    identity = _git(root, "rev-parse", "--show-toplevel", "HEAD", deadline=deadline)
    if not identity.startswith(os.fsencode(root) + b"\n"):
        raise ValueError("not_worktree_root")
    names = sorted(
        set(
            _git(
                root,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                deadline=deadline,
            ).split(b"\0")
        )
        - {b""}
    )
    if len(names) > MAX_WORKTREE_FILES:
        raise ValueError("oversized_worktree")
    index = _git(root, "ls-files", "--stage", "-z", deadline=deadline)
    for entry in index.split(b"\0"):
        if not entry:
            continue
        header = entry.partition(b"\t")[0]
        if header.startswith(b"160000 ") or not header.endswith(b" 0"):
            raise ValueError("submodule_or_conflicted_index")
    digest = hashlib.sha256(identity + index)
    for name in names:
        if time.monotonic() >= deadline:
            raise ValueError("worktree_inventory_timeout")
        digest.update(
            name + b"\0" + repr(_file_stamp(root, name)).encode("ascii") + b"\0"
        )
    return digest.hexdigest()


def worktree_stamp(root: Path) -> str | None:
    """Compare two bounded inventories; no file bodies, hooks, or build tools read/run.

    This is a filesystem/Git metadata observation, NOT a content digest or proof
    that a check is still valid. Ignored files and external dependencies are outside
    its scope. Missing Git, symlinks, submodules, races, or excess size yield unknown.
    """
    try:
        deadline = time.monotonic() + 2
        first = _inventory_stamp(root, deadline)
        return first if first == _inventory_stamp(root, deadline) else None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


class EvidenceReuse:
    """At most one private receipt per configured role; no report bodies stored."""

    def __init__(
        self,
        project: Path,
        retained_roles: set[str],
        *,
        custom_contracts: object = None,
    ) -> None:
        self.roles = KNOWN_ROLES | validate_custom_contracts(custom_contracts).keys()
        if retained_roles - self.roles:
            raise ValueError("unknown_retained_role")
        self.project = project
        self.retained_roles = frozenset(retained_roles)
        self.guidance_generation = 0
        self.receipts: dict[str, tuple[str | None, int]] = {}

    def remember(self, role: str) -> None:
        if role not in self.roles:
            raise ValueError("unknown_evidence_role")
        if self.retained_roles:
            self.receipts[role] = (
                worktree_stamp(self.project),
                self.guidance_generation,
            )

    def invalidate_guidance(self) -> None:
        self.guidance_generation += 1

    def snapshot(self) -> dict[str, str]:
        comparable = any(
            stamp is not None and generation == self.guidance_generation
            for stamp, generation in self.receipts.values()
        )
        current = worktree_stamp(self.project) if comparable else None
        result = {}
        for role, (stamp, generation) in self.receipts.items():
            if generation != self.guidance_generation:
                result[role] = "guidance_changed"
            elif stamp is None or current is None:
                result[role] = "unavailable"
            elif stamp != current:
                result[role] = "worktree_changed"
            else:
                result[role] = "metadata_unchanged"
        return result
