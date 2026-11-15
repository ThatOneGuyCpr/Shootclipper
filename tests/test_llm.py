"""Claude requests: structured replies, cut-off retries, plain-language errors."""
import pytest

from clipper import llm

SCHEMA = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "string"}},
                                           "score": {"type": "integer", "minimum": 1, "maximum": 5}},
          "required": ["picks"]}


def ask(**kw):
    return llm.structured(llm.client(), model="claude-sonnet-5-5", schema=SCHEMA,
                          messages=[{"role": "user", "content": "hi"}], **kw)


def test_structured_reply_is_streamed_without_forced_tools(claude):
    claude.answers["picks"] = {"picks": ["a"], "score": 3}
    obj, responses = ask()
    assert obj == {"picks": ["a"], "score": 3}
    body = claude.requests[0]
    assert body["stream"] is True
    assert "tool_choice" not in body          # Sonnet 5.5 rejects forced tool use
    sent = body["output_config"]["format"]["schema"]
    assert sent["additionalProperties"] is False
    assert "minimum" not in sent["properties"]["score"]     # unsupported keywords are stripped
    assert len(responses) == 1


def test_cut_off_reply_is_retried_with_more_room(claude):
    calls = []

    def on_request(body):
        calls.append(body["max_tokens"])
        if len(calls) == 1:
            return '{"picks": ["a', "max_tokens"
        return {"picks": ["a", "b"]}, "end_turn"
    claude.on_request = on_request
    obj, responses = ask(max_tokens=32000)
    assert obj == {"picks": ["a", "b"]}
    assert calls == [32000, 64000]
    assert len(responses) == 2                # both attempts are billed, so both are returned


def test_reply_cut_off_twice_is_an_error(claude):
    claude.on_request = lambda body: ('{"picks": [', "max_tokens")
    with pytest.raises(RuntimeError, match="cut off"):
        ask()


@pytest.mark.parametrize("status, message, expect", [
    (401, "invalid x-api-key", "rejected the API key"),
    (400, "Your credit balance is too low to access the Anthropic API.", "credit has run out"),
    (404, "model: claude-nope not found", "isn't available on this key"),
])
def test_api_errors_say_what_to_do(claude, status, message, expect):
    claude.on_request = lambda body: ("__error__", status, message)
    with pytest.raises(RuntimeError, match=expect):
        ask()


def test_no_api_key_is_a_clear_error(monkeypatch):
    from clipper import config
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    with pytest.raises(RuntimeError, match="No API key"):
        llm.client()


def test_cost_uses_listed_prices(claude):
    claude.answers["picks"] = {"picks": []}
    claude.usage = {"input_tokens": 1_000_000, "output_tokens": 100_000}
    _, responses = ask()
    assert llm.cost("claude-sonnet-5-5", responses) == pytest.approx(2.00 + 1.00)
    assert llm.cost("claude-haiku-5-5", responses) == pytest.approx(0.10 + 0.05)
