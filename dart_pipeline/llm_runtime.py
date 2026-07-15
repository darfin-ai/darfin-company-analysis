"""Shared Gemini transport controls and content-free call telemetry."""

from __future__ import annotations

import random
import time
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

REQUEST_TIMEOUT_MS = 60_000
MAX_ATTEMPTS = 2
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def create_client() -> genai.Client:
    """Create the only supported Gemini client configuration for pipeline jobs."""
    return genai.Client(
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            # Retries live in generate_content(), keeping the total attempt count visible.
            retry_options=types.HttpRetryOptions(attempts=1),
        )
    )


def generate_content(client: genai.Client, *, operation: str, item_count: int, **kwargs: Any):
    """Run one bounded Gemini call, retrying a transient API response at most once.

    The SDK enforces the request timeout configured by :func:`create_client`. Logging
    deliberately contains only operational metadata, never prompts or credentials.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        print(f"gemini_call start operation={operation} items={item_count} attempt={attempt}")
        try:
            response = client.models.generate_content(**kwargs)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            usage = getattr(response, "usage_metadata", None)
            tokens_out = getattr(usage, "candidates_token_count", 0) or 0
            candidates = getattr(response, "candidates", None) or []
            finish_reason = getattr(candidates[0], "finish_reason", "unknown") if candidates else "unknown"
            print(
                f"gemini_call end operation={operation} items={item_count} attempt={attempt} "
                f"duration_ms={elapsed_ms} output_tokens={tokens_out} finish_reason={finish_reason}"
            )
            return response
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            status = getattr(exc, "code", None)
            transient = isinstance(exc, (errors.APIError, TimeoutError, httpx.TimeoutException)) and (
                isinstance(exc, (TimeoutError, httpx.TimeoutException)) or status in _TRANSIENT_STATUS_CODES
            )
            print(
                f"gemini_call error operation={operation} items={item_count} attempt={attempt} "
                f"duration_ms={elapsed_ms} error_type={type(exc).__name__} transient={transient}"
            )
            if not transient or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(0.5 + random.random() * 0.25)
