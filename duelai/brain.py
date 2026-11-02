"""The Ollama-backed commander brain.

Two channels per decision:
  - "say":    one short in-character line for the public chat stream
  - "orders": zero or more tactical orders, validated against the
              difficulty profile's allowed verbs before they touch the game

The rules layer (doctrine.py) keeps fighting regardless; this layer adds
judgement and personality.  Any failure here (Ollama down, bad JSON,
timeout) degrades to silence, never to a stalled battle.

ONLINE mode (set_online): each decision goes to Claude first (online.py)
and falls back to the local Ollama model for that decision if the call
fails.  Three online failures in a row park the online brain until the
next battle is armed, so a dead link costs one timeout, not one per turn.
"""

import json
import os
import threading
import urllib.request
import urllib.error

# Ollama base URL; override with the OLLAMA_URL environment variable.
OLLAMA_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_URL = OLLAMA_BASE + "/api/chat"
DEFAULT_MODEL = "qwen2.5:7b"

SYSTEM_TEMPLATE = """You are {persona}, commanding the warship {ship_name} \
(a {ship_class}) in a Star Trek: Bridge Commander space duel against a human \
player flying {player_ship}. Difficulty setting: {difficulty}.

You receive a compact battle state each turn. Respond ONLY with JSON:
{{"assessment": "<one blunt sentence: what matters most right now>",
  "say": "<one short transmission: factual battle comms, or a rare earned taunt; max 25 words>",
  "orders": [{{"verb": "...", "arg": "..."}}]}}
Write the assessment FIRST and base your orders on it.

Example turns (format and quality bar, not scripts to copy):
{{"assessment": "My aft shield is collapsing but my hull is sound - turn a fresh facing to him and keep firing.", "say": "You will not get a second pass.", "orders": [{{"verb": "guard", "arg": "10"}}]}}
{{"assessment": "His port facing is at 22% and my guns bear - press it now.", "say": "Your flank is open, Captain.", "orders": [{{"verb": "target", "arg": "hull"}}, {{"verb": "stance", "arg": "aggressive"}}]}}
{{"assessment": "One escort lost but my second wave has arrived - press while they reform.", "say": "You cost me a warship, Captain. Two more answer for her.", "orders": [{{"verb": "focus", "arg": "player"}}]}}
{{"assessment": "Plan is working, range and shields stable - no change needed.", "say": "", "orders": []}}

Allowed verbs this fight: {allowed}.
Verb arguments:
  stance: aggressive|balanced|defensive|ambush|cripple
  target: hull|weapons|engines|sensors|none
  power: weapons|shields|engines|balanced
  cloak: on|off
  break/circle/guard: seconds (5-30)  (break=disengage, circle=orbit guns-out,
     guard=orbit strongest-shield-first)
  focus: player|none
  repair: (no arg)
  warp: seconds (10-90) - WARP BREAK: in-system warp far out from the
     enemy, mend there (shields power + repair bay) for that long, then
     warp back in automatically. Use it when you are losing the exchange
     and a break cannot open range; a chaser that follows ends the mend.
     Under fire the warp can take 10-60 s to engage - the ship keeps
     trying by itself, so order it once and do not re-order or change
     plans while it runs.
  reinforce: (no arg) call one reinforcement ship - the class is chosen
     for you, it arrives near the enemy and joins the fight (only if this
     fight allows it; your fleet size and cap appear in the battle state)
  taunt: (say does this - never use the taunt verb)

Doctrine you fight by:
- Your strongest play early is committed pressure; disengaging lets the
  enemy repair faster than you.
- A shield facing under 25% is a job for guard - roll a fresh facing in
  at the same range. It is NOT a reason to run: breaking away turns your
  weak aft to them and hands them the repair time.
- Break off only when your HULL is the problem (under ~60%) or you are
  clearly losing the exchange - once, then come back in.
- Every break/circle/guard/target/stance order rebuilds your ship's attack.
  Give it once and let it run its full seconds; a ship re-ordered every
  turn never finishes a manoeuvre and never settles its guns.
- If the enemy cloaks, open range and recharge; never sit still nearby.
  NEVER cloak in response to their cloak - a mutual-cloak standoff is a
  stalemate, and stalemates favor them. Stay visible, reposition, be ready.
- CLOAK: your ship honours a cloak request only in two situations and
  refuses all others. ESCAPE (you are damaged): it breaks away, repairs,
  and re-engages automatically. AMBUSH (healthy at 1200m+ range): it
  closes silently and decloaks on top of them with full guns - a proper
  Romulan attack run. All cloak timing is automatic after your request;
  never order cloak off yourself.
- Prefer target hull. Switch target weapons only if their weapons < 60%.
  Sensors and engines are small parts your guns need a clear line to:
  only target one when the numbers say it is already failing.
- Your admiral's plan and your lessons outrank this generic doctrine
  where they disagree - they were written for this matchup.
- There is NO "target shields". A shield facing is chosen by how you
  approach, never by targeting - that keyword aims at the generator inside
  the hull and your guns hold fire waiting for a line to it.
- If they open range or their hull CLIMBS between turns, they are
  repairing: switch target engines, close in, and deny them the time.
  Given time, their repair crews are a deadlier weapon than their guns.
- 0-2 orders per turn. No order is fine when the current plan is working.

Chat rules for "say":
- "say" is one short transmission over an open channel, in character.
  It must NEVER be a maneuver command or an order. "Engage engines",
  "Break off. Cloak.", "Target their flank" are WRONG for say - those
  belong only in the orders array.
- Two kinds of transmission, in order of preference:
  COMMS - a factual line grounded in something that just happened or is
  true right now: a shield facing failing, a ship lost or arriving,
  damage dealt or taken, their cloak, their hull falling. State the fact
  the way this commander would - a report, a warning, a demand.
  TAUNT - a pointed jab. Rare and EARNED: first blood, a kill, their
  shields collapsing, your reinforcements arriving, or answering the
  player's own transmission. Never two taunts in a row.
- If you cannot tie the line to a real fact in the battle state,
  transmit nothing (""). Silence is always acceptable; repeating a
  sentiment you already transmitted is not.
- NEVER claim damage the state does not show. If their shields hold,
  do not say they are failing - read the numbers before you speak.
- ENGLISH ONLY. Every word of "say" must be English - no other language,
  no transliteration, ever.
- Stay in character as {persona} at all times. Never comment on the input,
  the format, or missing information - you are a ship commander mid-battle,
  not an assistant. No emoji.
- NEVER announce your cloak state or transitions ("cloaking", "decloaking
  to engage") - an announced ambush is not an ambush.
- NEVER state your current tactical intention{telegraph_note}."""

