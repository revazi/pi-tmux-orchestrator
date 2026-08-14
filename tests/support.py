"""Test-only compatibility facade across the modular orchestrator package."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pi_tmux_orchestrator import (
    broker,
    broker_client,
    broker_store,
    cli,
    commands,
    configuration,
    constants,
    controller,
    models,
    output,
    prompts,
    protocol,
    relay,
    rpc,
    rpc_protocol,
    rpc_store,
    rpc_supervisor,
    runtime,
    storage,
    supervisor_api,
    supervisor_commands,
    tmux,
)

_TEST_MODEL_CONFIG = (
    Path(tempfile.gettempdir()) / f"pi-tmux-test-model-config-{os.getpid()}.json"
)
os.environ.setdefault("PI_TMUX_ORCHESTRATOR_CONFIG", str(_TEST_MODEL_CONFIG))


class ModuleFacade:
    """Route test patches to every module that imported a shared symbol."""

    def __init__(self) -> None:
        object.__setattr__(self, "_patches", {})
        object.__setattr__(
            self,
            "_modules",
            (
                broker,
                broker_client,
                broker_store,
                cli,
                commands,
                configuration,
                controller,
                relay,
                rpc,
                rpc_protocol,
                rpc_store,
                rpc_supervisor,
                storage,
                supervisor_api,
                supervisor_commands,
                tmux,
                output,
                prompts,
                protocol,
                models,
                constants,
                runtime,
            ),
        )

    def __getattr__(self, name: str):
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        originals = []
        for module in self._modules:
            if hasattr(module, name):
                originals.append((module, getattr(module, name)))
        if not originals:
            raise AttributeError(name)
        self._patches.setdefault(name, []).append(originals)
        for module, _original in originals:
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        stacks = self._patches.get(name)
        if not stacks:
            raise AttributeError(name)
        originals = stacks.pop()
        for module, original in originals:
            setattr(module, name, original)
        if not stacks:
            del self._patches[name]


ORCHESTRATOR = ModuleFacade()
