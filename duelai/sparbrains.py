"""Brains sparring: Claude commands one side, the local model the other.

    spar federation vs klingon 2v2 x4 brains=swap

Every ship on both sides still flies under the game's own compound AI.
On top of it each side gets a SQUADRON COMMANDER - one LLM decision per
side every CADENCE seconds - issuing the same verbs the duel brain has
(stance, target, power, break/circle/guard, repair, cloak) plus fleet
focus fire ("A2 focus E1").  One commander is Claude (the online model and
effort from settings), the other is the local Ollama model.

Fair by construction:
  - both commanders get the SAME system prompt, the SAME per-ship brief
    (own ships as A1.., enemies as E1.. - no red/blue, no names hinting
    who is who), the SAME verbs, the SAME cadence and order caps;
  - a failed call is a lost turn, never a hand-off to the other brain;
  - the hulls are NOT redrafted between rounds, and brains=swap moves
    Claude to the other side every round, so over an even number of
    rounds each brain flies each draft once;
  - a round with player hits, a dead link, or a commander that failed more
    than a third of its calls is VOID - logged, never scored.

Results: data/brains.json (by matchup key and by model pairing).

Learning (2026-10-24): after every round each commander writes ONE lesson
from its own round with its own model, and the next round - this set or a
later set of the same matchup - starts with that brain's own notes in its
system prompt.  Same mechanism for both brains: what is compared is how
well each one learns, not who gets more help.  Refused orders come back
to the commander in the next brief, so it stops re-sending them blind.
"""

import json
import re
import os
import threading
import time
import urllib.request

from . import bridge, doctrine, intel, online

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "brains.json")

# Ollama base URL; override with the OLLAMA_URL environment variable.
OLLAMA_URL = (os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
              + "/api/chat")
CADENCE = 8.0          # seconds between one side's decisions
MAX_ORDERS = 6         # LLM orders read per turn
HISTORY = 3            # previous exchanges each commander remembers
VOID_FAIL_RATIO = 0.34
POWER_DWELL = 24       # ticks a power setting holds before it may change
                       # (~3 decisions).  In testing: Claude flipped weapons
                       # <-> shields every turn and lost both rounds it did
                       # it most ("shields-heavy power is blunting our own
                       # guns").  Same idea as doctrine.POWER_DWELL in duels.
MEMORY_KEEP = 8        # notes kept per brain per matchup
LOCAL_KEEP_ALIVE = "10m"   # keep the local model in VRAM between rounds
LOCAL_TIMEOUT = 30

STANCE_ARGS = ("aggressive", "balanced", "defensive", "ambush", "cripple",
               "gunboat")
TARGET_ARGS = ("hull", "weapons", "engines", "sensors")
CLOAK_WORDS = {bridge.CLOAK_NO_DEVICE: "none", bridge.CLOAK_READY: "ready",
               bridge.CLOAK_CLOAKING: "cloaking", bridge.CLOAK_CLOAKED: "DARK",
               bridge.CLOAK_DECLOAKING: "decloaking"}
_STANCE_NAMES = {v: k for k, v in bridge.STANCES.items()}

SYSTEM = """You command one squadron in a Star Trek: Bridge Commander \
fleet battle against another commander's squadron. Every ship on both sides \
is flown by the game's own combat AI; you give squadron orders on top of it. \
You win by destroying the enemy squadron, or by having more average hull \
left when the round's time runs out ({limit} ticks).

Each turn you receive the battle state. Respond ONLY with JSON:
{{"assessment": "<one blunt sentence: what matters most right now>",
 "orders": [{{"ship": "A1", "verb": "...", "arg": "..."}}]}}
Write the assessment first and base the orders on it. 0-{max_orders} orders \
per turn; no orders is right when the current plan is working. "ship" is one \
of your ships (A1, A2, ...) or "all".

Verbs:
  focus E<n> | nearest - concentrate that ship's fire on enemy E<n>, or hand
      it back to the nearest enemy. A cloaked enemy cannot be focused.
  stance aggressive|balanced|defensive|ambush|cripple|gunboat - the combat
      AI's posture. Each change rebuilds the ship's AI: change it for a
      reason, not every turn. gunboat only for hulls marked gunboat below.
  target hull|weapons|engines|sensors - which part of its current target
      to shoot. There is NO "target shields": a shield facing is worn down
      by the angle of attack, and aiming at the generator wastes the guns.
  power weapons|shields|engines|balanced - the power grid trade. A
      setting HOLDS for at least {dwell} ticks; changing it sooner is
      refused, so pick one you can live with for a while.
  break <5-30> - disengage for that many seconds (open range, recharge).
  circle <5-30> - orbit the target guns-out for that many seconds.
  guard <5-30> - orbit keeping the strongest shield toward the target.
  repair - damage-control crews to the worst system.
  warp <10-90> - WARP BREAK: in-system warp far out from its target, mend
      there (shields power + repair) for that many seconds, then warp back
      in by itself. A break at impulse rarely opens range against a chaser;
      this does. Needs hull above 25% and working warp + impulse engines.
      While it runs that ship takes no stance/target/focus orders. Under
      fire the warp can take 10-60 s to engage: the ship keeps trying by
      itself, so order it once and let it run.
  cloak on|off - only hulls with a cloak device (marked below). Shields
      are DOWN while dark and recharge unseen; the ship cannot fire; it
      stays dark at least 8 ticks before it may decloak.

Only ONE of stance / target / focus / warp per ship per turn - each rebuilds that
ship's AI, and extra ones are refused. Orders take a few seconds to show in
the readings. The brief starts with what happened to your last orders.

Readings: shields are F(ore) A(ft) P(ort) S(tarboard) D(orsal) V(entral) in
percent. "firing at E2 140m closing 5" = range to its current target in
metres, positive closing = range falling.

Hulls in this battle:
{intel}"""