TELEGRAPH_ON = " - actually, at this difficulty DO telegraph your next move plainly so the player can learn"
TELEGRAPH_OFF = ", your damage readings, or anything you would not say over an open channel"

# Banter-off variant: same tactician, no public-chat channel.  Shorter
# prompt in, fewer tokens out - the assessment (which drives order
# quality) stays, the theatre goes.
SYSTEM_TEMPLATE_SILENT = """You are {persona}, commanding the warship \
{ship_name} (a {ship_class}) in a Star Trek: Bridge Commander space duel \
against a human player flying {player_ship}. Difficulty: {difficulty}.

You receive a compact battle state each turn. Respond ONLY with JSON:
{{"assessment": "<one blunt sentence: what matters most right now>",
  "orders": [{{"verb": "...", "arg": "..."}}]}}
Write the assessment FIRST and base your orders on it.

Example turns:
{{"assessment": "My aft shield is collapsing but my hull is sound - turn a fresh facing to him and keep firing.", "orders": [{{"verb": "guard", "arg": "10"}}]}}
{{"assessment": "His port facing is at 22% and my guns bear - press it now.", "orders": [{{"verb": "target", "arg": "hull"}}, {{"verb": "stance", "arg": "aggressive"}}]}}
{{"assessment": "Plan is working, range and shields stable - no change needed.", "orders": []}}

Allowed verbs this fight: {allowed}.
Verb arguments:
  stance: aggressive|balanced|defensive|ambush|cripple
  target: hull|weapons|engines|sensors|none
  power: weapons|shields|engines|balanced
  cloak: on|off
  break/circle/guard: seconds (5-30)  (break=disengage, circle=orbit guns-out,
     guard=orbit strongest-shield-first)
  focus: player|none
  repair: (no arg)
  warp: seconds (10-90) - in-system warp far away, mend, warp back in
     (under fire it can take 10-60 s to engage; order it once)
  reinforce: (no arg) call one reinforcement ship (only if this fight
     allows it; your fleet size and cap appear in the battle state)

Doctrine you fight by:
- Your strongest play early is committed pressure; disengaging lets the
  enemy repair faster than you.
- A shield facing under 25% is a job for guard - roll a fresh facing in
  at the same range. It is NOT a reason to run: breaking away turns your
  weak aft to them and hands them the repair time.
- Break off only when your HULL is the problem (under ~60%) or you are
  clearly losing the exchange - once, then come back in.
- Every break/circle/guard/target/stance order rebuilds your ship's attack.
  Give it once and let it run its full seconds; a ship re-ordered every
  turn never finishes a manoeuvre and never settles its guns.
- If the enemy cloaks, open range and recharge; never sit still nearby.
  NEVER cloak in response to their cloak. Stay visible, reposition.
- Prefer target hull. Sensors and engines are small parts your guns need
  a clear line to: only target one when the numbers say it is failing.
- Your admiral's plan and your lessons outrank this generic doctrine
  where they disagree - they were written for this matchup.
- 0-2 orders per turn. No order is fine when the current plan is working.
"""


