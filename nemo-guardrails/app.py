"""Small NeMo Guardrails policy service for the independent demo agents.

The OpenShift AI NeMo Guardrails service exposes the documented standalone
``/v1/guardrails/checks`` shape.  These rails deliberately use deterministic
checks: a policy decision on a PR proposal must not depend on an extra model
call or an external model being available.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from nemoguardrails import RailsConfig
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

CONFIG_PATH = Path(os.getenv("NEMO_CONFIG_PATH", "/app/config"))
MAX_CONTENT_BYTES = int(os.getenv("GUARDRAILS_MAX_CONTENT_BYTES", "1048576"))

SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
PROMPT_INJECTION_PATTERNS = {
    "instruction_override": re.compile(
        r"\b(?:ignore|disregard|override)\b.{0,80}\b(?:previous|prior|system|instructions?)\b",
        re.IGNORECASE,
    ),
    "prompt_exfiltration": re.compile(
        r"\b(?:reveal|print|show|exfiltrate)\b.{0,80}\b(?:system prompt|secrets?|tokens?|credentials?)\b",
        re.IGNORECASE,
    ),
}


class GuardrailCheck(BaseModel):
    content: str = Field(max_length=MAX_CONTENT_BYTES)
    phase: str = Field(pattern="^(input|output|pr_proposal)$")
    agent_type: str = Field(pattern="^(doc-gen|openapi-gen|catalog-gen|compliance-scan)$")


class GuardrailResult(BaseModel):
    allowed: bool
    violations: list[str]


def _violations(content: str, phase: str) -> list[str]:
    violations = [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(content)]
    if phase == "input":
        violations.extend(
            name for name, pattern in PROMPT_INJECTION_PATTERNS.items() if pattern.search(content)
        )
    return violations


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Parse the NeMo configuration at startup. A bad policy image must never
    # look healthy; the caller will then retain its documented fail-open path.
    RailsConfig.from_path(str(CONFIG_PATH))
    logger.info("Loaded NeMo Guardrails configuration from %s", CONFIG_PATH)
    yield


app = FastAPI(title="NeMo Guardrails for Agentic Doc Generation", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/guardrails/checks", response_model=GuardrailResult)
async def checks(check: GuardrailCheck) -> GuardrailResult:
    violations = _violations(check.content, check.phase)
    if violations:
        logger.warning("Blocked %s check for %s: %s", check.phase, check.agent_type, ", ".join(violations))
    return GuardrailResult(allowed=not violations, violations=violations)
