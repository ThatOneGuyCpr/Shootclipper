"""Structured replies from Claude.

Current models (Sonnet 5.5, Opus 5.5 and newer) reject forced tool use
(tool_choice "tool"/"any") with a 400, so structured data comes back through
structured outputs instead: output_config.format with a JSON schema. The API
guarantees the reply text parses against the schema.
"""
import copy
import json

from . import jobs


def api_schema(schema):
    """Adapt a schema to what structured outputs accepts (additionalProperties: false
    everywhere, unsupported limits such as minimum/maximum moved into descriptions)."""
    try:
        from anthropic import transform_schema
        return transform_schema(copy.deepcopy(schema))
    except Exception:
        return _fallback_schema(copy.deepcopy(schema))


def _fallback_schema(s):
    if isinstance(s, dict):
        for k in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
            s.pop(k, None)
        if s.get("type") == "object":
            s["additionalProperties"] = False
        for v in s.values():
            _fallback_schema(v)
    elif isinstance(s, list):
        for v in s:
            _fallback_schema(v)
    return s


def usage_tokens(usage):
    """(input tokens incl. cache, output tokens)."""
    tin = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    return tin, usage.output_tokens or 0


def structured(client, *, model, schema, messages, system=None, max_tokens=4000):
    """One call whose reply is a JSON object matching `schema`.

    Returns (obj, [responses]) so callers can total the cost of every attempt.
    Retries once with more room if the reply was cut off.

    Always streams: the SDK refuses large non-streamed requests ("Streaming is required
    for operations that may take longer than 10 minutes"), and long interviews need room.
    """
    kw = {"system": system} if system else {}
    fmt = {"format": {"type": "json_schema", "schema": api_schema(schema)}}
    responses = []
    for attempt in range(2):
        with client.messages.stream(model=model, max_tokens=max_tokens, messages=messages,
                                    output_config=fmt, **kw) as stream:
            for _ in stream:
                jobs.check()  # Stop works mid-reply too
            resp = stream.get_final_message()
        responses.append(resp)
        if resp.stop_reason == "max_tokens" and attempt == 0:
            max_tokens = min(max_tokens * 2, 64000)
            continue
        break
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's reply was cut off before it finished.")
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    try:
        return json.loads(text), responses
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude's reply wasn't valid JSON ({e}).") from e
