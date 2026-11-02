"""Claude API transport + the ONLINE tactical brain.

DUELAI has two brains for the live battle loop:
  offline - local Ollama (brain.Brain._chat), free, ~1-3 s, 8B judgement
  online  - Claude over the Messages API (OnlineBrain here), paid per call,
            much better judgement, needs the internet

brain.Brain owns the prompts and history for both, and drops to the local
model for any decision the online call fails (network, 429, refusal, bad
key), so switching to online can never make the opponent go quiet as long
as Ollama is up.

Raw stdlib HTTP by design, like strategist.py: DUELAI has no pip
dependencies and the Messages API is plain JSON over HTTPS.

Model notes (2026-10):
  claude-sonnet-5-5 - default online model. Sent with thinking
      {"type": "between_tools"} (= no extended thinking) at low effort:
      the assessment field already makes it reason out loud, and combat
      tempo wants the reply in a couple of seconds.
  claude-opus-5-5   - smarter, slower, 2x the price. Thinking cannot be
      switched off on this model, so it runs adaptive at the chosen effort.
"""

import json
import time
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models/"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_ONLINE_MODEL = "claude-sonnet-5-5"
DEFAULT_ONLINE_EFFORT = "low"
EFFORTS = ("low", "medium", "high")

# $ per million tokens: input, output, cache read.  Cache writes (5-minute
# TTL) cost 1.25x input.  Only used for the per-battle cost line.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}


class ApiError(Exception):
    pass


def post_messages(api_key, body, timeout):
    """POST /v1/messages with server-side refusal fallback. Returns the
    decoded response; raises ApiError with a short reason on any failure,
    including a refusal (the caller decides what silence means)."""
    body = dict(body)
    body.setdefault("fallbacks", "default")
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": FALLBACK_BETA,
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise ApiError("http %s %s" % (e.code, detail[:120]))
    except Exception as e:
        raise ApiError("%s: %s" % (type(e).__name__, str(e)[:120]))
    if data.get("stop_reason") == "refusal":
        cat = (data.get("stop_details") or {}).get("category") or "?"
        raise ApiError("refused (%s)" % cat)
    return data


def response_text(data):
    return "\n".join(b.get("text", "") for b in data.get("content", [])
                     if b.get("type") == "text").strip()


def usage_cost(model, usage):
    """Dollar cost of one response's usage block (0.0 for unknown models)."""
    p = None
    for k in sorted(PRICES, key=len, reverse=True):
        if (model or "").startswith(k):
            p = PRICES[k]
            break
    if not p or not usage:
        return 0.0
    inp, out, cread = p
    return (usage.get("input_tokens", 0) * inp
            + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
            + usage.get("cache_read_input_tokens", 0) * cread
            + usage.get("output_tokens", 0) * out) / 1e6


def thinking_for(model, effort, think=False):
    """The `thinking` field for a fast tactical call, or None to omit it.

    Only Claude Sonnet 5.5 accepts between_tools (its "thinking off"), and
    only at effort high or below.  Opus 5.5 / Fable reject any attempt to
    disable thinking, so they run adaptive (omitting the field)."""
    if not think and model.startswith("claude-sonnet-5-5") \
            and effort in EFFORTS:
        return {"type": "between_tools"}
    return None


def orders_schema(with_say):
    """Structured-output schema for one tactical decision. Property order
    is generation order: assessment first, so the orders follow from it."""
    props = {"assessment": {"type": "string"}}
    if with_say:
        props["say"] = {"type": "string"}
    props["orders"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"verb": {"type": "string"},
                           "arg": {"type": "string"}},
            "required": ["verb", "arg"],
            "additionalProperties": False,
        },
    }
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}


class OnlineBrain:
    """Claude as the live tactical layer.  Brain.decide calls chat()."""

    def __init__(self, api_key, model=DEFAULT_ONLINE_MODEL,
                 effort=DEFAULT_ONLINE_EFFORT, think=False, timeout=20,
                 post=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_ONLINE_MODEL).strip()
        self.effort = effort if effort in EFFORTS else DEFAULT_ONLINE_EFFORT
        self.think = bool(think)
        self.timeout = timeout
        self._post = post or post_messages      # injectable for selfcheck
        self.ok = None if self.api_key else False
        self.last_error = "" if self.api_key else "no api key"
        self.reset_stats()

    def reset_stats(self):
        self.stats = {"calls": 0, "fails": 0, "secs": 0.0, "cost": 0.0,
                      "in": 0, "out": 0, "cache_read": 0}

    def ping(self):
        """GET /v1/models/<id>: free, and proves both the key and the
        model name.  Sets and returns self.ok."""
        if not self.api_key:
            self.ok, self.last_error = False, "no api key"
            return False
        req = urllib.request.Request(MODELS_URL + self.model, headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                json.load(r)
            self.ok, self.last_error = True, ""
        except urllib.error.HTTPError as e:
            self.ok, self.last_error = False, "http %s on model check" % e.code
        except Exception as e:
            # offline: leave ok False so the local brain carries the fight
            self.ok = False
            self.last_error = "%s: %s" % (type(e).__name__, str(e)[:80])
        return self.ok

    def body(self, system, messages, with_say, max_tokens=4000):
        b = {
            "model": self.model,
            "max_tokens": max_tokens,
            # the system prompt (doctrine + intel + lore + admiral's plan)
            # is identical for every decision in a battle: cache it
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": {"effort": self.effort,
                              "format": {"type": "json_schema",
                                         "schema": orders_schema(with_say)}},
        }
        th = thinking_for(self.model, self.effort, self.think)
        if th:
            b["thinking"] = th
        return b

    def chat(self, system, messages, with_say):
        """One decision. Returns the JSON text; raises ApiError."""
        t0 = time.time()
        try:
            data = self._post(self.api_key,
                              self.body(system, messages, with_say),
                              self.timeout)
        except ApiError as e:
            self.stats["fails"] += 1
            self.last_error = str(e)
            raise
        st = self.stats
        st["calls"] += 1
        st["secs"] += time.time() - t0
        u = data.get("usage") or {}
        st["in"] += u.get("input_tokens", 0) + \
            u.get("cache_creation_input_tokens", 0)
        st["cache_read"] += u.get("cache_read_input_tokens", 0)
        st["out"] += u.get("output_tokens", 0)
        st["cost"] += usage_cost(data.get("model") or self.model, u)
        if data.get("stop_reason") == "max_tokens":
            self.last_error = "max_tokens"
            raise ApiError("max_tokens")
        self.last_error = ""
        return response_text(data)

    def summary(self):
        st = self.stats
        if not st["calls"] and not st["fails"]:
            return ""
        avg = st["secs"] / st["calls"] if st["calls"] else 0
        return ("online brain (%s, %s effort): %d calls, %d failed, "
                "avg %.1fs, ~$%.3f" % (self.model, self.effort, st["calls"],
                                       st["fails"], avg, st["cost"]))
