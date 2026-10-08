"""Tests for the OpenAI-compatible provider."""

import io
import json
import urllib.error

from ferrum.model import (
    AuthenticationError,
    OpenAICompatibleProvider,
    ProviderError,
    RateLimitError,
    list_models,
    probe_endpoint,
)
from ferrum.toolcalls import MALFORMED_JSON, extract_tool_calls


def _message_response(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": message}]}).encode("utf-8")


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _provider(**kwargs):
    return OpenAICompatibleProvider("http://localhost:11434/v1", "test-model", **kwargs)


def _set_urlopen(monkeypatch, func):
    monkeypatch.setattr("ferrum.model.urllib.request.urlopen", func)


def test_content_only_response(monkeypatch):
    _set_urlopen(
        monkeypatch, lambda req, timeout: FakeResponse(_message_response("hello"))
    )
    response = _provider().complete([{"role": "user", "content": "hi"}], [])
    assert response.content == "hello"
    assert response.tool_calls == []


def test_tool_calls_are_parsed(monkeypatch):
    raw = _message_response(
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "main.c"}',
                },
            }
        ]
    )
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(raw))
    response = _provider().complete([], [])
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments == {"path": "main.c"}
    assert call.id == "call-1"


def test_malformed_tool_arguments_are_kept_for_feedback(monkeypatch):
    raw = _message_response(
        tool_calls=[
            {
                "id": "c",
                "type": "function",
                "function": {"name": "apply_patch", "arguments": '{"old": "broken'},
            }
        ]
    )
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(raw))
    response = _provider().complete([], [])
    assert response.tool_calls[0].arguments[MALFORMED_JSON] == '{"old": "broken'


def test_schema_envelope_arguments_are_unwrapped(monkeypatch):
    envelope = json.dumps(
        {
            "type": "function",
            "function": "list_files",
            "parameters": {"path": "src"},
        }
    )
    raw = _message_response(
        tool_calls=[
            {
                "id": "c",
                "type": "function",
                "function": {"name": "list_files", "arguments": envelope},
            }
        ]
    )
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(raw))
    response = _provider().complete([], [])
    assert response.tool_calls[0].arguments == {"path": "src"}


def test_pure_schema_arguments_are_flagged(monkeypatch):
    schema_args = json.dumps({"type": "string", "required": ["path"]})
    raw = _message_response(
        tool_calls=[
            {
                "id": "c",
                "type": "function",
                "function": {"name": "read_file", "arguments": schema_args},
            }
        ]
    )
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(raw))
    response = _provider().complete([], [])
    assert MALFORMED_JSON in response.tool_calls[0].arguments


def _raise_http(code, body=b"boom", counter=None):
    def raiser(req, timeout):
        if counter is not None:
            counter["n"] += 1
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body))

    return raiser


def test_transient_gateway_error_is_retried(monkeypatch):
    counter = {"n": 0}

    def flaky(req, timeout):
        counter["n"] += 1
        if counter["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 502, "bad gateway", {}, io.BytesIO(b"cf")
            )
        return FakeResponse(_message_response("recovered"))

    _set_urlopen(monkeypatch, flaky)
    response = _provider().complete([], [])
    assert response.content == "recovered"
    assert counter["n"] == 2


def test_persistent_gateway_error_fails_after_retries(monkeypatch):
    counter = {"n": 0}
    _set_urlopen(monkeypatch, _raise_http(502, counter=counter))
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "502" in str(exc)
        assert counter["n"] == 3
    else:
        raise AssertionError("expected ProviderError")


def test_client_errors_are_not_retried(monkeypatch):
    counter = {"n": 0}
    _set_urlopen(monkeypatch, _raise_http(400, counter=counter))
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "400" in str(exc)
        assert counter["n"] == 1
    else:
        raise AssertionError("expected ProviderError")


def test_timeouts_are_not_retried(monkeypatch):
    counter = {"n": 0}

    def raiser(req, timeout):
        counter["n"] += 1
        raise TimeoutError("read timed out")

    _set_urlopen(monkeypatch, raiser)
    try:
        _provider(timeout=7).complete([], [])
    except ProviderError as exc:
        assert "did not answer within 7s" in str(exc)
        assert counter["n"] == 1
    else:
        raise AssertionError("expected ProviderError")


def test_http_401_is_authentication_error(monkeypatch):
    _set_urlopen(monkeypatch, _raise_http(401, b"bad key"))
    try:
        _provider().complete([], [])
    except AuthenticationError as exc:
        assert "401" in str(exc)
        assert "bad key" in str(exc)
    else:
        raise AssertionError("expected AuthenticationError")


