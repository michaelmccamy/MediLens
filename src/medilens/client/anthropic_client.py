"""Synchronous wrapper around the Anthropic Messages API.

This is the only module in the codebase allowed to talk to the model
endpoint. Everything the compliance guardrails require of a model call is
layered here so call sites cannot forget it: client-side rate limiting,
retries with backoff, pre-send token counting, structured JSON output, and
response metadata capture for the audit store.

PHI note: only synthetic and de-identified data may pass through this client
until a BAA-covered deployment path exists (CLAUDE.md section 2). Nothing
from a request or response body is ever logged in this module.

Temperature note: CLAUDE.md section 5 asks for a low temperature for
stability, but the locked model (claude-sonnet-5) rejects non-default
sampling parameters with a 400 error, so no temperature is sent. Output
stability is provided instead by schema-enforced structured outputs
(output_config.format), which guarantee valid JSON matching the given
schema. This conflict was raised and accepted at implementation time.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from medilens.client.rate_limiter import RequestAndTokenLimiter
from medilens.client.retry import RetryPolicy, execute_with_retries
from medilens.config import Settings

logger = logging.getLogger(__name__)

# Non-streaming requests above roughly 16k output tokens risk SDK HTTP
# timeouts, so this is the ceiling until a streaming path is added.
DEFAULT_MAX_OUTPUT_TOKENS = 16000


class ModelResponseError(Exception):
    """The model responded, but not in a form this system can accept.

    Raised for refusals, truncated output, and unparseable JSON. CLAUDE.md
    section 7 requires failing loudly on unexpected model output rather
    than guessing, so callers must handle this explicitly.
    """


@dataclass
class StructuredResult:
    """A parsed model response plus the metadata the audit store needs.

    CLAUDE.md section 3 (guardrail 7) requires every recommendation to be
    reconstructable, so the model name, request id, stop reason, and token
    usage travel with the parsed data instead of being discarded here.
    """

    data: Any
    model: str
    request_id: str | None
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class ModelClient:
    """Rate-limited, retrying, structured-output client for the Messages API."""

    def __init__(
        self,
        settings: Settings,
        limiter: RequestAndTokenLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        anthropic_client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model_name = settings.model_name
        if limiter is None:
            limiter = RequestAndTokenLimiter(
                max_requests_per_minute=settings.max_requests_per_minute,
                max_tokens_per_minute=settings.max_tokens_per_minute,
            )
        self._limiter = limiter
        if retry_policy is None:
            retry_policy = RetryPolicy()
        self._retry_policy = retry_policy
        if anthropic_client is None:
            # max_retries=0 disables the SDK's built-in retry. It would stack
            # multiplicatively with the policy in client/retry.py and make
            # backoff timing unauditable, so exactly one retry layer exists.
            anthropic_client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                max_retries=0,
            )
        self._client = anthropic_client

    def count_input_tokens(self, system: str, messages: list[dict[str, Any]]) -> int:
        """Estimate input tokens via the Token Counting API before sending.

        CLAUDE.md section 5 requires throttling before a hard limit is hit,
        which is only possible with a pre-send estimate. This call consumes
        request quota but no token quota, and gets the same retry treatment
        as any other API call.
        """
        self._limiter.acquire_request()

        def send_count_request() -> Any:
            return self._client.messages.count_tokens(
                model=self.model_name,
                system=system,
                messages=messages,
            )

        count_response = execute_with_retries(send_count_request, self._retry_policy)
        return count_response.input_tokens

    def create_structured(
        self,
        system: str,
        user_content: str,
        json_schema: dict[str, Any],
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> StructuredResult:
        """Send one request and return schema-conforming parsed JSON.

        The output schema is enforced server-side via output_config.format,
        which replaces prose-based "return strict JSON" prompting and the
        temperature knob as the stability mechanism (see module docstring).
        Parsing is still defensive: a response that cannot be parsed raises
        ModelResponseError rather than propagating half-parsed data.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        estimated_input_tokens = self.count_input_tokens(system, messages)
        # Output tokens draw from the same per-minute budget as input tokens,
        # so the reservation covers the worst case of a full-length response.
        self._limiter.acquire(estimated_input_tokens + max_output_tokens)

        def send_message_request() -> Any:
            return self._client.messages.create(
                model=self.model_name,
                max_tokens=max_output_tokens,
                system=system,
                messages=messages,
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": json_schema,
                    }
                },
            )

        response = execute_with_retries(send_message_request, self._retry_policy)

        self._check_stop_reason(response)
        parsed_data = self._parse_json_text(response)

        request_id = getattr(response, "_request_id", None)
        result = StructuredResult(
            data=parsed_data,
            model=response.model,
            request_id=request_id,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return result

    def _check_stop_reason(self, response: Any) -> None:
        """Reject responses that ended for any reason other than completion.

        A refusal or a max_tokens truncation cannot produce trustworthy
        structured output, and passing it downstream would violate the
        fail-loudly rule (CLAUDE.md section 7).
        """
        if response.stop_reason == "refusal":
            raise ModelResponseError(
                "model refused the request (stop_reason=refusal); "
                "the response cannot be used"
            )
        if response.stop_reason == "max_tokens":
            raise ModelResponseError(
                "model output was truncated (stop_reason=max_tokens); "
                "raise max_output_tokens and retry"
            )
        if response.stop_reason != "end_turn":
            raise ModelResponseError(
                f"unexpected stop_reason: {response.stop_reason}"
            )

    def _parse_json_text(self, response: Any) -> Any:
        """Extract the text block and parse it as JSON, failing loudly.

        The error message intentionally excludes the response content: once
        real notes flow through this system the content could contain PHI,
        and exceptions frequently end up in logs (CLAUDE.md section 3,
        guardrail 6).
        """
        text_value: str | None = None
        for block in response.content:
            if block.type == "text":
                text_value = block.text
                break
        if text_value is None:
            raise ModelResponseError("model response contained no text block")
        try:
            parsed_data = json.loads(text_value)
        except json.JSONDecodeError as exc:
            raise ModelResponseError(
                "model response text was not valid JSON despite the "
                "structured output constraint; content withheld from this "
                "error to keep PHI out of logs"
            ) from exc
        return parsed_data
