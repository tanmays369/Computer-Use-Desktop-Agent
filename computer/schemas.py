"""Typed contracts for the Computer-Use skill.

`ComputerOutput` mirrors Session 9's `BrowserOutput`: the cascade layer
that produced the answer is surfaced as `path` so the replay viewer can
show it the same way it shows Browser's path (brief §10, §12).

`ACTION_SCHEMA` is the JSON schema the Layer-2b judge LLM and the Layer-3
vision LLM both emit — one structured action per turn, with a verdict
that is either `act` (carry an element_index / coordinate) or `escalate`
(give a reason so the cascade climbs one layer). This mirrors Browser's
action vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Path = Literal["extract", "deterministic", "a11y", "vision", "blocked"]


@dataclass
class TurnRecord:
    """One scan-act-verify turn, persisted for the replay report (§7)."""
    turn: int
    layer: str
    thinking: str = ""
    action: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    note: str = ""


@dataclass
class ComputerOutput:
    goal: str
    app: str
    path: Path
    turns: int
    success: bool
    result_text: str = ""           # the extracted answer / final state
    final_state: str = ""           # short post-condition description
    actions: list[dict] = field(default_factory=list)
    error_code: str | None = None   # precondition_blocked / no_target / vlm_unavailable
    error: str | None = None
    trajectory_dir: str | None = None
    vision_calls: int = 0
    llm_calls: int = 0

    def to_dict(self) -> dict:
        return {
            "goal": self.goal, "app": self.app, "path": self.path,
            "turns": self.turns, "success": self.success,
            "result_text": self.result_text, "final_state": self.final_state,
            "actions": self.actions, "error_code": self.error_code,
            "error": self.error, "trajectory_dir": self.trajectory_dir,
            "vision_calls": self.vision_calls, "llm_calls": self.llm_calls,
        }


# Layer-2b / Layer-3 action vocabulary. One action per turn.
ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "verdict"],
    "properties": {
        "thinking": {"type": "string",
                     "description": "one short sentence of reasoning"},
        "verdict": {"type": "string", "enum": ["act", "escalate", "done"]},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string",
                         "enum": ["click", "type_text", "press_key", "hotkey"]},
                "element_index": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "text": {"type": "string"},
                "key": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "modifiers": {"type": "array", "items": {"type": "string"}},
            },
        },
        "reason": {"type": "string",
                   "description": "why escalate (when verdict=escalate)"},
        "success": {"type": "boolean",
                    "description": "did the goal complete (when verdict=done)"},
    },
}


# Layer-3 set-of-marks: the VLM picks the number of the mark sitting on the
# target. Classification beats coordinate regression for small VLMs.
MARK_PICK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mark", "thinking"],
    "properties": {
        "mark": {"type": "integer",
                 "description": "the number printed on top of the target"},
        "thinking": {"type": "string"},
    },
}


# Layer-2a deterministic plan: the planner LLM turns a goal into an ordered
# keystroke list, then the skill dispatches it with no LLM in the loop.
KEYSTROKE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keys"],
    "properties": {
        "keys": {
            "type": "array",
            "description": "ordered key presses to send to the app",
            "items": {"type": "string"},
        },
        "explanation": {"type": "string"},
    },
}
