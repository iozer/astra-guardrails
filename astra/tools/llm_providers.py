"""Pluggable LLM providers for the Astra repair loop.

This package cannot assume a specific hosted API is available.
So the provider interface is deliberately simple:

- Input: a single prompt string
- Output: JSON Patch operations (RFC 6902 subset) as a JSON array

Providers:
- mock: returns []
- cmd: runs an external command, passes prompt on stdin, expects JSON on stdout
- openai: example implementation using the OpenAI HTTP API (requires network)

Important: This repository's execution environment may not have network access; therefore the openai provider is provided as code, but is not executed by default.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


class LLMProvider(Protocol):
    def propose_patches(self, prompt: str) -> List[Dict[str, Any]]:
        ...


@dataclass
class MockProvider:
    def propose_patches(self, prompt: str) -> List[Dict[str, Any]]:
        return []


@dataclass
class CmdProvider:
    """Run an external command.

    The command receives the prompt on stdin and must print a JSON array of patch ops.
    """

    command: List[str]
    timeout_s: int = 60

    def propose_patches(self, prompt: str) -> List[Dict[str, Any]]:
        proc = subprocess.run(
            self.command,
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"LLM cmd provider failed: rc={proc.returncode} stderr={proc.stderr.decode('utf-8', errors='replace')}")
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        data = json.loads(out) if out else []
        if isinstance(data, dict) and "patch" in data:
            data = data["patch"]
        if not isinstance(data, list):
            raise RuntimeError("LLM cmd provider must output a JSON array")
        return data


@dataclass
class OpenAIProvider:
    """Example provider for OpenAI's Chat Completions API.

    You must set:
    - OPENAI_API_KEY

    Optional:
    - OPENAI_MODEL (default: gpt-4o-mini)
    - OPENAI_BASE_URL (default: https://api.openai.com)

    NOTE: API surfaces can evolve. If this breaks, prefer the `cmd` provider.
    """

    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com"
    timeout_s: int = 60

    def propose_patches(self, prompt: str) -> List[Dict[str, Any]]:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        model = os.getenv("OPENAI_MODEL", self.model)
        base = os.getenv("OPENAI_BASE_URL", self.base_url).rstrip("/")

        # Chat Completions request
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You output ONLY valid JSON patch arrays. No prose."},
                {"role": "user", "content": prompt},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{base}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        patch = json.loads(content)
        if not isinstance(patch, list):
            raise RuntimeError("OpenAI response was not a JSON patch array")
        return patch


def make_provider(kind: str, *, cmd: Optional[str] = None) -> LLMProvider:
    kind = kind.lower().strip()
    if kind == "mock":
        return MockProvider()
    if kind == "cmd":
        if not cmd:
            raise ValueError("cmd provider requires --cmd")
        # split like a shell would (simple)
        command = cmd.split()
        return CmdProvider(command)
    if kind == "openai":
        return OpenAIProvider()
    raise ValueError(f"Unknown provider kind: {kind}")
