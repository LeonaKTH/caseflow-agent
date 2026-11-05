"""Defensive persistence redaction; HTTP authorization headers are never recorded."""

import re


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]" if re.search(r"api.?key|authorization|secret|access.?token", k, re.I) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.=-]+", "Bearer [redacted]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted]", value)
    return value