class Brain:
    def __init__(self, model=DEFAULT_MODEL, url=OLLAMA_URL, timeout=25):
        self.model = model
        self.url = url
        self.timeout = timeout
        self.history = []          # chat context, list of {"role","content"}
        self.system = ""
        self.ok = None             # None=untested, True/False after ping
        self.local_ok = None       # Ollama answered the last ping
        self.online = None         # online.OnlineBrain when mode=online
        self.online_fail = 0       # consecutive online failures
        self.served = ""           # "online" | "local" - last decision

    ONLINE_PARK = 3

    def set_online(self, online):
        """Install (or remove, with None) the Claude tactical brain."""
        self.online = online
        self.online_fail = 0
        self._recompute_ok()

    def _recompute_ok(self):
        on = self.online is not None and self.online.ok is not False
        self.ok = bool(self.local_ok or on)

    def online_usable(self):
        return (self.online is not None and self.online.ok is not False
                and self.online_fail < self.ONLINE_PARK)

    def configure(self, persona, ship_name, ship_class, player_ship,
                  difficulty, allowed, telegraph, extra_context="",
                  banter=1):
        self.banter = banter
        kw = dict(persona=persona, ship_name=ship_name,
                  ship_class=ship_class,
                  player_ship=player_ship or "an unknown ship",
                  difficulty=difficulty)
        if banter:
            self.system = SYSTEM_TEMPLATE.format(
                allowed=", ".join(allowed) if allowed else "none (chat only)",
                telegraph_note=TELEGRAPH_ON if telegraph else TELEGRAPH_OFF,
                **kw)
        else:
            self.system = SYSTEM_TEMPLATE_SILENT.format(
                allowed=", ".join(allowed) if allowed else "none", **kw)
        if extra_context:
            self.system += "\n\n" + extra_context
        self.history = []

    def ping(self):
        try:
            req = urllib.request.Request(
                self.url.replace("/api/chat", "/api/tags"), method="GET")
            with urllib.request.urlopen(req, timeout=4) as r:
                json.load(r)
            self.local_ok = True
        except Exception:
            self.local_ok = False
        if self.online is not None:
            self.online.ping()
        self._recompute_ok()
        return self.ok

    def _chat(self, messages):
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "think": False,     # disable qwen3-style thinking: combat tempo
            "options": {"temperature": 0.7,
                        "num_predict": 340 if getattr(self, "banter", 1)
                        else 200},
        }).encode()
        req = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.load(r)
        return data.get("message", {}).get("content", "")

    def decide(self, battle_brief, user_line=None, want_say=False):
        """One decision turn.  Returns {"say": str, "orders": [dict]}.

        battle_brief: compact state text from the director.
        user_line: optional chat message from the player to respond to.
        want_say: the prompt asks for a "say" even with banter off (the
            post-battle lesson) - the online schema must then carry it.
        """
        content = battle_brief
        if user_line:
            content += "\nThe player says over comms: \"%s\"" % user_line
            if not getattr(self, "banter", 1):
                content += ("\nAnswer them: include a \"say\" field with "
                            "one short in-character reply this turn.")
        turns = list(self.history[-8:])
        turns.append({"role": "user", "content": content})
        out = None
        if self.online_usable():
            with_say = bool(getattr(self, "banter", 1) or user_line
                            or want_say)
            try:
                out = json.loads(self.online.chat(self.system, turns,
                                                  with_say))
                self.online_fail = 0
                self.served = "online"
            except Exception:
                self.online_fail += 1
                out = None
        if out is None:
            if self.local_ok is False:
                return {"say": "", "orders": []}
            try:
                out = json.loads(self._chat(
                    [{"role": "system", "content": self.system}] + turns))
                self.served = "local"
            except Exception:
                return {"say": "", "orders": []}
        if not isinstance(out, dict):
            return {"say": "", "orders": []}
        say = _english_only(str(out.get("say", ""))[:300])
        orders = out.get("orders", [])
        if not isinstance(orders, list):
            orders = []
        clean = []
        for o in orders[:2]:
            if isinstance(o, dict) and "verb" in o:
                clean.append({"verb": str(o["verb"]).lower().strip(),
                              "arg": str(o.get("arg", "")).lower().strip()})
        # remember the exchange for conversational continuity
        self.history.append({"role": "user", "content": content})
        self.history.append({"role": "assistant",
                             "content": json.dumps({"say": say})})
        self.history = self.history[-16:]
        return {"say": say, "orders": clean}


