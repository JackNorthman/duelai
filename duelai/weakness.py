"""Measured hull weaknesses: "take out its impulse and it's a dead duck".

Player feedback: "can it also log weaknesses it finds too - ie, for the
enterprise D - take out its impulse and it's a dead duck".

Every fight (duel and sparring) is watched per ship: the first tick each
subsystem drops to DOWN_PCT or below, and the ship's hull at that moment.
When the fight ends, each knockout that happened EARLY - hull still at or
above EARLY_HULL, so it is a cause and not just part of the death rattle -
is banked against the ship's class, with whether the ship then died inside
WINDOW ticks.  A subsystem whose early loss kills that class far more often
than the class dies anyway is a weakness, and the prompts say so:

    Measured weaknesses of the Galaxy CVB (from 7 ships fought):
    - impulse engines down early 4x -> dead within 150 ticks 4x (avg 38):
      take them out (target engines) and it is a dead duck.

Deterministic, no LLM: the numbers are the evidence.  Used by the duel
brain (the player's hull, and the AI's own hull as a thing to protect),
the strategist's plan, and both sparring commanders (identically).

Data: data/weakness.json  {class: {ships, deaths, subs: {key: {down, died,
ticks}}, last}}
"""

import json
import os
import time

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "weakness.json")

DOWN_PCT = 20        # subsystem condition at or below this = knocked out
EARLY_HULL = 40      # knockout only counts while hull is still >= this
WINDOW = 150         # ticks: "died after" means died inside this window
MIN_DOWN = 2         # knockouts needed before anything is claimed
DUCK_RATIO = 0.6     # died-after share that makes it a weakness...
LIFT = 0.2           # ...and at least this far above the class's base rate

# telemetry key -> (words, the DUELAI target verb that hits it, or "")
SUBS = {
    "Eng": ("impulse engines", "engines"),
    "Weap": ("weapons", "weapons"),
    "Sens": ("sensors", "sensors"),
    "Warp": ("warp engines", ""),
    "Pow": ("power plant", ""),
    "Torp": ("torpedo launchers", ""),
    "Rep": ("repair bay", ""),
}


class Tracker:
    """One fight's worth of observations.  Keys are per-fight ship ids."""

    def __init__(self):
        self.ships = {}

    def see(self, key, cls, tick, reading, alive=True):
        """reading: dict with Hull + any SUBS keys (percent)."""
        if not cls:
            return
        r = self.ships.get(key)
        if r is None:
            if not alive:
                return          # a wreck from before this fight
            r = self.ships[key] = {"cls": cls, "down": {}, "dead": None}
        if r["dead"] is not None:
            return
        if not alive:
            r["dead"] = tick
            return
        hull = reading.get("Hull", 100)
        for k in SUBS:
            v = reading.get(k)
            # a child engine knocked out counts, whatever the worst
            # reading says (EngOut/WarpOut since the 10-29 engine fix)
            if k in ("Eng", "Warp") and (reading.get(k + "Out") or 0) >= 1:
                v = 0
            if isinstance(v, int) and 0 <= v <= DOWN_PCT and k not in r["down"]:
                r["down"][k] = (tick, hull)

    def mark_dead(self, key, tick):
        r = self.ships.get(key)
        if r is not None and r["dead"] is None:
            r["dead"] = tick


def _load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


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


def record(tracker):
    """Bank a finished fight.  Returns the classes touched."""
    if not tracker or not tracker.ships:
        return []
    d = _load()
    touched = []
    for r in tracker.ships.values():
        c = d.setdefault(r["cls"], {"ships": 0, "deaths": 0, "subs": {}})
        c["ships"] += 1
        if r["dead"] is not None:
            c["deaths"] += 1
        for k, (t, hull) in r["down"].items():
            if hull < EARLY_HULL:
                continue
            s = c["subs"].setdefault(k, {"down": 0, "died": 0, "ticks": 0})
            s["down"] += 1
            if r["dead"] is not None and 0 <= r["dead"] - t <= WINDOW:
                s["died"] += 1
                s["ticks"] += r["dead"] - t
        c["last"] = int(time.time())
        if r["cls"] not in touched:
            touched.append(r["cls"])
    _save(d)
    return touched


def verdicts(cls, d=None):
    """[(ratio, line)] weaknesses for a class, strongest first."""
    c = (d if d is not None else _load()).get(cls)
    if not c:
        return []
    base = c["deaths"] / float(c["ships"]) if c.get("ships") else 0.0
    out = []
    for k, s in c.get("subs", {}).items():
        if s["down"] < MIN_DOWN or k not in SUBS:
            continue
        ratio = s["died"] / float(s["down"])
        if ratio < DUCK_RATIO or ratio < base + LIFT:
            continue
        words, verb = SUBS[k]
        avg = s["ticks"] // s["died"] if s["died"] else 0
        how = ("take them out%s and it is a dead duck" if ratio >= 0.8
               else "losing them usually finishes it%s")
        out.append((ratio, "%s down early %dx -> dead within %d ticks %dx "
                    "(avg %d): %s." % (words, s["down"], WINDOW, s["died"],
                                       avg, how % ((" (target %s)" % verb)
                                                   if verb else ""))))
    out.sort(key=lambda x: -x[0])
    return out


def _label(cls):
    from . import intel
    return intel._db().get(cls, {}).get("label", cls)


def text_for(cls, own=False):
    """Prompt paragraph for one hull, or "" when nothing is proven."""
    d = _load()
    v = verdicts(cls, d)
    if not v:
        return ""
    n = d.get(cls, {}).get("ships", 0)
    head = ("Your own hull's measured weak spot - protect it" if own else
            "Measured weaknesses of the %s" % _label(cls))
    return "%s (from %d ships fought):\n%s" % (
        head, n, "\n".join("- " + line for _r, line in v[:3]))


def table():
    return _load()
