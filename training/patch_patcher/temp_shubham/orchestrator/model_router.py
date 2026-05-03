from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class EndpointConfig:
    url: str
    api_key: str
    timeout_sec: int = 60
    adapter: Optional[str] = None


@dataclass(frozen=True)
class RouterConfig:
    planner: EndpointConfig
    patcher: EndpointConfig
    critic: EndpointConfig


class HFModelRouter:
    """
    Unified endpoint router for planner/patcher/critic.
    Uses OpenAI-compatible chat/completions payload style over HF/vLLM endpoints.
    """

    def __init__(self, config: RouterConfig):
        self.config = config

    def call_planner(self, prompt: str, max_tokens: int = 900) -> Dict[str, Any]:
        return self._call(self.config.planner, prompt, max_tokens)

    def call_patcher(self, prompt: str, max_tokens: int = 1200) -> Dict[str, Any]:
        return self._call(self.config.patcher, prompt, max_tokens)

    def call_critic(self, prompt: str, max_tokens: int = 700) -> Dict[str, Any]:
        return self._call(self.config.critic, prompt, max_tokens)

    def _call(self, endpoint: EndpointConfig, prompt: str, max_tokens: int) -> Dict[str, Any]:
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "top_p": 0.9,
        }
        if endpoint.adapter:
            payload["adapter_name"] = endpoint.adapter

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {endpoint.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=endpoint.timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body)
        return parsed
