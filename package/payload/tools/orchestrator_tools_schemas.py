"""JSON schemas for the E-019 Super-agent Router (`orchestrator_*`) toolset.

Kept separate from `tools/orchestrator_tools.py` so the handler module stays
focused on gating, routing, board reads, proactive replies, and evaluations.
"""

from __future__ import annotations

from typing import Any

from hermes_cli.kanban_workflow import ARCHIVED, DEFAULT_WORKFLOW

_BOARD_PROP: dict[str, Any] = {
    "type": "string",
    "description": (
        "Board slug (letters/digits/dot/underscore/hyphen). "
        "Defaults to 'default' or the HERMES_KANBAN_BOARD env var."
    ),
}

ORCHESTRATOR_ROUTE_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_route",
    "description": (
        "Accept and route work onto the task board as a new ticket. "
        "Stamps created_by='router' and returns the created task id without "
        "executing the work inline in the router turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short summary of the work to route.",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this ticket (for example "
                    "'coder', 'researcher', or 'default')."
                ),
            },
            "body": {
                "type": "string",
                "description": "Detailed requirements, context, and acceptance criteria.",
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Task ids that must reach 'done' before this ticket is "
                    "auto-dispatched."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional tenant namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker priority. Higher values are picked "
                    "sooner when multiple ready tickets share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace layout for the worker: 'scratch' (fresh temp dir, "
                    "default), 'dir' (shared directory), or 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": "Absolute working directory or git repo path for the worker.",
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the ticket to for "
                    "inheriting workspace defaults."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, ticket lands in 'triage' instead of 'todo' so a "
                    "specifier profile can refine it before execution."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "Optional caller-supplied dedup token; returns the existing "
                    "live ticket if a matching non-archived key exists."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional per-ticket wall-clock timeout in seconds.",
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status: 'running' (default dispatch path) or "
                    "'blocked' (held for immediate human review)."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skill names to preload into the worker system prompt.",
            },
            "goal_mode": {
                "type": "boolean",
                "description": "Enable iterative multi-turn goal-loop execution.",
            },
            "completion_contract": {
                "type": "string",
                "description": (
                    "Concrete verification criteria when goal_mode is enabled."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum goal-loop turns before stopping.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for the dispatched worker.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider override for the dispatched worker.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["title", "assignee"],
    },
}

ORCHESTRATOR_READ_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_read",
    "description": (
        "Read a single board ticket by id or list tickets matching filters. "
        "Provide task_id to inspect one ticket in full, or omit task_id to list "
        "tickets on the board."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Ticket id (or unique prefix) to inspect in detail. "
                    "When omitted, lists tickets on the board."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["show", "list"],
                "description": (
                    "Optional explicit read mode ('show' for single ticket, "
                    "'list' for board listing)."
                ),
            },
            "assignee": {
                "type": "string",
                "description": "Filter listed tickets by assignee profile.",
            },
            "status": {
                "type": "string",
                "enum": [*DEFAULT_WORKFLOW.keys(), ARCHIVED],
                "description": "Filter listed tickets by workflow column.",
            },
            "tenant": {
                "type": "string",
                "description": "Filter listed tickets by tenant.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tickets when listing.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Maximum number of tickets to return when listing.",
            },
            "board": _BOARD_PROP,
        },
        "required": [],
    },
}

ORCHESTRATOR_REPLY_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_reply",
    "description": (
        "Send a proactive reply message to a conversation via the gateway. "
        "Routes through the ticket's notification subscriptions or the active "
        "gateway delivery target."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Message text to deliver to the conversation.",
            },
            "message": {
                "type": "string",
                "description": "Alias for text when supplying message content.",
            },
            "task_id": {
                "type": "string",
                "description": (
                    "Optional ticket id whose notification subscriptions identify "
                    "the destination platform and chat."
                ),
            },
            "platform": {
                "type": "string",
                "description": (
                    "Optional platform name (for example 'telegram', 'discord', "
                    "'slack') when targeting a conversation directly."
                ),
            },
            "chat_id": {
                "type": "string",
                "description": "Optional destination chat or channel identifier.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional destination thread or topic identifier.",
            },
            "reply_to": {
                "type": "string",
                "description": "Optional message id to reply to in the destination chat.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["text"],
    },
}

ORCHESTRATOR_EVAL_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_eval",
    "description": (
        "Run evaluation for queued triggers (for a specific thread or all queued triggers). "
        "Returns the batch summary with counts by outcome."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_key": {
                "type": "string",
                "description": (
                    "Optional thread key to evaluate queued triggers for. "
                    "When omitted, evaluates all queued triggers."
                ),
            },
            "board": _BOARD_PROP,
        },
        "required": [],
    },
}

