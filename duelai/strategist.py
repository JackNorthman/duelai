"""The Claude strategist: debrief-and-plan.

Two jobs, both OUTSIDE the live battle loop:
  plan()    - at staging: read the matchup, the lesson store and the
              sparring table, write a short battle plan the local brain
              receives as its admiral's orders.
  debrief() - after a battle: read the measured facts and the track,
              write a 2-3 sentence analysis and (only when the battle
              taught something new) one lesson for the store.

Raw stdlib HTTP by design - DUELAI has no pip dependencies and the
Anthropic API is plain JSON over HTTPS.  Everything degrades to silence:
no key, network down, refusal, bad JSON -> the local stack fights on
exactly as before.

Config (data/config.json):
  api_key            - Anthropic API key (sk-ant-...)
  api_model          - default claude-opus-5-5
  strategist_effort  - low|medium|high (default high)
  strategist         - master switch

2026-10-23: Opus 5 -> Opus 5.5.  Opus 5.x always thinks, and the thinking
counts against max_tokens - the old 700/600 caps could cut a plan off
mid-thought, so the caps are now generous (only what is written is
billed).  The debrief comes back through a JSON schema instead of
string-slicing the reply.
"""

import json

from . import online

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
TIMEOUT = 180

SYSTEM = """You are the fleet admiral advising the AI opponent in a Star \
Trek: Bridge Commander duel against a human player. Your tactical layer is \
a small local model with fast reflexes but weak judgement; your job is the \
judgement. Be concrete and numeric where the data allows. Never pad."""


class Strategist:
    def __init__(self, api_key="", model=DEFAULT_MODEL,
                 effort=DEFAULT_EFFORT):
        self.api_key = (api_key or "").strip()
        self.model = model or DEFAULT_MODEL
        self.effort = effort if effort in online.EFFORTS else DEFAULT_EFFORT
        self.last_error = ""
        self.cost = 0.0             # running $ since server start

    def ok(self):
        return bool(self.api_key)

    # ------------------------------------------------------------ transport

    def _call(self, user_text, max_tokens=16000, schema=None):
        """One Messages API call. Returns response text or "" (error)."""
        if not self.ok():
            self.last_error = "no api key"
            return ""
        oc = {"effort": self.effort}
        if schema:
            oc["format"] = {"type": "json_schema", "schema": schema}
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": user_text}],
            "output_config": oc,
        }
        try:
            data = online.post_messages(self.api_key, body, TIMEOUT)
        except online.ApiError as e:
            self.last_error = str(e)
            return ""
        self.cost += online.usage_cost(data.get("model") or self.model,
                                       data.get("usage") or {})
        if data.get("stop_reason") == "max_tokens":
            self.last_error = "max_tokens"
            return ""
        self.last_error = ""
        return online.response_text(data)

    # ------------------------------------------------------------ the jobs

    def plan(self, ctx):
        """ctx: dict with player_ship, own_ship, own_label, difficulty,
        matchup, guns, reach, lessons, sparring, recent.  Returns plan
        text or "".

        `guns`/`reach` are not decoration.  Asked for a "preferred range
        band" while told only the hull and broadside, and handed a lesson
        saying the knife band bleeds us, the strategist quite reasonably
        planned a logged duel for the 500-900 band - in a Challenger whose
        beams reach 125m.  She held that range for 137 ticks, fired almost
        nothing, and lost 91 hull to 1 dealt."""
        ctx = self._plan_defaults(dict(ctx))
        prompt = (
            "STAGE A BATTLE PLAN.\n"
            "Your ship: %(own_label)s (%(own_ship)s). Enemy (human): "
            "%(player_ship)s. Difficulty: %(difficulty)s.\n"
            "Paper matchup: %(matchup)s\n"
            "YOUR GUNS: %(guns)s\n"
            "HARD CONSTRAINT: your weapons do NOT reach beyond %(reach)dm. "
            "Any range band you name must be one you can shoot from. If a "
            "lesson below says to avoid the band your guns live in, that "
            "lesson was learned in a different hull - obey your reach, not "
            "the lesson, and solve the problem some other way (approach "
            "angle, shield facing, when to commit).\n"
            "TARGETING: a shield facing is chosen by approach angle, never "
            "by targeting - there is no 'target shields', and the tactical "
            "model will turn 'hit one facing' into a hold-fire order. Name "
            "the facing to approach, and name a target only from: hull, "
            "sensors, engines, weapons. Default to hull: sensors and "
            "engines are small parts the guns need a clear line to, and "
            "a 'sensors first' plan (a logged duel) spent 61 polls in reach "
            "on them for 6 hull dealt. Name a subsystem only for a reason "
            "in the numbers - a weakness proven in the log below, or their "
            "weapons already failing.\n"
            "MANOEUVRES: give the tactical model one manoeuvre plan and "
            "say it is held. A shield facing going down is answered by "
            "guard (roll a fresh facing in), not a break; break or warp "
            "only for a HULL wound.\n"
            "Lessons from previous battles against this human:\n%(lessons)s\n"
            "%(hull_lessons)s\n"
            "%(weakness)s\n"
            "Sparring table (measured fleet-vs-fleet results):\n%(sparring)s\n"
            "Recent battles: %(recent)s\n\n"
            "Write the battle plan for the local tactical model, max 8 short "
            "lines, imperative voice. Cover: opening posture, preferred "
            "range band (inside %(reach)dm), primary target priority, when "
            "the cloak is authorised (mend / ambush / not at all), and the "
            "single biggest mistake to avoid against this opponent. Plain "
            "text, no headings." % ctx)
        return self._call(prompt)

    @staticmethod
    def _plan_defaults(ctx):
        ctx.setdefault("hull_lessons", "")
        ctx.setdefault("weakness", "")
        return ctx

    def debrief(self, ctx):
        """ctx: dict with result, facts, metrics, curve, lessons, recent.
        Returns {"analysis": str, "lesson": str} (either may be "")."""
        prompt = (
            "DEBRIEF A FINISHED BATTLE.\n"
            "Result: %(result)s\n"
            "Measured facts from the battle log: %(facts)s\n"
            "Metrics: %(metrics)s\n"
            "Hull curve (tick, ours, theirs): %(curve)s\n"
            "Existing lessons (do NOT rephrase these):\n%(lessons)s\n"
            "Recent battles: %(recent)s\n\n"
            "Respond ONLY with JSON: {\"analysis\": \"2-3 blunt sentences: "
            "why this battle went the way it did, citing the numbers\", "
            "\"lesson\": \"ONE genuinely new transferable lesson in at most "
            "30 words, or an "
            "empty string if this battle only confirms what is already "
            "known\"}" % ctx)
        raw = self._call(prompt, schema={
            "type": "object",
            "properties": {"analysis": {"type": "string"},
                           "lesson": {"type": "string"}},
            "required": ["analysis", "lesson"],
            "additionalProperties": False})
        if not raw:
            return {"analysis": "", "lesson": ""}
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            d = json.loads(raw[start:end])
            return {"analysis": str(d.get("analysis", ""))[:600],
                    "lesson": str(d.get("lesson", ""))[:220]}
        except (ValueError, KeyError):
            return {"analysis": raw[:600], "lesson": ""}
