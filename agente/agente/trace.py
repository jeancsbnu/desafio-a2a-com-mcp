"""W3C Trace Context: o trace-id atravessa da chamada A2A ate cada request MCP.

O trace-id e mantido; cada request MCP ganha um span-id novo.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

_FORMATO = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class Trace:
    trace_id: str
    flags: str = "01"

    def novo_traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


def do_header(traceparent: str | None) -> Trace | None:
    """Le o header `traceparent`. Devolve None se ausente ou invalido."""
    casamento = _FORMATO.match((traceparent or "").strip().lower())
    if casamento is None:
        return None
    trace_id, span_id, flags = casamento.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return Trace(trace_id=trace_id, flags=flags)


def novo() -> Trace:
    return Trace(trace_id=secrets.token_hex(16))
