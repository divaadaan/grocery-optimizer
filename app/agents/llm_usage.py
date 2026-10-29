"""Provider-reported token usage for one logical agent call.

``invoke_validated`` may call the model several times (validation retries), so
an agent wraps its model in a :class:`MeteredLLM` for the duration of one call
and reads :attr:`MeteredLLM.usage` afterwards for MLflow.

Counts come only from the provider's ``usage_metadata``. A count is reported
only when *every* response in the call reported it: a sum over some of the
responses would be an undercount presented as a measurement, so it is absent
instead. Nothing is ever estimated from text length.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def response_usage(response: Any) -> Dict[str, Optional[int]]:
    """``{key: int | None}`` from one response's ``usage_metadata``."""
    usage = getattr(response, "usage_metadata", None)
    out: Dict[str, Optional[int]] = {k: None for k in USAGE_KEYS}
    if not usage:
        return out
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    for key in USAGE_KEYS:
        value = get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


class MeteredLLM:
    """Wraps ``.invoke(messages)`` and sums the provider's token counts.

    One instance per logical call (it holds per-call state), so it is safe
    under the parallel SousChefs: each worker meters its own wrapper.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._responses: List[Dict[str, Optional[int]]] = []

    def invoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        response = self.inner.invoke(messages, *args, **kwargs)
        self._responses.append(response_usage(response))
        return response

    @property
    def usage(self) -> Optional[Dict[str, int]]:
        """Summed counts, only for keys every response reported; ``None`` if
        no key qualifies (or no response came back)."""
        if not self._responses:
            return None
        summed = {}
        for key in USAGE_KEYS:
            values = [r[key] for r in self._responses]
            if all(v is not None for v in values):
                summed[key] = sum(values)
        return summed or None
