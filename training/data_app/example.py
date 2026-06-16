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
        """Conversational prompt-completion record (TRL format).

        The app serves these agents through ``ChatOllama`` with a single
        ``HumanMessage(content=prompt)`` (see ``app.agents.llm_output``), so the
        model sees the prompt wrapped in a chat template with an assistant turn
        appended. We mirror that exactly: a user turn (the rendered prompt) and
        an assistant turn (the schema-valid completion). The trainer applies the
        model's chat template, masks everything up to the assistant header, and
        trains only on the completion — train-time inputs then match inference
        byte-for-byte, and the prompt/completion boundary is a clean token break
        (no ``:``+``{`` BPE merge swallowing the opening brace)."""
        return {
            "task": self.task,
            "prompt": [{"role": "user", "content": self.prompt}],
            "completion": [{"role": "assistant", "content": self.completion}],
            "meta": self.meta,
        }
