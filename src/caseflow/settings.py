"""Local configuration. Reading settings never contacts a provider."""

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    max_tokens: int = 700
    timeout: float = 20
    temperature: float = 0

    @classmethod
    def from_env(cls):
        base = os.getenv("CASEFLOW_BASE_URL", "").rstrip("/")
        model, key = os.getenv("CASEFLOW_MODEL", ""), os.getenv("CASEFLOW_API_KEY", "")
        parsed = urlsplit(base)
        if (
            not base
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CASEFLOW_BASE_URL must be an HTTPS API base URL without credentials/query")
        if not model or not key:
            raise ValueError("Set CASEFLOW_MODEL and CASEFLOW_API_KEY locally; never paste secrets into chat")
        tokens = int(os.getenv("CASEFLOW_MAX_TOKENS", "700"))
        timeout = float(os.getenv("CASEFLOW_MODEL_TIMEOUT", "20"))
        temperature = float(os.getenv("CASEFLOW_TEMPERATURE", "0"))
        if not 1 <= tokens <= 2000 or not 1 <= timeout <= 60 or not 0 <= temperature <= 1:
            raise ValueError("Invalid bounded model parameters")
        return cls(base, model, key, tokens, timeout, temperature)

    def public(self):
        return {
            "provider_host": urlsplit(self.base_url).hostname,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "temperature": self.temperature,
            "verification": "not-verified",
        }


def configured_planner(mode="mock", *, strategy="improved", quota=None, client=None):
    from caseflow.planner import ModelPlanner, OfflinePlanner

    if mode == "mock":
        return OfflinePlanner()
    settings = ModelSettings.from_env()
    if os.getenv("CASEFLOW_ALLOW_PAID") != "1":
        raise ValueError("Live requests disabled: explicit CASEFLOW_ALLOW_PAID=1 required after authorization")
    from caseflow.spending import Quota

    planner = ModelPlanner(
        settings.base_url + "/chat/completions",
        settings.model,
        settings.api_key,
        enabled=True,
        strategy=strategy,
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
        temperature=settings.temperature,
        client=client,
    )
    try:
        planner.quota = quota or Quota.from_env(settings)
    except ValueError:
        planner.close()
        raise
    return planner
