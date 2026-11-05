"""Small local signed-token adapter; no password/SSO server or default secret."""

import base64
import hashlib
import hmac
import json
import time

from caseflow.contracts import Conflict, Principal, canonical


def _encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Auth:
    def __init__(self, secret: str, clock=time.time):
        if len(secret.encode()) < 32:
            raise ValueError("CASEFLOW_SECRET must contain at least 32 bytes")
        self.secret, self.clock = secret.encode(), clock

    def issue(self, subject, role="customer", lifetime=3600):
        body = _encode(
            canonical(
                {
                    "sub": subject,
                    "role": role,
                    "exp": int(self.clock()) + lifetime,
                    "aud": "caseflow-local",
                    "iss": "caseflow-local",
                }
            ).encode()
        )
        signature = _encode(hmac.digest(self.secret, body.encode(), hashlib.sha256))
        return body + "." + signature

    def verify(self, token):
        try:
            if len(token) > 4096:
                raise ValueError
            body, signature = token.split(".")
            expected = _encode(hmac.digest(self.secret, body.encode(), hashlib.sha256))
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if (
                data["aud"] != "caseflow-local"
                or data["iss"] != "caseflow-local"
                or type(data["exp"]) is not int
                or data["exp"] <= self.clock()
                or data["role"] not in {"customer", "reviewer"}
                or not isinstance(data["sub"], str)
                or not 0 < len(data["sub"]) <= 100
            ):
                raise ValueError
            return Principal(data["sub"], data["role"])
        except (ValueError, KeyError, TypeError):
            raise Conflict("INVALID_TOKEN", 401) from None