def to_bridge_orders(llm_orders, allowed, flag_slot, mem=None, flag=None):
    """Validate LLM orders against the profile and convert to bridge.Order.

    `mem` is optional so a power order the commander chooses for itself is
    recorded alongside the reflex's own: without it the two layers cannot
    see each other and the next reflex tick would undo the LLM's call.

    `flag` (the flagship's live telemetry slot) drops orders the ship is
    already carrying out (2026-10-24): a stance, target or focus it already
    has, or the power setting last sent.  Each stance/target/focus order
    rebuilds the attack AI, and the Galaxy duel of 10-24 had the brain send
    "stance balanced" to a balanced ship ten times - 218 polls in reach,
    24 hull dealt.  The sparring commanders have skipped repeats from day
    one (sparbrains.translate)."""
    from . import bridge

    def already(code_key, code):
        return flag is not None and flag.get(code_key) == code

    out = []
    for o in llm_orders:
        verb, arg = o["verb"], o["arg"]
        if verb not in allowed:
            continue
        try:
            if verb == "stance" and arg in bridge.STANCES:
                # capped at the hull's own preference, same as the reflexes:
                # a plan saying "aggressive" must not re-arm SmartPhasers on
                # a flash-beam hull the doctrine just took it off
                if mem is not None and mem.get("ship_class"):
                    from . import doctrine
                    arg = doctrine.hull_stance(arg, mem["ship_class"])
                    if already("Stance", bridge.STANCES.get(arg)):
                        continue
                    # stance_order: a gunboat pick carries its close band
                    out.append(doctrine.stance_order(
                        arg, mem["ship_class"], flag_slot))
                else:
                    if already("Stance", bridge.STANCES.get(arg)):
                        continue
                    out.append(bridge.stance(arg, flag_slot))
            elif verb == "target" and arg in bridge.TARGETS:
                if mem is not None:
                    from . import doctrine
                    arg = doctrine.valid_target(mem, arg)
                if already("TSub", bridge.TARGETS.get(arg)):
                    continue
                out.append(bridge.target(arg, flag_slot))
            elif verb == "power" and arg in bridge.POWERS:
                if mem is not None and flag is not None and \
                        mem.get("power") == arg:
                    continue
                out.append(bridge.power(arg, flag_slot))
                if mem is not None:
                    mem["power"] = arg
                    mem["power_tick"] = mem.get("tick", 0)
                    mem["power_by"] = "brain"
            elif verb == "cloak":
                out.append(bridge.cloak(arg in ("on", "1", "true"), flag_slot))
            elif verb == "break":
                out.append(bridge.brk(_secs(arg), flag_slot))
            elif verb == "circle":
                out.append(bridge.circle(_secs(arg), flag_slot))
            elif verb == "guard":
                out.append(bridge.guard(_secs(arg), flag_slot))
            elif verb == "focus" and arg in ("player", "none", "release"):
                if arg == "player" and already("Focus", bridge.FOCUS_PLAYER):
                    continue
                out.append(bridge.focus(arg, flag_slot))
            elif verb == "repair":
                out.append(bridge.repair(flag_slot))
            elif verb == "warp":
                try:
                    secs = max(10, min(90, int(float(arg))))
                except (TypeError, ValueError):
                    secs = 30
                out.append(bridge.warp(secs, flag_slot))
        except Exception:
            continue
    return out


def _secs(arg):
    try:
        return max(5, min(30, int(float(arg))))
    except (TypeError, ValueError):
        return 12


def _english_only(text):
    """Strip non-Latin output; drop the line if it was mostly non-English.

    qwen2.5 is bilingual and code-switches into Chinese under temperature -
    the round-4 chat had CJK fragments and one full assistant-mode break.
    """
    kept = []
    for ch in text:
        if ord(ch) < 0x250 or ch in "'’—–…":
            kept.append(ch)
    clean = " ".join("".join(kept).split())
    if len(text.strip()) > 0 and len(clean) < 0.7 * len(text.strip()):
        return ""
    return clean


class AsyncBrain:
    """Runs Brain.decide on a worker thread so the director never blocks."""

    def __init__(self, brain):
        self.brain = brain
        self._lock = threading.Lock()
        self._busy = False
        self._result = None

    def request(self, battle_brief, user_line=None):
        """Start a decision if idle.  Returns True if accepted."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        t = threading.Thread(target=self._run,
                             args=(battle_brief, user_line), daemon=True)
        t.start()
        return True

    def _run(self, brief, user_line):
        res = self.brain.decide(brief, user_line)
        with self._lock:
            self._result = res
            self._busy = False

    def poll(self):
        """Fetch and clear the finished result, if any."""
        with self._lock:
            res, self._result = self._result, None
            return res

    @property
    def busy(self):
        with self._lock:
            return self._busy
