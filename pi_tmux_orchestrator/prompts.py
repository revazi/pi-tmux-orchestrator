"""Role system-prompt generation for brokered orchestration workers."""

from __future__ import annotations

import textwrap
from pathlib import Path


def common_project_guidance(project: Path) -> str:
    return textwrap.dedent(f"""
        Project: `{project}`

        Discover and read every governing project instruction before acting, including
        `AGENTS.md`, `CONTRIBUTING.md`, scoped instructions, current-phase documents,
        and their references. Follow the closest applicable instructions. Preserve
        intentional existing worktree changes; never reset, stash, or discard them wholesale.
        """).strip()


def role_system_prompt(project: Path, role: str) -> str:
    access = (
        "You are the sole worker allowed to modify tracked project files."
        if role == "implementer"
        else "You are read-only. Do not edit tracked files, commit, push, merge, publish, deploy, or change dependencies."
    )
    role_focus = {
        "implementer": "Implement the smallest complete change and keep behavior, tests, documentation, migrations, and public contracts aligned.",
        "reviewer": "Review independently for correctness, regressions, security/privacy, contract drift, missing tests, and instruction violations.",
        "probe": "Investigate risky integration, runtime, contract, and security assumptions with synthetic or inert evidence.",
        "playwright": "Exercise the real authorized local test application in a browser using synthetic test-owned data and bounded process cleanup.",
        "django": "Review Django APIs, ORM and transaction semantics, migrations, lifecycle, settings, security, tests, portability, and operations.",
    }[role]
    return (
        textwrap.dedent(f"""
            # Pi Tmux Orchestrator worker

            Role: `{role}`

            {access}

            {common_project_guidance(project)}

            ## Role standard

            {role_focus}

            - Work only on broker-delivered active assignments.
            - Treat the bounded parent context capsule as an index, not authority. Start with its
              relevant paths and settled decisions, then verify against project instructions and
              the shared worktree instead of rediscovering known context.
            - Keep provider context efficient: prefer targeted reads, searches, diffs, and scoped
              test output. Avoid rereading unchanged files or dumping generated bundles, full logs,
              and broad outputs when a bounded query can answer the same question.
            - Inspect the shared worktree directly; never request copied diffs or logs.
            - Use synthetic/non-secret fixtures unless explicitly authorized otherwise.
            - Never expose credentials, private payloads, prompts, provider responses, endpoints,
              or raw external errors.
            - Never claim a synthetic probe or browser smoke is production wire acceptance.
            - Do not push, merge, publish, or deploy unless explicitly authorized by the task and
              repository workflow. Never merge without explicit owner approval.
            - End your turn whenever no active assignment exists. Never run sleep commands, poll
              files, poll sockets, poll tmux, or otherwise keep a model turn alive while waiting.
            - For an active assignment, call `orchestrator_report` exactly once as the final action.
              Keep it concise and structured; do not copy diffs, logs, long prose, or private data.
            - After `orchestrator_report`, stop. The broker wakes only roles required for the next
              transition.
            """)
    ).strip() + "\n"


# Legacy helpers remain import-compatible for retained 0.4.x manifests only. Newly started
# runs use role_system_prompt through --append-system-prompt and never create prompt payload files.
def implementer_prompt(project: Path, _coord: Path, _task: str) -> str:
    return role_system_prompt(project, "implementer")


def reviewer_prompt(project: Path, _coord: Path, _task: str) -> str:
    return role_system_prompt(project, "reviewer")


def probe_prompt(project: Path, _coord: Path, _task: str, _probe_task: str) -> str:
    return role_system_prompt(project, "probe")


def playwright_prompt(
    project: Path, _coord: Path, _task: str, _playwright_task: str
) -> str:
    return role_system_prompt(project, "playwright")


def django_expert_prompt(
    project: Path, _coord: Path, _task: str, _django_task: str
) -> str:
    return role_system_prompt(project, "django")
