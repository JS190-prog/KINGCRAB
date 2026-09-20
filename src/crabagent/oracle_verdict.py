"""Parse an explicit Oracle decision independently of model transport success."""
from __future__ import annotations

import json
import re
from typing import Any, Dict

PREFIX = "ORACLE_VERDICT_V1:"
CONTRACT = ('Return exactly one decision line: ORACLE_VERDICT_V1:{"verdict":"pass"}. '
            'The verdict must be pass, fail, blocked, or unknown. Use pass only after actual acceptance checks pass; '
            'a successful model call or a planned/unexecuted action is not acceptance. Explain the evidence separately.')
_VALUES = {"pass": "pass", "accepted": "pass", "fail": "fail", "failed": "fail", "rejected": "fail", "blocked": "blocked", "unknown": "unknown"}


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate verdict object field")
        result[key] = value
    return result


def parse_oracle_verdict(text: str) -> Dict[str, Any]:
    decisions = []
    lines = str(text or "").splitlines()
    try:
        if str(text).lstrip().startswith("{"):
            payload = json.loads(text, object_pairs_hook=_unique_fields)
            if isinstance(payload, dict) and "verdict" in payload:
                decisions.append(payload["verdict"])
        else:
            for index, line in enumerate(lines):
                line = line.strip()
                if line.startswith(PREFIX):
                    payload = json.loads(line[len(PREFIX):], object_pairs_hook=_unique_fields)
                    if not isinstance(payload, dict) or set(payload) != {"verdict"}:
                        raise ValueError("invalid decision object")
                    decisions.append(payload["verdict"])
                    continue
                heading = re.fullmatch(r"(?:#{1,6}\s*)?(?:\*\*)?VERDICT(?:\*\*)?\s*(?::\s*(.*))?", line, flags=re.IGNORECASE)
                if heading:
                    value = heading.group(1)
                    if not value:
                        value = next((candidate.strip() for candidate in lines[index + 1:] if candidate.strip()), "")
                    decisions.append(value)
    except (ValueError, TypeError):
        return {"verdict": "unknown", "valid": False, "reason": "malformed_verdict"}
    if len(decisions) != 1 or not isinstance(decisions[0], str) or decisions[0].strip().casefold() not in _VALUES:
        return {"verdict": "unknown", "valid": False, "reason": "missing_ambiguous_or_unsupported_verdict"}
    return {"verdict": _VALUES[decisions[0].strip().casefold()], "valid": True}
