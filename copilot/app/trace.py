"""Optional Langfuse tracing. Fully guarded: with no Langfuse keys configured this
is a set of no-ops with zero overhead, and any SDK error degrades to a no-op rather
than breaking the agent. Observability must never take down the clinical path.

Enable by setting LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_HOST
(self-hosted server or Langfuse Cloud). Spans use semantic types so the trace tree
reads clinically: agent -> {span, generation, tool, guardrail}.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Optional


def enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


_client_cache: Any = None
_client_tried = False


def _client():
    global _client_cache, _client_tried
    if not enabled():
        return None
    if not _client_tried:
        _client_tried = True
        # accept LANGFUSE_BASE_URL as an alias for the SDK's LANGFUSE_HOST
        if not os.getenv("LANGFUSE_HOST") and os.getenv("LANGFUSE_BASE_URL"):
            os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
        try:
            from langfuse import get_client
            _client_cache = get_client()
        except Exception:
            _client_cache = None
    return _client_cache


class _Noop:
    def update(self, **_: Any) -> None:
        pass


_ALLOWED = {"input", "output", "metadata", "model", "usage_details",
            "cost_details", "level", "status_message"}


@contextmanager
def observe(name: str, as_type: str = "span", **kw: Any):
    c = _client()
    cm = None
    if c is not None:
        try:
            cm = c.start_as_current_observation(
                name=name, as_type=as_type,
                **{k: v for k, v in kw.items() if k in _ALLOWED and v is not None})
        except Exception:
            cm = None
    if cm is None:
        yield _Noop()
        return
    try:
        with cm as obs:
            yield obs
    except Exception:
        yield _Noop()


def update(obs: Any, **kw: Any) -> None:
    try:
        obs.update(**{k: v for k, v in kw.items() if k in _ALLOWED and v is not None})
    except Exception:
        pass


def score(name: str, value, comment: Optional[str] = None,
          data_type: Optional[str] = None) -> None:
    c = _client()
    if c is None:
        return
    try:
        c.score_current_trace(name=name, value=value, comment=comment,
                              data_type=data_type)
    except Exception:
        pass


def current_trace_id() -> Optional[str]:
    """The active Langfuse trace id (call inside an `observe` block). Returned to the
    client so user feedback (Layer C) can score this exact interaction later."""
    c = _client()
    if c is None:
        return None
    try:
        return c.get_current_trace_id()
    except Exception:
        return None


def score_trace(trace_id: Optional[str], name: str, value,
                comment: Optional[str] = None, data_type: Optional[str] = None) -> bool:
    """Attach a score to a specific (past) trace by id — used by the feedback endpoint.
    Degrades to False on any SDK difference/error rather than raising."""
    c = _client()
    if c is None or not trace_id:
        return False
    for attempt in ("create_score", "score"):
        try:
            getattr(c, attempt)(trace_id=trace_id, name=name, value=value,
                                comment=comment, data_type=data_type)
            c.flush()
            return True
        except Exception:
            continue
    return False


def flush() -> None:
    c = _client()
    if c is None:
        return
    try:
        c.flush()
    except Exception:
        pass
