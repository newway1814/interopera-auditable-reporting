from __future__ import annotations

import re
import json
import os
import urllib.request
from dataclasses import dataclass

from interopera.computation import Figure


NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z])")


@dataclass(frozen=True)
class FirewallResult:
    passed: bool
    allowed_numbers: tuple[str, ...]
    narrative_numbers: tuple[str, ...]
    unexpected_numbers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "allowed_numbers": list(self.allowed_numbers),
            "narrative_numbers": list(self.narrative_numbers),
            "unexpected_numbers": list(self.unexpected_numbers),
        }


def _tokens(value: str) -> set[str]:
    return {token.replace(",", "") for token in NUMBER_PATTERN.findall(value)}


def generate_narrative(figures: list[Figure]) -> str:
    """Default narrative is deterministic and intentionally contains no numbers."""
    statuses = {figure.status for figure in figures}
    if "BREACH" in statuses:
        return "The deterministic checks identified at least one compliance exception requiring review. All conclusions in this report are linked to computed figures and their source paths."
    if "AT LIMIT" in statuses:
        return "The deterministic checks found the portfolio within its limits, with at least one measure at its boundary. All conclusions are linked to computed figures and their source paths."
    return "The deterministic checks found the portfolio within its configured limits. All conclusions are linked to computed figures and their source paths."


def generate_openai_narrative(figures: list[Figure], model: str) -> str:
    """Optional off-path commentary via Responses API; output still faces the firewall."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --narrative-provider openai")
    computed_snapshot = [
        {"metric": figure.metric, "value": figure.value, "limit": figure.limit, "status": figure.status}
        for figure in figures
    ]
    body = {
        "model": model,
        "store": False,
        "max_output_tokens": 300,
        "instructions": (
            "Write concise portfolio compliance commentary. Do not perform calculations. "
            "Do not include any digits or numeric claims. Use only qualitative statements grounded in the supplied deterministic results."
        ),
        "input": json.dumps(computed_snapshot, ensure_ascii=False),
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.load(response)
    texts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    if not texts:
        raise RuntimeError("OpenAI Responses API returned no narrative text")
    return "\n".join(texts).strip()


def check_narrative_firewall(narrative: str, figures: list[Figure]) -> FirewallResult:
    allowed: set[str] = set()
    for figure in figures:
        for value in (figure.value, figure.limit, figure.utilization):
            allowed.update(_tokens(value))
    narrative_numbers = _tokens(narrative)
    unexpected = narrative_numbers - allowed
    return FirewallResult(
        passed=not unexpected,
        allowed_numbers=tuple(sorted(allowed)),
        narrative_numbers=tuple(sorted(narrative_numbers)),
        unexpected_numbers=tuple(sorted(unexpected)),
    )
