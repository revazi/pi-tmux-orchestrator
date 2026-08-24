from __future__ import annotations

import re

CONTROLLER_TMUX_SESSION = "pi-orchestrator-controller"
CONTROLLER_WINDOW = "controller"
CONTROLLER_PI_SESSION_ID = "pi-tmux-orchestrator-controller-v1"
CONTROLLER_STATE_VERSION = 1
CONTROLLER_OPTION_VERSION = "@pi_agents_controller_version"
CONTROLLER_OPTION_ROOT = "@pi_agents_controller_root"
CONTROLLER_OPTION_SESSION_ID = "@pi_agents_controller_session_id"
VERSION = "0.8.1"
JSON_SCHEMA_VERSION = "1"
SUPERVISOR_API_VERSION = "2"
MAX_ERROR_CHARS = 512
MAX_JSON_ITEMS = 100
WINDOW = "agents"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_MODELS = {
    "implementer": {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "thinking": "xhigh",
    },
    "reviewer": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
    "probe": {
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "thinking": "high",
    },
    "playwright": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
    "django": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
}
READ_ONLY_TOOLS = "read,bash,grep,find,ls"
BROKER_READ_ONLY_TOOLS = f"{READ_ONLY_TOOLS},orchestrator_report"
MAX_TASK_BYTES = 64 * 1024
MAX_CONTEXT_CAPSULE_BYTES = 12 * 1024
MAX_RUN_STATE_BYTES = 16 * 1024
MAX_WORKER_DELIVERY_CHARS = 32 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CONTROLLER_STATE_BYTES = 16 * 1024
MAX_RPC_PROMPT_BYTES = 256 * 1024
MAX_RPC_RECORD_BYTES = 2 * 1024 * 1024
MAX_RPC_ACK_BYTES = 4 * 1024
MAX_RPC_EVENT_BYTES = 4 * 1024
MAX_RPC_EVENT_SEGMENT_BYTES = 1024 * 1024
MAX_RPC_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_RPC_COMMANDS = 4096
MAX_RPC_EVENTS = 100
MAX_SUPERVISOR_SCAN_ENTRIES = 4096
RPC_ACK_TIMEOUT_SECONDS = 10.0
MAX_BROKER_FRAME_BYTES = 256 * 1024
MAX_REPORT_BYTES = 32 * 1024
MAX_REPORT_SUMMARY_CHARS = 2000
MAX_REPORT_ITEMS = 50
MAX_REPORT_ITEM_CHARS = 500
MAX_BROKER_EVENTS = 4096
BROKER_PROTOCOL_VERSION = 1
BROKER_COORDINATION = "broker-v1"
RPC_TRANSPORT = "rpc"
TUI_TRANSPORT = "tui"
KNOWN_ROLES = frozenset(DEFAULT_MODELS)
MANIFEST_V1_FIELDS = frozenset(
    {
        "version",
        "created_at",
        "session",
        "window",
        "project",
        "coord",
        "approve_project",
        "monitor_pane_id",
        "roles",
    }
)
MANIFEST_FIELDS = MANIFEST_V1_FIELDS | {"transport"}
MANIFEST_V3_FIELDS = MANIFEST_FIELDS | {"coordination", "protocol_version"}
ROLE_FIELDS = frozenset(
    {"provider", "model", "thinking", "tools", "pane_id", "prompt_path", "session_dir"}
)
ROLE_V3_FIELDS = frozenset(
    {"provider", "model", "thinking", "tools", "pane_id", "session_dir", "session_id"}
)
PANE_ID_PATTERN = re.compile(r"%[0-9]+")
RPC_STATE_FIELDS = frozenset(
    {
        "version",
        "role",
        "pid",
        "status",
        "is_streaming",
        "steering_count",
        "follow_up_count",
        "session_id",
        "updated_at",
    }
)
RPC_STATUSES = frozenset(
    {"starting", "idle", "streaming", "settled", "error", "exited"}
)
RPC_TOKEN_PATTERN = re.compile(r"[a-f0-9]{32}")
RPC_COMMAND_STATUSES = frozenset(
    {
        "received",
        "accepted",
        "started",
        "completed",
        "failed",
        "aborted",
        "rejected",
        "uncertain",
    }
)
RPC_TERMINAL_COMMAND_STATUSES = frozenset(
    {"completed", "failed", "aborted", "rejected", "uncertain"}
)
RPC_COMMAND_TRANSITIONS = {
    "received": frozenset({"accepted", "rejected", "uncertain"}),
    "accepted": frozenset({"started", "completed", "failed", "aborted", "uncertain"}),
    "started": frozenset({"completed", "failed", "aborted", "uncertain"}),
}
RPC_COMMAND_FIELDS = frozenset(
    {
        "id",
        "command",
        "delivery",
        "status",
        "received_at",
        "updated_at",
        "event_sequence",
    }
)
RPC_REGISTRY_FIELDS = frozenset(
    {
        "version",
        "role",
        "worker_id",
        "generation",
        "pid",
        "session_id",
        "status",
        "active_command_ids",
        "last_outcome",
        "last_event_sequence",
        "commands",
        "updated_at",
    }
)
RPC_EVENT_FIELDS = frozenset(
    {
        "version",
        "sequence",
        "timestamp",
        "role",
        "worker_id",
        "generation",
        "event",
        "command_id",
        "command",
        "delivery",
        "status",
    }
)
RPC_COMMAND_EVENT_STATUSES = {
    "command_received": "received",
    "command_accepted": "accepted",
    "command_started": "started",
    "command_completed": "completed",
    "command_failed": "failed",
    "command_aborted": "aborted",
    "command_rejected": "rejected",
    "command_uncertain": "uncertain",
}
RPC_LIFECYCLE_EVENT_STATUSES = {
    "supervisor_started": "starting",
    "agent_started": "started",
    "agent_completed": "completed",
    "agent_failed": "failed",
    "agent_aborted": "aborted",
    "supervisor_exited": "exited",
    "supervisor_failed": "failed",
}
CONTROLLER_STATE_FIELDS = frozenset(
    {
        "version",
        "created_at",
        "last_started_at",
        "session",
        "window",
        "pi_session_id",
        "root",
        "workspace",
        "session_dir",
    }
)
