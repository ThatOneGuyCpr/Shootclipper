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


def explain(e, model=""):
    """Turn an Anthropic SDK error into a sentence that says what to do. Returns None for
    anything that isn't an API error."""
    try:
        import anthropic
    except Exception:
        return None
    if not isinstance(e, anthropic.APIError):
        return None
    text = str(getattr(e, "message", "") or e).lower()
    status = getattr(e, "status_code", None)
    if isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError)) and status is None:
        return "Couldn't reach Claude. Check your internet connection and try again."
    if status == 401:
        return "Claude rejected the API key. Paste a fresh one in Settings."
    if "credit balance" in text or "billing" in text:
        return ("Your Claude API credit has run out. Add credit at platform.claude.com under "
                "Billing, then try again.")
    if status == 403:
        return "This API key isn't allowed to do that. Check the key's permissions at platform.claude.com."
    if status == 404 and "model" in text:
        return f"The model “{model}” isn't available on this key. Pick another one in Settings."
    if status == 429:
        return "Claude is rate-limiting this key right now. Wait a minute and try again."
    if status == 413 or "too long" in text or "too large" in text:
        return "This interview is too long to send to Claude in one piece."
    if status and status >= 500:
        return "Claude's servers are busy right now. Try again in a few minutes."
    return None


def friendly(fn):
    """Decorator: re-raise API errors as RuntimeError with a plain-language message."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except jobs.Cancelled:
            raise
        except Exception as e:
            msg = explain(e, kw.get("model", ""))
            if msg:
                raise RuntimeError(msg) from e
            raise
    return wrap


def client():
    import anthropic
    from . import config
    key = config.get_api_key()
    if not key:
        raise RuntimeError("No API key saved. Add one in Settings, or switch Analysis to Paste.")
    return anthropic.Anthropic(api_key=key)


def cost(model, responses, searches=0):
    """Dollar cost of a list of responses at the listed prices."""
    from . import config
    pin, pout = config.MODEL_PRICES.get(model, (2.0, 10.0))
    total = searches * 0.01
    for r in responses:
        tin, tout = usage_tokens(r.usage)
        total += tin / 1e6 * pin + tout / 1e6 * pout
    return total


@friendly
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
