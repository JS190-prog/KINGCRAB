from __future__ import annotations

import re
from typing import Optional


_CONTINUATION_PHRASES = {
    "진행",
    "진행해",
    "계속 진행",
    "계속 진행해",
    "계속해",
    "이어가",
    "이어가줘",
    "다시 실행",
    "다시 실행해",
    "제대로 다시 실행해",
    "작업을 해",
    "작업을 하라고",
    "왜 그러냐고 작업을 하라고",
    "continue",
    "continue it",
    "go ahead",
    "do it",
    "proceed",
    "proceed with it",
    "run it",
    "retry",
}


def normalize_continuation_prompt(prompt: str) -> str:
    """Normalize only enough to recognize short execution follow-ups.

    This intentionally does not use broad substring matching. A phrase such as
    ``계속 설명해줘`` is a normal conversational request and must not be
    mistaken for a mission continuation.
    """
    value = str(prompt or "").strip().lower()
    value = re.sub(r"[\s\u00a0]+", " ", value)
    value = re.sub(r"[.!?。！？]+$", "", value).strip()
    return value


def continuation_intent(prompt: str) -> Optional[str]:
    """Return ``resume``/``retry`` for a terse follow-up, otherwise ``None``."""
    value = normalize_continuation_prompt(prompt)
    if not value or len(value) > 80 or value not in _CONTINUATION_PHRASES:
        return None
    if "다시" in value or value in {"retry", "다시 실행", "다시 실행해", "제대로 다시 실행해"}:
        return "retry"
    return "resume"