def _hull_line(cid):
    db = intel._db().get(cid, {})
    label = db.get("label", cid)
    prof = (intel.weapon_profile_text(cid) or "").strip().splitlines()
    gun = prof[0].lstrip("- ").strip() if prof else "unknown guns"
    tags = []
    try:
        if intel.gunboat_profile(cid):
            tags.append("gunboat")
    except Exception:
        pass
    if any(x.get("kind") == "cloak" for x in db.get("subsystems", [])):
        tags.append("cloak device")
    return "- %s: %d hull. %s%s" % (label, int(db.get("hull", 0) or 0), gun,
                                    (" [" + ", ".join(tags) + "]")
                                    if tags else "")


def system_prompt(own_classes, enemy_classes, notes=None):
    lines = ["Yours:"] + [_hull_line(c) for c in _uniq(own_classes)]
    lines += ["Enemy:"] + [_hull_line(c) for c in _uniq(enemy_classes)]
    from . import sparring
    text = SYSTEM.format(limit=sparring.ROUND_TICK_LIMIT, dwell=POWER_DWELL,
                         max_orders=MAX_ORDERS, intel="\n".join(lines))
    from . import weakness
    weak = [weakness.text_for(c) for c in _uniq(enemy_classes)]
    weak += [weakness.text_for(c, own=True) for c in _uniq(own_classes)]
    weak = [w for w in weak if w]
    if weak:
        text += "\n\n" + "\n".join(weak)
    if notes:
        text += ("\n\nYOUR OWN NOTES FROM EARLIER ROUNDS OF THIS MATCHUP "
                 "(oldest first; you wrote the lessons):\n"
                 + "\n".join("- " + n for n in notes))
    own_u, en_u = _uniq(own_classes), _uniq(enemy_classes)
    ev = evidence_text(own_u[0] if own_u else "",
                       en_u[0] if len(en_u) == 1 else None)
    if ev:
        text += ("\n\nTHE RECORD (counted from the logs, not written by a "
                 "brain - where a note above disagrees, the record wins):\n"
                 + ev)
    return text