def test_http_429_is_rate_limit_error(monkeypatch):
    counter = {"n": 0}
    sleeps = []
    monkeypatch.setattr("ferrum.model.time.sleep", sleeps.append)
    _set_urlopen(monkeypatch, _raise_http(429, counter=counter))
    try:
        _provider().complete([], [])
    except RateLimitError as exc:
        assert "429" in str(exc)
        assert counter["n"] == 3
        assert sleeps == [15, 30]
    else:
        raise AssertionError("expected RateLimitError")


def test_429_retries_then_succeeds(monkeypatch):
    counter = {"n": 0}
    sleeps = []
    monkeypatch.setattr("ferrum.model.time.sleep", sleeps.append)

    def flaky(req, timeout):
        counter["n"] += 1
        if counter["n"] <= 2:
            raise urllib.error.HTTPError(
                req.full_url, 429, "slow down", {}, io.BytesIO(b"rate")
            )
        return FakeResponse(_message_response("recovered"))

    _set_urlopen(monkeypatch, flaky)
    response = _provider().complete([], [])
    assert response.content == "recovered"
    assert counter["n"] == 3
    assert sleeps == [15, 30]


def test_probe_endpoint_reports_errors(monkeypatch):
    _set_urlopen(monkeypatch, _raise_http(500, b"oops"))
    models, error = probe_endpoint("http://x/v1")
    assert models == []
    assert error == "HTTP 500"


def test_probe_endpoint_reports_unreachable(monkeypatch):
    def raiser(req, timeout):
        raise urllib.error.URLError("connection refused")

    _set_urlopen(monkeypatch, raiser)
    models, error = probe_endpoint("http://x/v1")
    assert models == []
    assert "connection refused" in str(error)


def test_probe_endpoint_returns_models(monkeypatch):
    data = json.dumps({"data": [{"id": "m1"}]}).encode("utf-8")
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(data))
    assert probe_endpoint("http://x/v1") == (["m1"], None)


def test_http_500_is_provider_error(monkeypatch):
    _set_urlopen(monkeypatch, _raise_http(500, b"oops"))
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "500" in str(exc)
        assert "oops" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_unreachable_server_names_the_url(monkeypatch):
    def raiser(req, timeout):
        raise urllib.error.URLError("connection refused")

    _set_urlopen(monkeypatch, raiser)
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "cannot reach model at http://localhost:11434/v1" in str(exc)
        assert "connection refused" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_malformed_body_is_provider_error(monkeypatch):
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(b"not json"))
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "malformed response" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_payload_contains_model_messages_and_tools(monkeypatch):
    captured = {}

    def spy(req, timeout):
        captured["payload"] = json.loads(req.data)
        captured["auth"] = req.get_header("Authorization")
        captured["ua"] = req.get_header("User-agent")
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, spy)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    _provider(api_key="s3cret").complete([{"role": "user", "content": "hi"}], tools)
    payload = captured["payload"]
    assert payload["model"] == "test-model"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"] == tools
    assert payload["stream"] is False
    assert payload["max_tokens"] == 2048
    assert payload["temperature"] == 0.2
    assert captured["auth"] == "Bearer s3cret"
    assert captured["ua"].startswith("ferrum/")


def test_payload_max_tokens_is_configurable(monkeypatch):
    captured = {}

    def spy(req, timeout):
        captured["payload"] = json.loads(req.data)
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, spy)
    _provider(max_tokens=99).complete([], [])
    assert captured["payload"]["max_tokens"] == 99


def test_no_auth_header_without_api_key(monkeypatch):
    captured = {}

    def spy(req, timeout):
        captured["auth"] = req.get_header("Authorization")
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, spy)
    _provider().complete([], [])
    assert captured["auth"] is None


def test_list_models(monkeypatch):
    data = json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode("utf-8")
    _set_urlopen(monkeypatch, lambda req, timeout: FakeResponse(data))
    assert list_models("http://localhost:11434/v1") == ["a", "b"]


def test_list_models_never_raises(monkeypatch):
    def raiser(req, timeout):
        raise urllib.error.URLError("down")

    _set_urlopen(monkeypatch, raiser)
    assert list_models("http://localhost:11434/v1") == []


def test_list_models_sends_auth_when_key_given(monkeypatch):
    data = json.dumps({"data": [{"id": "a"}]}).encode("utf-8")
    captured = {}

    def spy(req, timeout):
        captured["auth"] = req.get_header("Authorization")
        captured["ua"] = req.get_header("User-agent")
        return FakeResponse(data)

    _set_urlopen(monkeypatch, spy)
    assert list_models("http://x/v1", api_key="sk-abc") == ["a"]
    assert captured["auth"] == "Bearer sk-abc"
    assert captured["ua"].startswith("ferrum/")


