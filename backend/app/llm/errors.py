"""What a model error means, from the message the adapters write. The dashboard uses it to say why
runs fail (N-037); the pre-launch check uses it to say what to fix (N-032)."""

from __future__ import annotations

import re

_HTTP_STATUS = re.compile(r"\b(?:HTTP|Error code:) ?(\d{3})\b")

HINTS = {
    "rejected": "Request rejected: 401/403 is usually a wrong key or missing permission, 404 usually a "
                "wrong model name or base URL, 400/422 usually an unsupported parameter (for example "
                "stream_options; set AGENTLOOP_LLM_STREAM_USAGE=0). Retrying will not help",
    "unavailable": "Service unavailable: 5xx, 429 (rate limit or quota exhausted) or the address is "
                   "unreachable; wait for it to recover, or check the network and the base URL",
    "timeout": "Timed out: the model answered too slowly or the network is down; check LLM_BASE_URL and "
               "the network",
    "breaker": "Circuit open: this model has failed repeatedly and is not being called for now",
    "disconnected": "The customer disconnected before the run finished",
    "interrupted": "The process exited mid-run (killed, OOM, a deploy that did not wait for in-flight "
                   "requests); the sweep marked the run as failed",
    "other": "Other error: see the error message in the trace",
}


def error_kind(error: str | None) -> str:
    """A 4xx other than 408/409/429 is a request the service refused, usually our configuration or
    parameters: retrying does not help, so it is told apart from an unavailable service."""
    text = error or ""
    if text.startswith("circuit open"):
        return "breaker"
    if text.startswith("client disconnected"):
        return "disconnected"
    if text.startswith("interrupted"):  # app.ops.stale_runs: the process running it is gone
        return "interrupted"
    status = _HTTP_STATUS.search(text)
    if status:
        code = int(status.group(1))
        return "unavailable" if code >= 500 or code in (408, 409, 429) else "rejected"
    lowered = text.lower()
    if "deadline" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if lowered.startswith(("network error", "stream interrupted", "connection error")):
        return "unavailable"
    return "other"
