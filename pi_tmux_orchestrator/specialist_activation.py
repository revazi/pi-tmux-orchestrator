"""Deterministic specialist activation policy without provider calls."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .models import OrchestrationError

SPECIALIST_ROLES = ("probe", "playwright", "django")
ACTIVATION_DECISIONS = ("run", "skipped")
MAX_FORCED_SPECIALISTS = len(SPECIALIST_ROLES)

_DOCUMENTATION_NAMES = {
    "changelog",
    "contributing",
    "license",
    "readme",
    "security",
}
_DOCUMENTATION_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
_FRONTEND_SUFFIXES = {
    ".css",
    ".js",
    ".jsx",
    ".sass",
    ".scss",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}
_BROWSER_SUFFIXES = _FRONTEND_SUFFIXES | {".html", ".htm"}
_DJANGO_PARTS = {
    "asgi.py",
    "admin.py",
    "apps.py",
    "manage.py",
    "middleware.py",
    "models.py",
    "settings.py",
    "urls.py",
    "views.py",
    "wsgi.py",
}
_DJANGO_DIRECTORIES = {"migrations", "templates"}
_PROBE_RISK_TERMS = {
    "api",
    "authentication",
    "authorization",
    "concurrency",
    "credential",
    "database",
    "integration",
    "migration",
    "protocol",
    "runtime",
    "security",
    "transaction",
}
_PROBE_DOC_TERMS = {"changelog", "documentation", "readme", "spelling", "typo"}
_PROBE_CODE_TERMS = {"code", "implement", "logic", "refactor", "runtime", "test"}


def validate_forced_specialists(
    values: object, configured_roles: list[str] | tuple[str, ...] | set[str]
) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or len(values) > MAX_FORCED_SPECIALISTS:
        raise OrchestrationError("Forced specialists are invalid")
    configured = set(configured_roles)
    selected: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or value not in SPECIALIST_ROLES
            or value not in configured
            or value in selected
        ):
            raise OrchestrationError(
                "Forced specialists must be unique enabled specialist roles"
            )
        selected.append(value)
    return tuple(role for role in SPECIALIST_ROLES if role in selected)


def _decision(
    role: str, decision: str, rule_id: str, *, forced: bool
) -> dict[str, Any]:
    return {
        "role": role,
        "decision": decision,
        "rule_id": rule_id,
        "forced": forced,
    }


def _normalized_paths(paths: object) -> tuple[str, ...] | None:
    if not isinstance(paths, list) or not paths:
        return None
    if not all(isinstance(path, str) and path for path in paths):
        return None
    return tuple(path.replace("\\", "/").lower() for path in paths)


def _is_documentation(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        parsed.suffix in _DOCUMENTATION_SUFFIXES
        or parsed.stem.lower() in _DOCUMENTATION_NAMES
    )


def _all_documentation(paths: tuple[str, ...]) -> bool:
    return all(_is_documentation(path) for path in paths)


def _django_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return parsed.name in _DJANGO_PARTS or bool(
        set(parsed.parts).intersection(_DJANGO_DIRECTORIES)
    )


def decide_initial_probe(task: object, *, forced: bool = False) -> dict[str, Any]:
    if forced:
        return _decision("probe", "run", "probe-forced-v1", forced=True)
    if not isinstance(task, str) or not task.strip():
        return _decision("probe", "run", "probe-ambiguous-task-v1", forced=False)
    words = set(re.findall(r"[a-z]+", task.lower()))
    if words.intersection(_PROBE_RISK_TERMS):
        return _decision("probe", "run", "probe-high-risk-task-v1", forced=False)
    if words.intersection(_PROBE_DOC_TERMS) and not words.intersection(
        _PROBE_CODE_TERMS
    ):
        return _decision("probe", "skipped", "probe-docs-only-task-v1", forced=False)
    return _decision("probe", "run", "probe-ambiguous-task-v1", forced=False)


def decide_specialist(
    role: str, changed_paths: object, *, forced: bool = False
) -> dict[str, Any]:
    if role not in SPECIALIST_ROLES:
        raise OrchestrationError("Specialist activation role is invalid")
    if forced:
        return _decision(role, "run", f"{role}-forced-v1", forced=True)
    paths = _normalized_paths(changed_paths)
    if paths is None:
        return _decision(role, "run", f"{role}-ambiguous-paths-v1", forced=False)
    if role == "probe":
        if _all_documentation(paths):
            return _decision(role, "skipped", "probe-docs-only-paths-v1", forced=False)
        return _decision(role, "run", "probe-ambiguous-paths-v1", forced=False)
    if role == "playwright":
        if any(PurePosixPath(path).suffix in _BROWSER_SUFFIXES for path in paths):
            return _decision(role, "run", "playwright-browser-path-v1", forced=False)
        if _all_documentation(paths):
            return _decision(
                role, "skipped", "playwright-docs-only-paths-v1", forced=False
            )
        return _decision(role, "run", "playwright-ambiguous-paths-v1", forced=False)
    if any(_django_path(path) for path in paths):
        return _decision(role, "run", "django-framework-path-v1", forced=False)
    if _all_documentation(paths):
        return _decision(role, "skipped", "django-docs-only-paths-v1", forced=False)
    if all(PurePosixPath(path).suffix in _FRONTEND_SUFFIXES for path in paths):
        return _decision(role, "skipped", "django-frontend-only-paths-v1", forced=False)
    return _decision(role, "run", "django-ambiguous-paths-v1", forced=False)
