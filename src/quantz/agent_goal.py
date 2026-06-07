from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class AgentGoal:
    objective: str
    symbol: str
    playbook: str
    stage: str = "replay_to_paper"
    goal_id: str = field(default_factory=lambda: str(uuid4()))
    success_criteria: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 6
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentObservation:
    goal: dict[str, Any]
    state: dict[str, Any]
    last_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentThought:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    stop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentStep:
    step: int
    observation: dict[str, Any]
    thought: dict[str, Any]
    result: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
