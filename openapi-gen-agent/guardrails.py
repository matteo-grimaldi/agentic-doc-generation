"""Optional client for the shared NeMo Guardrails service."""
from __future__ import annotations
import logging
import os
import httpx
logger = logging.getLogger(__name__)
NEMO_GUARDRAILS_URL = os.getenv("NEMO_GUARDRAILS_URL", "").rstrip("/")
AGENT_TYPE = "openapi-gen"
async def check_content(content: str, phase: str) -> str | None:
    if not NEMO_GUARDRAILS_URL: return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.post(f"{NEMO_GUARDRAILS_URL}/v1/guardrails/checks", json={"content": content, "phase": phase, "agent_type": AGENT_TYPE})
            response.raise_for_status()
        result = response.json()
        if result.get("allowed", False): return None
        violations = ", ".join(str(item) for item in result.get("violations", [])) or "policy violation"
        logger.warning("NeMo Guardrails denied %s: %s", phase, violations)
        return violations
    except Exception as exc:
        logger.warning("NeMo Guardrails unavailable for %s check; allowing run: %s", phase, exc)
        return None
