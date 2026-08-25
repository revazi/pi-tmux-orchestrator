#!/usr/bin/env python3
"""Checked model-free cold-assignment workspace-capsule proxy benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_tmux_orchestrator.workspace_capsules import (  # noqa: E402
    INSTRUCTION_CANDIDATES,
    TOP_LEVEL_MARKERS,
    construct_workspace_capsule,
    render_workspace_capsule,
    serialize_workspace_capsule,
)

EXPECTED_PATH = ROOT / "tests" / "fixtures" / "workspace-capsule-baseline.json"
UNAVAILABLE_EVIDENCE = {
    "provider_calls": "unavailable",
    "provider_tokens": "unavailable",
    "provider_cost": "unavailable",
    "reviewer_findings": "unavailable",
    "checks": "unavailable",
    "revisions": "unavailable",
    "correctness": "unavailable",
}
FIXTURES = (
    {
        "id": "python-service-cold-fix",
        "relevant_paths": ["src/service.py", "tests/test_service.py"],
        "files": {
            "AGENTS.md": "# Synthetic instructions\nRead CONTRIBUTING.md.\n",
            "CONTRIBUTING.md": "Synthetic contribution policy.\n",
            "pyproject.toml": "[build-system]\nrequires=[]\n",
            "src/service.py": "VALUE = 1\n",
            "tests/test_service.py": "def test_value():\n    assert True\n",
            **{
                f"src/components/component_{index:02d}.py": "pass\n"
                for index in range(40)
            },
            **{f"docs/guide_{index:02d}.md": "synthetic\n" for index in range(20)},
        },
    },
    {
        "id": "node-workspace-cold-feature",
        "relevant_paths": [
            "packages/web/src/widget.ts",
            "packages/web/test/widget.test.ts",
        ],
        "files": {
            "AGENTS.md": "# Synthetic root instructions\n",
            "package.json": '{"private":true}\n',
            "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
            "packages/web/AGENTS.override.md": "# Synthetic web instructions\n",
            "packages/web/src/widget.ts": "export const widget = true;\n",
            "packages/web/test/widget.test.ts": "// synthetic test\n",
            **{
                f"packages/lib-{index:02d}/src/index.ts": "export {};\n"
                for index in range(30)
            },
            **{f"notes/design-{index:02d}.md": "synthetic\n" for index in range(30)},
        },
    },
)


def _git(project: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
    )
    subprocess.run(
        ["git", "-C", str(project), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )


def _create_fixture(root: Path, fixture: dict[str, Any]) -> Path:
    project = root / fixture["id"]
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "fixture@example.invalid")
    _git(project, "config", "user.name", "Fixture")
    for relative, content in fixture["files"].items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "fixture")
    return project


def _construction_operation_proxy(capsule: dict[str, Any]) -> int:
    instruction_directories = {
        ".",
        *{
            parent.as_posix()
            for relative in capsule["relevant_paths"]
            for parent in Path(relative).parents
            if parent.as_posix() != "."
        },
    }
    relevant_component_probes = sum(
        len(Path(path).parts) for path in capsule["relevant_paths"]
    )
    return (
        4
        + len(TOP_LEVEL_MARKERS)
        + len(instruction_directories) * len(INSTRUCTION_CANDIDATES)
        + relevant_component_probes
    )


def _fixture_result(project: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    capsule = construct_workspace_capsule(project, fixture["relevant_paths"])
    serialized_capsule = serialize_workspace_capsule(capsule)
    workspace_hint = (
        "## Experimental workspace capsule\n"
        + render_workspace_capsule(capsule, project)
        + "\n\n"
    )
    workspace_hint_bytes = len(workspace_hint.encode("utf-8"))
    canaries = [
        "Synthetic instructions",
        "Synthetic root instructions",
        "Synthetic web instructions",
        "VALUE = 1",
        "export const widget",
    ]
    if any(
        canary in serialized_capsule or canary in workspace_hint for canary in canaries
    ):
        raise RuntimeError(
            "workspace capsule leaked a synthetic source/instruction body"
        )
    return {
        "id": fixture["id"],
        "without_capsule": {
            "workspace_hint_bytes": 0,
            "serialized_worker_discovery_result_bytes": "unavailable",
            "worker_discovery_operations_proxy": 4,
            "construction_operations_proxy": 0,
        },
        "with_capsule": {
            "workspace_hint_bytes": workspace_hint_bytes,
            "serialized_worker_discovery_result_bytes": "unavailable",
            "worker_discovery_operations_proxy": 2,
            "construction_operations_proxy": _construction_operation_proxy(capsule),
        },
        "capsule_shape": {
            "instruction_count": len(capsule["instructions"]),
            "marker_count": len(capsule["markers"]),
            "relevant_path_count": len(capsule["relevant_paths"]),
            "complete_tree_included": False,
            "instruction_or_source_contents_included": False,
        },
        "authoritative_evidence": dict(UNAVAILABLE_EVIDENCE),
    }


def generate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        fixtures = [
            _fixture_result(_create_fixture(root, fixture), fixture)
            for fixture in FIXTURES
        ]
    return {
        "schema_version": 1,
        "kind": "model-free-workspace-capsule-proxy",
        "methodology": {
            "workspace_hint_bytes": "exact UTF-8 bytes added to the synthetic worker baseline by the capsule; disabled runs add zero workspace-hint bytes",
            "serialized_discovery_results": "unavailable because no model/tool transcript is fabricated and the default path never injects a repository tree",
            "discovery_operations": "fixed conceptual operation-category proxy; construction and worker operations are reported separately and are not provider calls",
            "provider_or_quality_authority": "none; all provider usage, review, check, revision, and correctness fields remain unavailable",
            "decision": "experiment remains opt-in; this proxy cannot support a default change, savings claim, provider-call claim, or correctness-equivalence claim",
        },
        "fixtures": fixtures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.check == args.write:
        parser.error("select exactly one of --check or --write")
    result = generate()
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.write:
        EXPECTED_PATH.write_text(rendered, encoding="utf-8")
        return 0
    expected = EXPECTED_PATH.read_text(encoding="utf-8")
    if expected != rendered:
        raise SystemExit("workspace capsule baseline drifted; inspect before updating")
    print("Workspace capsule model-free proxy baseline matches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