ORCHESTRATOR_EVAL_READ_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_eval_read",
    "description": (
        "Read the latest Decision records for a thread from sa_decisions, newest first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_key": {
                "type": "string",
                "description": "Thread key to read decisions for.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Maximum number of decisions to return (default 20).",
            },
            "board": _BOARD_PROP,
        },
        "required": ["thread_key"],
    },
}

ORCHESTRATOR_EVAL_DECIDE_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_eval_decide",
    "description": (
        "Record a Router decision on a pending Decision record. Either accepts the decision "
        "(instantiating a task and mandate via orchestrator.accept) or dismisses it "
        "(marking the Decision dismissed)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decision_id": {
                "type": "integer",
                "description": "ID of the sa_decisions record to act on.",
            },
            "id": {
                "type": "integer",
                "description": "Alias for decision_id.",
            },
            "decision": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": (
                    "Decision outcome: 'accept' to instantiate the task and mandate on the board, "
                    "or 'dismiss' to mark the Decision dismissed without creating a ticket."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": "Alias for decision ('accept' or 'dismiss').",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason or rationale for the decision.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["decision_id"],
    },
}

ORCHESTRATOR_TICKET_RETRY_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_ticket_retry",
    "description": (
        "Retry a failed, stalled, or running board ticket by ending its current run via "
        "the dispatcher retry path and marking the task ready for a new assignment. "
        "Refuses on terminal tickets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Ticket id to retry.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason or guidance for the retry attempt.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["task_id"],
    },
}

ORCHESTRATOR_TICKET_STOP_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_ticket_stop",
    "description": (
        "Stop a board ticket with person-stopped semantics: withdraws its active mandate, "
        "ends the ticket via the follows-done/release_on_end end path, and releases unread person inputs to Eval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Ticket id to stop.",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason for stopping the ticket.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["task_id"],
    },
}

ORCHESTRATOR_TICKET_REQUEST_STATUS_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_ticket_request_status",
    "description": (
        "Return a ticket's status summary including task status, mandate strength/status, "
        "unread inputs count, and latest decision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Ticket id to query for status summary.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["task_id"],
    },
}

ORCHESTRATOR_TICKET_NOTE_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_ticket_note",
    "description": (
        "Write an orchestrator coordination note (sa_inputs row kind 'note') for a ticket."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Ticket id to annotate.",
            },
            "note": {
                "type": "string",
                "description": "Coordination note content.",
            },
            "body": {
                "type": "string",
                "description": "Alias for note.",
            },
            "board": _BOARD_PROP,
        },
        "required": ["task_id"],
    },
}

ORCHESTRATOR_TRIGGER_REQUEUE_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_trigger_requeue",
    "description": (
        "Set a trigger's eval_state back to 'queued' for re-evaluation. "
        "Refuses if the trigger is currently evaluating."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_id": {
                "type": "string",
                "description": "ID of the trigger to requeue.",
            },
            "task_id": {
                "type": "string",
                "description": "Alias for trigger_id.",
            },
            "board": _BOARD_PROP,
        },
        "required": [],
    },
}

ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA: dict[str, Any] = {
    "name": "orchestrator_thread_unsubscribe",
    "description": (
        "Unsubscribe a conversation thread from ticket notifications by dropping all "
        "matching rows in sa_subscriptions for thread_key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_key": {
                "type": "string",
                "description": "Thread key whose subscriptions should be dropped.",
            },
            "thread_id": {
                "type": "string",
                "description": "Alias for thread_key.",
            },
            "board": _BOARD_PROP,
        },
        "required": [],
    },
}

ORCHESTRATOR_SCHEMAS: tuple[dict[str, Any], ...] = (
    ORCHESTRATOR_ROUTE_SCHEMA,
    ORCHESTRATOR_EVAL_SCHEMA,
    ORCHESTRATOR_EVAL_READ_SCHEMA,
    ORCHESTRATOR_EVAL_DECIDE_SCHEMA,
    ORCHESTRATOR_REPLY_SCHEMA,
    ORCHESTRATOR_READ_SCHEMA,
    ORCHESTRATOR_TICKET_RETRY_SCHEMA,
    ORCHESTRATOR_TICKET_STOP_SCHEMA,
    ORCHESTRATOR_TICKET_REQUEST_STATUS_SCHEMA,
    ORCHESTRATOR_TICKET_NOTE_SCHEMA,
    ORCHESTRATOR_TRIGGER_REQUEUE_SCHEMA,
    ORCHESTRATOR_THREAD_UNSUBSCRIBE_SCHEMA,
)
