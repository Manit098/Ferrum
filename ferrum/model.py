"""Model access through an OpenAI-compatible chat endpoint.

Ollama exposes one at http://localhost:11434/v1; vLLM and friends do too.
Only the endpoints actually needed here are implemented: chat completion
with tool calls, and a best-effort model listing for error hints.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ferrum import __version__
from ferrum.config import ConfigError, normalize_base_url
from ferrum.toolcalls import ToolCall, coerce_arguments

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {500, 502, 503, 504}
# Free tiers meter per minute; waiting out the window beats failing the run.
RATE_LIMIT_BACKOFF = (15, 30)

# max_tokens -> max_completion_tokens -> nothing; temperature can go too.
MAX_PARAM_FIXES = 3


class ProviderError(Exception):
    """The model could not be reached or answered unusably."""


class AuthenticationError(ProviderError):
    pass


class RateLimitError(ProviderError):
    pass


class ParameterRejected(ProviderError):
    """The endpoint refused a request parameter; a variant is worth one try."""


@dataclass(frozen=True)
class ModelResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class ModelProvider(ABC):
    @abstractmethod
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        """One round trip: messages in, content and tool calls out."""


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = 60,
        max_tokens: int = 2048,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        try:
            self.base_url = normalize_base_url(base_url)
        except ConfigError as exc:
            raise ProviderError(str(exc)) from exc
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.on_retry = on_retry
        # Clouds disagree about these two; a 400 relaxes them, once each.
        self._max_tokens_key: str | None = "max_tokens"
        self._temperature: float | None = 0.2

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc or self.base_url

    def _note(self, text: str) -> None:
        """A retry is worth one status line, not a wall of log output."""
        if self.on_retry is not None:
            self.on_retry(text)

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        for _attempt in range(MAX_PARAM_FIXES + 1):
            request = self._request(self._payload(messages, tools))
            try:
                return self._parse(self._send(request))
            except ParameterRejected as exc:
                if not self._relax_parameters(str(exc)):
                    raise
                log.info("endpoint refused a parameter, retrying: %s", exc)
        raise ProviderError(f"model at {self.base_url} kept rejecting the request")

    def _payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        # Without an explicit cap some servers truncate mid-tool-call.
        if self._max_tokens_key is not None:
            payload[self._max_tokens_key] = self.max_tokens
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if tools:
            payload["tools"] = tools
        return payload

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json",
            # Some edges (Cloudflare) reject urllib's default signature.
            "User-Agent": f"ferrum/{__version__}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _relax_parameters(self, detail: str) -> bool:
        """Drop the parameter the endpoint named, if we still have one left."""
        text = detail.lower()
        mentions_limit = "max_tokens" in text or "max_completion_tokens" in text
        if mentions_limit and self._max_tokens_key is not None:
            if self._max_tokens_key == "max_tokens" and "max_tokens" in text:
                self._max_tokens_key = "max_completion_tokens"
            else:
                self._max_tokens_key = None
            return True
        if "temperature" in text and self._temperature is not None:
            self._temperature = None
            return True
        return False

    def _send(self, request: urllib.request.Request) -> bytes:
        # Gateways flake; retry transient failures before giving up.
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body: bytes = response.read()
                    return body
            except urllib.error.HTTPError as exc:
                wait = self._http_retry(exc, attempt)
                if wait is None:
                    raise self._http_error(exc) from exc
                exc.close()
                time.sleep(wait)
            except TimeoutError as exc:
                raise ProviderError(
                    f"model at {self.base_url} did not answer within {self.timeout}s"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise ProviderError(
                        f"cannot reach model at {self.base_url} "
                        f"(is the server running?): {exc.reason}"
                    ) from exc
                log.warning(
                    "cannot reach model (attempt %d/%d), retrying: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    exc.reason,
                )
                self._note(
                    f"cannot reach {self.host} — retrying"
                    f" (attempt {attempt}/{MAX_ATTEMPTS})"
                )
                time.sleep(attempt)
            except OSError as exc:
                raise ProviderError(
                    f"cannot reach model at {self.base_url}: {exc}"
                ) from exc
        raise ProviderError(
            f"cannot reach model at {self.base_url}"
        )  # pragma: no cover

    def _http_retry(
        self, exc: urllib.error.HTTPError, attempt: int
    ) -> int | float | None:
        """Seconds to wait after this status, or None to report it instead."""
        if attempt >= MAX_ATTEMPTS:
            return None
        if exc.code == 429:
            wait = RATE_LIMIT_BACKOFF[attempt - 1]
            log.warning(
                "model server HTTP 429 (rate limited), waiting %ds (attempt %d/%d)",
                wait,
                attempt,
                MAX_ATTEMPTS,
            )
            self._note(
                f"rate limited — waiting {wait}s (attempt {attempt}/{MAX_ATTEMPTS})"
            )
            return wait
        if exc.code in RETRYABLE_STATUS:
            log.warning(
                "model server HTTP %s (attempt %d/%d), retrying",
                exc.code,
                attempt,
                MAX_ATTEMPTS,
            )
            self._note(
                f"endpoint answered HTTP {exc.code} — retrying"
                f" (attempt {attempt}/{MAX_ATTEMPTS})"
            )
            return float(attempt)
        return None

    def _http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            detail = ""
        if exc.code in (401, 403):
            return AuthenticationError(
                f"the model server refused the request (HTTP {exc.code}): {detail}"
            )
        if exc.code == 429:
            return RateLimitError(
                f"rate limited by the model server (HTTP 429): {detail}"
            )
        if exc.code == 400:
            # Likely one of the parameters; complete() decides whether to retry.
            return ParameterRejected(
                f"the model server rejected the request (HTTP 400): {detail}"
            )
        return ProviderError(f"model server returned HTTP {exc.code}: {detail}")

    def _parse(self, raw: bytes) -> ModelResponse:
        try:
            message = json.loads(raw)["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"malformed response from model: {exc}") from exc
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        calls: list[ToolCall] = []
        for entry in message.get("tool_calls") or []:
            if not isinstance(entry, dict):
                continue
            function = entry.get("function") or {}
            name = function.get("name") or ""
            raw_args = function.get("arguments") or "{}"
            calls.append(
                ToolCall(
                    id=str(entry.get("id") or name or len(calls)),
                    name=str(name),
                    arguments=coerce_arguments(raw_args),
                )
            )
        return ModelResponse(content=content, tool_calls=calls)


def probe_endpoint(
    base_url: str, timeout: int = 5, api_key: str = ""
) -> tuple[list[str], str | None]:
    """(models, error) for GET /models. Unlike list_models, failures are
    reported instead of swallowed, so `ferrum doctor` can show why."""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url)
    request.add_header("User-Agent", f"ferrum/{__version__}")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
        models = [str(m["id"]) for m in data.get("data", []) if m.get("id")]
    except urllib.error.HTTPError as exc:
        exc.close()
        return [], f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - report any failure, raise none
        log.debug("model listing failed: %s", exc)
        return [], str(exc) or type(exc).__name__
    return models, None


def list_models(base_url: str, timeout: int = 5, api_key: str = "") -> list[str]:
    """Best-effort model listing for hints; never raises.

    Auth is optional: some endpoints (Cloudflare-fronted clouds) refuse
    anonymous GET /models, while local Ollama ignores the header.
    """
    models, _error = probe_endpoint(base_url, timeout=timeout, api_key=api_key)
    return models