def _uniq(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def decision_schema():
    return {
        "type": "object",
        "properties": {
            "assessment": {"type": "string"},
            "orders": {"type": "array", "items": {
                "type": "object",
                "properties": {"ship": {"type": "string"},
                               "verb": {"type": "string"},
                               "arg": {"type": "string"}},
                "required": ["ship", "verb", "arg"],
                "additionalProperties": False}},
        },
        "required": ["assessment", "orders"],
        "additionalProperties": False,
    }


# ------------------------------------------------------------ ids + brief

class SideView:
    """Stable A<n>/E<n> ids for one side's commander, plus what it has
    ordered (so repeats that would only rebuild an AI are dropped)."""

    def __init__(self, side_num):
        self.side = side_num
        self.own = {}           # slot -> "A1"
        self.enemy = {}         # slot -> "E1"
        self.focus_of = {}      # own slot -> "nearest" | enemy slot
        self.power_of = {}      # own slot -> power word last sent
        self.power_tick = {}    # own slot -> tick that setting was sent
        self.dark_since = {}    # own slot -> tick it went fully dark
        self.cloak_prev = {}

    def update(self, snap):
        for slot in sorted(snap.fleet):
            s = snap.fleet[slot]
            mine = s.get("Side", 0) == self.side
            book = self.own if mine else self.enemy
            if slot not in book and s.get("st") != bridge.SLOT_ALIVE:
                continue        # a wreck from an earlier round, not ours
            if slot not in book:
                book[slot] = "%s%d" % ("A" if mine else "E", len(book) + 1)
            if mine:
                c = s.get("Cloak", 0)
                if c == bridge.CLOAK_CLOAKED and \
                        self.cloak_prev.get(slot) != bridge.CLOAK_CLOAKED:
                    self.dark_since[slot] = snap.tick
                self.cloak_prev[slot] = c

    def slot_of(self, ident):
        ident = (ident or "").strip().upper()
        for book in (self.own, self.enemy):
            for slot, i in book.items():
                if i == ident:
                    return slot
        return None


def _sh(s):
    return "F%d A%d P%d S%d D%d V%d" % tuple(
        s.get(k, -1) for k in ("ShF", "ShA", "ShL", "ShR", "ShT", "ShB"))


def feedback_line(ok, bad):
    """What happened to a commander's last orders, for its next brief."""
    if not ok and not bad:
        return ""
    return "LAST ORDERS: applied %s; refused %s." % (
        ", ".join(ok) or "none", "; ".join(bad) or "none")


def brief(snap, view, round_tick, feedback=""):
    view.update(snap)
    live = snap.live_fleet()

    def label(slot):
        cid = snap.fleet.get(slot, {}).get("Type", "")
        return intel._db().get(cid, {}).get("label", cid or "?")

    def tgt_of(s, enemy_book):
        j = bridge.spar_target(s)
        if j is None or j not in enemy_book:
            return "no target"
        rng = bridge.slot_range(s)
        if rng >= bridge.NO_RANGE:
            return "targeting %s" % enemy_book[j]
        return "firing at %s %dm closing %d" % (enemy_book[j], rng,
                                                bridge.slot_closing(s))

    def power_of(slot):
        pw = view.power_of.get(slot)
        if not pw:
            return ""
        left = POWER_DWELL - (snap.tick - view.power_tick.get(slot, -99999))
        return ", power %s%s" % (pw, (" (held %d more ticks)" % left)
                                 if left > 0 else "")

    own_live = [k for k in view.own if k in live]
    en_live = [k for k in view.enemy if k in live]
    out = ([feedback] if feedback else []) + [
           "Tick %d of the round." % round_tick,
           "YOUR SQUADRON (%d of %d alive):" % (len(own_live), len(view.own))]
    for slot, ident in sorted(view.own.items(), key=lambda x: x[1]):
        if slot not in live:
            out.append("%s %s: destroyed." % (ident, label(slot)))
            continue
        s = live[slot]
        out.append(
            "%s %s: hull %d%%, shields %s, battery %d%%, weapons %d%%, "
            "engines %d%%, cloak %s, stance %s%s, %s%s." % (
                ident, label(slot), s.get("Hull", -1), _sh(s),
                s.get("Batt", -1), s.get("Weap", -1), s.get("Eng", -1),
                CLOAK_WORDS.get(s.get("Cloak", 0), "?"),
                _STANCE_NAMES.get(s.get("Stance", 0), "?"), power_of(slot),
                tgt_of(s, view.enemy),
                ("" if s.get("Man") != bridge.CMD_WARP else
                 ", ON WARP BREAK (%s)" % ("mending far out" if s.get("WB") == 2
                                           else "warping out"))))
    out.append("ENEMY SQUADRON (%d of %d alive):"
               % (len(en_live), len(view.enemy)))
    for slot, ident in sorted(view.enemy.items(), key=lambda x: x[1]):
        if slot not in live:
            out.append("%s %s: destroyed." % (ident, label(slot)))
            continue
        s = live[slot]
        if s.get("Cloak", 0) in (bridge.CLOAK_CLOAKED, bridge.CLOAK_CLOAKING):
            out.append("%s %s: CLOAKED - no sensor data." % (ident,
                                                             label(slot)))
            continue
        out.append("%s %s: hull %d%%, shields %s, %s." % (
            ident, label(slot), s.get("Hull", -1), _sh(s),
            tgt_of(s, view.own)))
    return "\n".join(out)


# ------------------------------------------------------------ orders

def _kinds(cid):
    """Subsystem kinds a target hull really has (same rule as the duel)."""
    ship = intel._db().get(cid, {})
    if not ship:
        return None
    m = {"impulse": "engines", "sensors": "sensors"}
    have = {m[x["kind"]] for x in ship.get("subsystems", [])
            if x.get("kind") in m}
    if ship.get("phasers") or ship.get("pulses"):
        have.add("weapons")
    have.add("hull")
    return have


def _secs90(arg):
    try:
        return int(float(arg))
    except (TypeError, ValueError):
        return 30


def _secs(arg):
    try:
        return max(5, min(30, int(float(arg))))
    except (TypeError, ValueError):
        return 12


def translate(llm_orders, snap, view):
    """LLM orders -> (bridge orders, applied words, rejected words).

    Safety, all from the 09-10 crash work: no AI-rebuilding order (stance /
    target / focus) for a ship in ANY cloak state but ready/none - a rebuild
    drops a settled cloak; at most one rebuild per ship per turn; a stance
    or focus the ship already has is not re-sent; decloak goes through
    doctrine._uncloak's spar sequence (stance first while settled, then
    break 0 + cloak 0) and only after CLOAK_MIN_HOLD ticks dark."""
    live = snap.live_fleet()
    out, ok, bad = [], [], []
    rebuilt, cloaked_now = set(), set()
    for o in (llm_orders or [])[:MAX_ORDERS]:
        verb = str(o.get("verb", "")).lower().strip()
        arg = str(o.get("arg", "")).lower().strip()
        who = str(o.get("ship", "")).strip().upper()
        slots = ([k for k in view.own if k in live] if who == "ALL"
                 else [view.slot_of(who)])
        for slot in slots:
            word = "%s %s %s" % (view.own.get(slot, who), verb, arg)
            s = live.get(slot) if slot in view.own else None
            if s is None:
                bad.append(word + (" (destroyed)" if slot in view.own
                                   else " (no such ship)"))
                continue
            cid = s.get("Type", "")
            cl = s.get("Cloak", 0)
            settled = cl in (bridge.CLOAK_NO_DEVICE, bridge.CLOAK_READY)
            warping = s.get("Man") == bridge.CMD_WARP
            if warping and verb not in ("power", "repair"):
                bad.append(word + " (warp break running)")
                continue
            if verb in ("stance", "target", "focus", "warp"):
                if not settled or slot in cloaked_now:
                    bad.append(word + " (cloak busy)")
                    continue
                if slot in rebuilt:
                    bad.append(word + " (one AI change per ship per turn)")
                    continue
            if verb == "stance":
                if arg not in STANCE_ARGS:
                    bad.append(word); continue
                if arg == "gunboat" and not intel.gunboat_profile(cid):
                    arg = "balanced"
                if bridge.STANCES[arg] == s.get("Stance"):
                    continue                    # already there: no rebuild
                out.append(doctrine.stance_order(arg, cid, slot))
                rebuilt.add(slot)
            elif verb == "target":
                if arg not in TARGET_ARGS:
                    bad.append(word); continue
                j = bridge.spar_target(s)
                have = _kinds(snap.fleet.get(j, {}).get("Type", "")) \
                    if j is not None else None
                if have is not None and arg not in have:
                    arg = "hull"
                if bridge.TARGETS[arg] == s.get("TSub"):
                    continue
                out.append(bridge.target(arg, slot))
                rebuilt.add(slot)
            elif verb == "focus":
                if arg in ("nearest", "none", "release", ""):
                    if view.focus_of.get(slot, "nearest") == "nearest":
                        continue
                    out.append(bridge.focus("spar", slot))
                    view.focus_of[slot] = "nearest"
                else:
                    j = view.slot_of(arg)
                    e = live.get(j) if j in view.enemy else None
                    if e is None:
                        bad.append(word + (" (destroyed)" if j in view.enemy
                                           else " (no such enemy)"))
                        continue
                    if e.get("Cloak", 0) in (bridge.CLOAK_CLOAKING,
                                             bridge.CLOAK_CLOAKED):
                        bad.append(word + " (enemy cloaked)"); continue
                    if view.focus_of.get(slot) == j:
                        continue
                    out.append(bridge.Order(bridge.CMD_FOCUS,
                                            bridge.SPAR_TGT_BASE + j, slot))
                    view.focus_of[slot] = j
                rebuilt.add(slot)
            elif verb == "power":
                if arg not in bridge.POWERS:
                    bad.append(word); continue
                if view.power_of.get(slot) == arg:
                    continue
                held = snap.tick - view.power_tick.get(slot, -99999)
                if slot in view.power_of and held < POWER_DWELL:
                    bad.append(word + " (power held %d more ticks)"
                               % (POWER_DWELL - held))
                    continue
                out.append(bridge.power(arg, slot))
                view.power_of[slot] = arg
                view.power_tick[slot] = snap.tick
            elif verb in ("break", "circle", "guard"):
                fn = {"break": bridge.brk, "circle": bridge.circle,
                      "guard": bridge.guard}[verb]
                out.append(fn(_secs(arg), slot))
            elif verb == "repair":
                out.append(bridge.repair(slot))
            elif verb == "warp":
                if s.get("Hull", 100) <= 25:
                    bad.append(word + " (hull too low to warp)"); continue
                out.append(bridge.warp(max(10, min(90, _secs90(arg))), slot))
                rebuilt.add(slot)
            elif verb == "cloak":
                if arg in ("on", "1", "true"):
                    if cl != bridge.CLOAK_READY or slot in rebuilt:
                        bad.append(word + " (cloak not ready)"); continue
                    out.append(bridge.cloak(1, slot))
                    cloaked_now.add(slot)
                elif arg in ("off", "0", "false"):
                    dark = view.dark_since.get(slot)
                    if cl != bridge.CLOAK_CLOAKED or dark is None:
                        bad.append(word + " (not dark)"); continue
                    if snap.tick - dark < doctrine.CLOAK_MIN_HOLD:
                        bad.append(word + " (minimum hold)"); continue
                    mem = {"ship_class": cid,
                           "restore_stance": _STANCE_NAMES.get(
                               s.get("Stance", 0), "")}
                    doctrine._uncloak(out, mem, doctrine.SPAR_CLOAK_PROFILE,
                                      slot, spar=True)
                    cloaked_now.add(slot)
                else:
                    bad.append(word); continue
            else:
                bad.append(word); continue
            ok.append("%s %s%s" % (view.own[slot], verb,
                                   (" " + arg) if arg else ""))
    return out, ok, bad


# ------------------------------------------------------------ commanders

def local_chat(model, messages, timeout=LOCAL_TIMEOUT, url=OLLAMA_URL):
    body = json.dumps({
        "model": model, "messages": messages, "stream": False,
        "format": "json", "think": False, "keep_alive": LOCAL_KEEP_ALIVE,
        "options": {"temperature": 0.7, "num_predict": 500},
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data.get("message", {}).get("content", "")


def warm_local(model, url=OLLAMA_URL):
    """Load the local model into VRAM before round 1.  The first set ran
    round 1 VOID: the first calls timed out while qwen3:8b loaded.
    Returns seconds taken, or -1 on failure."""
    t0 = time.time()
    body = json.dumps({"model": model, "messages": [
        {"role": "user", "content": "ready"}], "stream": False,
        "think": False, "keep_alive": LOCAL_KEEP_ALIVE,
        "options": {"num_predict": 1}}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            json.load(r)
        return time.time() - t0
    except Exception:
        return -1


def local_ok(url=OLLAMA_URL):
    try:
        req = urllib.request.Request(url.replace("/api/chat", "/api/tags"))
        with urllib.request.urlopen(req, timeout=4) as r:
            json.load(r)
        return True
    except Exception:
        return False


class SideCommander:
    """One side's LLM commander. engine: "claude" | "local"."""

    def __init__(self, engine, side_num, model, api_key="", effort="low",
                 post=None, chat=None, url=OLLAMA_URL):
        self.engine = engine
        self.side = side_num
        self.model = model
        self.api_key = api_key
        self.effort = effort if effort in online.EFFORTS else "low"
        self._post = post or online.post_messages
        self._chat = chat or local_chat
        self.url = url
        self.view = SideView(side_num)
        self.system = ""
        self.history = []
        self.last_request = 0.0
        self.last_error = ""
        self.decisions = []     # (tick, assessment, applied, rejected)
        self.feedback = ""      # what happened to the last orders
        self.stats = {"calls": 0, "fails": 0, "secs": 0.0, "cost": 0.0,
                      "orders": 0, "rejected": 0}
        self._lock = threading.Lock()
        self._busy = False
        self._result = None

    @property
    def name(self):
        return "claude" if self.engine == "claude" else "local"

    # -- one decision (worker thread) --
    def decide(self, text):
        msgs = list(self.history[-2 * HISTORY:])
        msgs.append({"role": "user", "content": text})
        t0 = time.time()
        try:
            if self.engine == "claude":
                body = {
                    "model": self.model, "max_tokens": 4000,
                    "system": [{"type": "text", "text": self.system,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": msgs,
                    "output_config": {"effort": self.effort, "format": {
                        "type": "json_schema", "schema": decision_schema()}},
                }
                th = online.thinking_for(self.model, self.effort)
                if th:
                    body["thinking"] = th
                data = self._post(self.api_key, body, 20)
                self.stats["cost"] += online.usage_cost(
                    data.get("model") or self.model, data.get("usage") or {})
                if data.get("stop_reason") == "max_tokens":
                    raise online.ApiError("max_tokens")
                raw = online.response_text(data)
            else:
                raw = self._chat(self.model, [{"role": "system",
                                               "content": self.system}] + msgs,
                                 url=self.url)
            out = json.loads(raw)
            if not isinstance(out, dict):
                raise ValueError("not an object")
            orders = out.get("orders", [])
            if not isinstance(orders, list):
                orders = []
            orders = [o for o in orders if isinstance(o, dict)]
            res = {"assessment": str(out.get("assessment", ""))[:240],
                   "orders": orders[:MAX_ORDERS]}
        except Exception as e:
            self.stats["fails"] += 1
            self.last_error = "%s: %s" % (type(e).__name__, str(e)[:100])
            return None
        self.stats["calls"] += 1
        self.stats["secs"] += time.time() - t0
        self.last_error = ""
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant",
                             "content": json.dumps(res)})
        self.history = self.history[-2 * HISTORY:]
        return res

    # -- after the round: one lesson, in this brain's own words --
    def reflect(self, summary):
        """Returns one lesson (<= 40 words) or "" on any failure."""
        sysmsg = ("You commanded a squadron in a Star Trek: Bridge Commander "
                  "fleet battle and it is over. Write ONE lesson for the next "
                  "time you fight this matchup: concrete, about your own "
                  "orders - what to repeat or what to do differently. Read "
                  "THE RECORD lines first: if your past lessons have not "
                  "produced a win, do not write them again, and do not blame "
                  "a loss on standing and fighting when the record says the "
                  "busier commander loses. At most 30 words. Respond ONLY "
                  "with JSON: {\"lesson\": \"...\"}")
        try:
            if self.engine == "claude":
                data = self._post(self.api_key, {
                    "model": self.model, "max_tokens": 2000,
                    "system": sysmsg,
                    "messages": [{"role": "user", "content": summary}],
                    "output_config": {"effort": self.effort, "format": {
                        "type": "json_schema", "schema": {
                            "type": "object",
                            "properties": {"lesson": {"type": "string"}},
                            "required": ["lesson"],
                            "additionalProperties": False}}},
                }, 30)
                self.stats["cost"] += online.usage_cost(
                    data.get("model") or self.model, data.get("usage") or {})
                raw = online.response_text(data)
            else:
                raw = self._chat(self.model, [
                    {"role": "system", "content": sysmsg},
                    {"role": "user", "content": summary}], url=self.url)
            lesson = str(json.loads(raw).get("lesson", "")).strip()
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, str(e)[:100])
            return ""
        return " ".join(lesson.split()[:45])[:300]

    def round_summary(self, side_key, own, enemy, result, why, ticks,
                      own_hull, enemy_hull, enemy_counts=None, record=""):
        lines = ["You commanded %s flying %s against %s." % (
                     side_key.upper(), " + ".join(own), " + ".join(enemy)),
                 "Result: you %s (%s) after %d ticks; final average hull "
                 "yours %s%%, theirs %s%%." % (result, why, ticks, own_hull,
                                               enemy_hull)]
        mine = manoeuvre_counts(self.decisions)
        if enemy_counts:
            lines.append("Orders applied: you %d (%d manoeuvres), the enemy "
                         "commander %d (%d manoeuvres)." % (
                             mine[0], mine[1], enemy_counts[0],
                             enemy_counts[1]))
        if record:
            lines.append("THE RECORD: " + record)
        lines.append("Your decisions (tick: assessment -> applied | "
                     "refused):")
        decs, prev = [], None
        for d in self.decisions:        # drop exact repeats of the turn before
            sig = (d["assessment"], tuple(d["orders"]), tuple(d["rejected"]))
            if sig != prev:
                decs.append(d)
            prev = sig
        if len(decs) > 14:
            decs = decs[:4] + decs[-10:]
        for d in decs:
            lines.append("t%d: %s -> %s%s" % (
                d["tick"], d["assessment"][:160],
                ", ".join(d["orders"]) or "no orders",
                (" | refused " + "; ".join(d["rejected"]))
                if d["rejected"] else ""))
        return "\n".join(lines)

    # -- async wrapper, like brain.AsyncBrain --
    def request(self, text):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self.last_request = time.time()

        def run():
            res = self.decide(text)
            with self._lock:
                self._result = res
                self._busy = False
        threading.Thread(target=run, daemon=True).start()
        return True

    def poll(self):
        with self._lock:
            res, self._result = self._result, None
            return res

    @property
    def busy(self):
        with self._lock:
            return self._busy

    def due(self, now=None):
        return ((now or time.time()) - self.last_request) >= CADENCE \
            and not self.busy

    def fail_ratio(self):
        n = self.stats["calls"] + self.stats["fails"]
        return (self.stats["fails"] / float(n)) if n else 0.0

    def summary(self):
        st = self.stats
        avg = st["secs"] / st["calls"] if st["calls"] else 0
        return "%s (%s): %d decisions, %d failed, avg %.1fs, %d orders%s" % (
            self.name, self.model, st["calls"], st["fails"], avg,
            st["orders"], (", ~$%.3f" % st["cost"]) if st["cost"] else "")


# ------------------------------------------------------------ results

def _load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}
    except ValueError:
        # Unreadable: keep a copy before any save can replace it with {}.
        keep = DATA_PATH + ".corrupt-%d" % int(time.time() // 3600)
        if not os.path.exists(keep):
            try:
                import shutil
                shutil.copyfile(DATA_PATH, keep)
            except OSError:
                pass
        return {}


def table():
    return _load()


def memory_key(scen):
    """One memory per matchup, whichever side each race drew: a swap set
    flies both directions of the same question."""
    races = sorted([str(scen.get("red_race", "?")),
                    str(scen.get("blue_race", "?"))])
    size = scen.get("size", 1)
    era = (scen.get("key", "tng").split("-", 1)[0]) or "tng"
    return "%s-%dv%d %s / %s%s" % (era, size, size, races[0], races[1],
                                   "" if scen.get("gods", 1) else " nogods")


def memory_for(scen, model):
    return list(_load().get("memory", {}).get(memory_key(scen), {})
                .get(model, []))[-MEMORY_KEEP:]


_mem_lock = threading.Lock()


def add_memory(scen, model, note):
    with _mem_lock:
        d = _load()
        book = d.setdefault("memory", {}).setdefault(memory_key(scen), {})
        book[model] = (book.get(model, []) + [note])[-MEMORY_KEEP:]
        _save(d)


def _save(d):
    # Write-then-rename (10-30): brains.json was found spliced - two saves
    # at 17:42:37 interleaved over the share, every read after that came
    # back {} and a logged duel flew with no lessons.  A per-writer temp name so two
    # writers never share one either.
    tmp = "%s.%d.%d.tmp" % (DATA_PATH, os.getpid(), id(d))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, DATA_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ------------------------------------------------------------ hull lessons
# The same lessons, indexed by hull so DUELS can use them (2026-10-24): the
# duel brain and the strategist get the lessons written by the commander who
# flew the duel's AI hull, preferring ones fought against the player's hull.
# Only Claude-written lessons go to duels by default - the local model's
# notes have so far been generic ("repeat focus fire at critical moments").
#
# Evidence first (2026-10-30): Claude lost all 12 rounds it flew the Fek'lhr
# against the Defiant, and every loss wrote "keep range, kite, warp out" -
# while the local model, which barely ordered anything, won with it.  Over
# all 39 brain rounds the commander with fewer manoeuvre orders won 29, the
# busier one 5.  So a lesson written after a win now outranks one written
# after a loss, a local lesson from a WIN may reach a duel, a lesson can be
# retired ("retired": reason), and the paragraph opens with the record.

HULL_LESSON_KEEP = 300
DUEL_ENGINES = ("claude",)
WIN_ENGINES = ("claude", "local")   # a winning lesson from either brain
EVIDENCE_MIN = 4                    # brain rounds before a pair has a record

_MANOEUVRE = re.compile(r"\b(break [1-9][0-9]*|circle|guard|warp)\b")


def manoeuvre_counts(decisions):
    """(orders applied, manoeuvre orders among them) for one commander's
    decision list.  break 0 is a stop, not a manoeuvre."""
    orders = [o for d in decisions or [] for o in d.get("orders", [])]
    return len(orders), sum(1 for o in orders if _MANOEUVRE.search(o))


def evidence_for(own_cid, enemy_cid=None):
    """The brain-round record for own_cid (vs enemy_cid when that pair has
    EVIDENCE_MIN rounds, else every matchup).  Only rounds banked with ids
    and manoeuvre counts count.  Returns a dict or None."""
    if not own_cid:
        return None
    own_l, en_l = own_cid.lower(), (enemy_cid or "").lower()

    def sides(r):
        ids = r.get("ids") or {}
        return ([c.lower() for c in ids.get("red", [])],
                [c.lower() for c in ids.get("blue", [])])

    rows = [r for r in _load().get("rounds", [])
            if not r.get("void") and r.get("man") and r.get("ids")
            and r.get("winner_brain") in ("claude", "local")]
    pair = []
    for r in rows:
        red, blue = sides(r)
        for mine, theirs, side in ((red, blue, "red"), (blue, red, "blue")):
            if own_l in mine and (not en_l or en_l in theirs):
                pair.append((r, side))
                break
    scope = "vs"
    if len(pair) < EVIDENCE_MIN:
        # the all-matchups record, but only for a hull that has sparred
        if not any(own_l in sides(r)[0] + sides(r)[1] for r in rows):
            return None
        scope, pair = "all", [(r, None) for r in rows]
    if len(pair) < EVIDENCE_MIN:
        return None
    ev = {"scope": scope, "rounds": len(pair), "calm_won": 0, "busy_won": 0,
          "flown": {"claude": [0, 0], "local": [0, 0]}}
    for r, side in pair:
        w = r["winner_brain"]
        l = "local" if w == "claude" else "claude"
        wm, lm = r["man"].get(w, 0), r["man"].get(l, 0)
        if wm < lm:
            ev["calm_won"] += 1
        elif wm > lm:
            ev["busy_won"] += 1
        if side:
            flyer = r["claude_side"] == side and "claude" or "local"
            ev["flown"][flyer][0 if w == flyer else 1] += 1
    return ev


def evidence_text(own_cid, enemy_cid=None):
    """One or two lines of record to sit above the lessons, or ""."""
    ev = evidence_for(own_cid, enemy_cid)
    if not ev:
        return ""
    if ev["scope"] == "vs":
        where = "%s vs %s" % (_label(own_cid), _label(enemy_cid))
    else:
        where = "every matchup"
    out = ["Sparring record, %s (%d brain-vs-brain rounds): the commander "
           "who ordered FEWER manoeuvres (break, circle, guard, warp) won "
           "%d, the busier one won %d." % (where, ev["rounds"],
                                           ev["calm_won"], ev["busy_won"])]
    if ev["scope"] == "vs":
        parts = []
        for eng, name in (("claude", "Claude"), ("local", "the local model")):
            w, l = ev["flown"][eng]
            if w + l:
                parts.append("%s flying your hull won %d, lost %d" % (
                    name, w, l))
        if parts:
            out.append("; ".join(parts) + ".")
    if ev["calm_won"] > 2 * max(1, ev["busy_won"]):
        out.append("Every break, circle or warp turns your guns off target "
                   "while his stay on you. Set power and target, keep the "
                   "bow on him and let the ship's AI fight; manoeuvre only "
                   "for a reason the record supports.")
    return " ".join(out)


def add_hull_lesson(entry):
    with _mem_lock:
        d = _load()
        d["hull_lessons"] = (d.get("hull_lessons", []) + [entry])[
            -HULL_LESSON_KEEP:]
        _save(d)


def hull_lessons_for(own_cid, enemy_cid=None, n=4, engines=DUEL_ENGINES):
    """Lessons from rounds where the commander flew own_cid, best first:
    fought against enemy_cid > written after a win > a 1v1 > newer.  Retired
    lessons and duplicate texts dropped; a lesson from a win may come from
    any WIN_ENGINES brain."""
    if not own_cid:
        return []
    own_l, en_l = own_cid.lower(), (enemy_cid or "").lower()
    rows = []
    for e in _load().get("hull_lessons", []):
        if not e.get("lesson") or e.get("retired"):
            continue
        won = e.get("result") == "won"
        if e.get("engine") not in engines and not (
                won and e.get("engine") in WIN_ENGINES):
            continue
        if own_l not in [c.lower() for c in e.get("own", [])]:
            continue
        vs = bool(en_l) and en_l in [c.lower() for c in e.get("enemy", [])]
        rows.append(((1 if vs else 0, 1 if won else 0,
                      1 if len(e.get("own", [])) == 1 else 0,
                      e.get("time", 0)), e))
    rows.sort(key=lambda r: r[0], reverse=True)
    out, seen = [], set()
    for _k, e in rows:
        t = e["lesson"].strip().lower()
        if t in seen:
            continue
        seen.add(t)
        out.append(e)
        if len(out) >= n:
            break
    return out


def _label(cid):
    return intel._db().get(cid, {}).get("label", cid)


def hull_lessons_text(own_cid, enemy_cid=None, n=4):
    """The paragraph a duel brain / strategist gets, or ""."""
    rows = hull_lessons_for(own_cid, enemy_cid, n)
    ev = evidence_text(own_cid, enemy_cid)
    if not rows:
        return ev
    lines = [ev] if ev else []
    lines.append("Lessons from sparring in your hull (%s), written by the "
                 "commander who flew it (a lesson from a loss is a guess; "
                 "the record above is fact):" % _label(own_cid))
    for e in rows:
        size = len(e.get("own", [])) or 1
        lines.append("- vs %s (%dv%d, %s): %s" % (
            " + ".join(_label(c) for c in e.get("enemy", [])) or "?",
            size, len(e.get("enemy", [])) or 1, e.get("result", "?"),
            e["lesson"]))
    return "\n".join(lines)


def retire_hull_lessons(own_cid, enemy_cid, pattern, reason,
                        engine="claude", result="lost"):
    """Mark matching lessons retired (kept, never served). Returns count."""
    rx = re.compile(pattern, re.I)
    n = 0
    with _mem_lock:
        d = _load()
        for e in d.get("hull_lessons", []):
            if e.get("retired") or e.get("engine") != engine or \
                    e.get("result") != result:
                continue
            if own_cid.lower() not in [c.lower() for c in e.get("own", [])]:
                continue
            if enemy_cid and enemy_cid.lower() not in \
                    [c.lower() for c in e.get("enemy", [])]:
                continue
            if rx.search(e.get("lesson", "")):
                e["retired"] = reason
                n += 1
        if n:
            _save(d)
    return n


def backfill_round_evidence(log_dir):
    """Give banked brain rounds the ids + manoeuvre counts evidence_for
    needs, from their spar logs (matched on the row's time).  Idempotent.
    Returns the number of rounds filled."""
    import glob
    by_time = {}
    for f in glob.glob(os.path.join(log_dir, "spar_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                lg = json.load(fh)
        except (OSError, ValueError):
            continue
        b = lg.get("brains") or {}
        if b.get("time") and b.get("decisions"):
            by_time[b["time"]] = (lg, b)
    n = 0
    with _mem_lock:
        d = _load()
        for r in d.get("rounds", []):
            if r.get("man") or r.get("time") not in by_time:
                continue
            lg, b = by_time[r["time"]]
            r["ids"] = {"red": list(lg.get("red") or []),
                        "blue": list(lg.get("blue") or [])}
            r["man"] = {k: manoeuvre_counts(b["decisions"].get(k))[1]
                        for k in ("claude", "local")}
            r["orders"] = {k: manoeuvre_counts(b["decisions"].get(k))[0]
                           for k in ("claude", "local")}
            n += 1
        if n:
            _save(d)
    return n


def _ids_for_labels(text):
    """'Damar DN + First DN' -> class ids (exact label match, first hit)."""
    out = []
    by_label = {}
    for cid, d in intel._db().items():
        by_label.setdefault(d.get("label", ""), cid)
    for part in text.split(" + "):
        cid = by_label.get(part.strip())
        if cid:
            out.append(cid)
    return out


def backfill_hull_lessons():
    """Index memory notes written before hull tagging existed. Idempotent:
    a note already indexed (same model + text) is skipped. Returns count."""
    import re
    d = _load()
    have = set((e.get("model"), e.get("note")) for e in
               d.get("hull_lessons", []))
    added = 0
    for key, book in d.get("memory", {}).items():
        for model, notes in book.items():
            for note in notes:
                if (model, note) in have:
                    continue
                m = re.match(r"^(\S+) r\d+: flew (.+?) vs (.+?), (WON|LOST|"
                             r"DREW) in (\d+) ticks.*?(?:\. Lesson: (.*))?$",
                             note)
                if not m or not m.group(6):
                    continue
                own, enemy = _ids_for_labels(m.group(2)), \
                    _ids_for_labels(m.group(3))
                if not own:
                    continue
                d.setdefault("hull_lessons", []).append({
                    "own": own, "enemy": enemy, "model": model,
                    "engine": "claude" if model.startswith("claude")
                    else "local",
                    "result": m.group(4).lower(), "lesson": m.group(6),
                    "key": key, "time": 0, "note": note})
                added += 1
    if added:
        with _mem_lock:
            cur = _load()
            cur["hull_lessons"] = d["hull_lessons"][-HULL_LESSON_KEEP:]
            _save(cur)
    return added


def record(row):
    """Bank one round. row["void"] rounds are logged but never scored."""
    d = _load()
    d.setdefault("rounds", []).append(row)
    d["rounds"] = d["rounds"][-200:]
    if not row.get("void"):
        for book, k in (("by_key", row.get("key", "?")),
                        ("by_pair", "%s vs %s" % (row.get("claude_model"),
                                                  row.get("local_model")))):
            rec = d.setdefault(book, {}).setdefault(
                k, {"claude": 0, "local": 0, "draw": 0, "rounds": 0})
            rec["rounds"] += 1
            rec[row.get("winner_brain", "draw")] = \
                rec.get(row.get("winner_brain", "draw"), 0) + 1
    with _mem_lock:
        cur = _load()
        d["memory"] = cur.get("memory", d.get("memory", {}))
        d["hull_lessons"] = cur.get("hull_lessons",
                                    d.get("hull_lessons", []))
        _save(d)
    return d
