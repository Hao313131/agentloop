"""What a refusal looks like, shared by the dashboard and the evaluations.

The system prompt tells the model to say it found no basis instead of guessing, and the
fallback texts say the agent cannot handle the request; both are recognised here, so the
"Self-service resolution" on the dashboard and the refusal checks in the eval suite use one definition.
"""

from __future__ import annotations

import re

REFUSAL_RE = re.compile(
    r"(?:couldn'?t find|can'?t help with that|can'?t handle|unable to (?:answer|help)|"
    r"not able to (?:answer|help)|speak(?:ing)? (?:with|to) a human|ask for a human|"
    r"no (?:relevant )?(?:information|evidence) (?:was )?found|don'?t have (?:that|enough) information)",
    re.IGNORECASE)


def is_refusal(answer: str | None) -> bool:
    return bool(answer and REFUSAL_RE.search(answer))