# --- JSON tool calls written as plain text ---


def test_extract_bare_json_call():
    content = 'Inspecting first: {"name": "read_file", "arguments": {"path": "main.c"}}'
    calls = extract_tool_calls(content)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "main.c"}


def test_extract_fenced_call_with_parameters_key():
    content = '```json\n{"name": "list_files", "parameters": {}}\n```'
    calls = extract_tool_calls(content)
    assert len(calls) == 1
    assert calls[0].name == "list_files"
    assert calls[0].arguments == {}


def test_extract_json_string_arguments():
    content = json.dumps(
        {"name": "read_file", "arguments": json.dumps({"path": "a.c"})}
    )
    calls = extract_tool_calls(content)
    assert calls[0].arguments == {"path": "a.c"}


def test_extract_malformed_string_arguments_flagged():
    content = json.dumps({"name": "apply_patch", "arguments": "{bad"})
    calls = extract_tool_calls(content)
    assert calls[0].arguments[MALFORMED_JSON] == "{bad"


def test_extract_non_dict_arguments_wrapped():
    content = json.dumps({"name": "x", "arguments": [1, 2]})
    calls = extract_tool_calls(content)
    assert calls[0].arguments == {"_": [1, 2]}


def test_extract_ignores_nameless_json():
    assert extract_tool_calls('{"version": 1} and {"path": "x"}') == []


def test_extract_ignores_invalid_json():
    assert extract_tool_calls("{not json at all") == []


def test_extract_multiple_calls():
    content = (
        '{"name": "list_files", "arguments": {}}\n'
        '{"name": "read_file", "arguments": {"path": "x.c"}}'
    )
    calls = extract_tool_calls(content)
    assert [c.name for c in calls] == ["list_files", "read_file"]
    assert calls[0].id == "text-0"
    assert calls[1].id == "text-1"


def test_extract_empty_content():
    assert extract_tool_calls("") == []


def test_extract_envelope_with_function_name():
    content = (
        '{"type": "function", "function": "read_file", "parameters": {"path": "a.c"}}'
    )
    calls = extract_tool_calls(content)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.c"}


def test_extract_schema_only_arguments_flagged():
    content = (
        '{"name": "read_file", "arguments": {"type": "string", "required": ["path"]}}'
    )
    calls = extract_tool_calls(content)
    assert MALFORMED_JSON in calls[0].arguments


def test_retry_is_reported_to_the_caller(monkeypatch):
    notes = []
    counter = {"n": 0}

    def flaky(req, timeout):
        counter["n"] += 1
        if counter["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 502, "bad gateway", {}, io.BytesIO(b"cf")
            )
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, flaky)
    assert _provider(on_retry=notes.append).complete([], []).content == "ok"
    assert len(notes) == 1
    assert "retrying" in notes[0]


def test_parameter_rejection_relaxes_the_limit(monkeypatch):
    payloads = []
    state = {"n": 0}

    def picky(req, timeout):
        state["n"] += 1
        payloads.append(json.loads(req.data))
        if state["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "bad request",
                {},
                io.BytesIO(b"max_tokens is not supported"),
            )
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, picky)
    response = _provider().complete([{"role": "user", "content": "hi"}], [])
    assert response.content == "ok"
    assert payloads[0]["max_tokens"] == 2048
    assert payloads[1]["max_completion_tokens"] == 2048
    assert "max_tokens" not in payloads[1]
    assert payloads[1]["temperature"] == 0.2  # untouched


def test_parameter_rejection_drops_temperature(monkeypatch):
    payloads = []
    state = {"n": 0}

    def picky(req, timeout):
        state["n"] += 1
        payloads.append(json.loads(req.data))
        if state["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "bad request",
                {},
                io.BytesIO(b"temperature must be between 0 and 2"),
            )
        return FakeResponse(_message_response("ok"))

    _set_urlopen(monkeypatch, picky)
    assert _provider().complete([], []).content == "ok"
    assert "temperature" in payloads[0]
    assert "temperature" not in payloads[1]
    assert "max_tokens" in payloads[1]


def test_unrelated_400_is_not_relaxed(monkeypatch):
    counter = {"n": 0}
    _set_urlopen(
        monkeypatch,
        _raise_http(400, body=b"context length exceeded", counter=counter),
    )
    try:
        _provider().complete([], [])
    except ProviderError as exc:
        assert "context length" in str(exc)
        assert counter["n"] == 1
    else:
        raise AssertionError("expected ProviderError")
