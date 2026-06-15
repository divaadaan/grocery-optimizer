"""The record every generator emits and the orchestrator writes as JSONL."""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class Example:
    task: str            # "chef_plan" | "nutritionist_verdict" | "sous_chef_recipe"
    prompt: str          # rendered via the app's real PromptTemplates
    completion: str      # schema-valid JSON text (no EOS; the trainer adds it)
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {"task": self.task, "prompt": self.prompt, "completion": self.completion, "meta": self.meta}
