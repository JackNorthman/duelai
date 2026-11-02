"""Cloak learner: which cloak PLAY actually wins, measured by sparring.

Two cloak brains existed before this and neither was ever measured: the
game's CloakAttack (hide-and-strike on fixed timers - "a cloaked ship only
evades") and doctrine.cloak_reflex, a playbook hand-tuned in
duels against the player.  A sparring set with `cloak=red|blue|both` hands every
cloak-capable hull on those sides to cloak_reflex under ONE play per round:

  off       never cloaks - the control; a play that cannot beat it is noise
  mend      cloaks only to break contact and repair, then fights again
  alpha     mend + stalk from outside its band, decloak point-blank, stay
  wolfpack  mend + strike, recloak, strike again from a new bearing

Each round's result for that ship's side (win / draw / loss) is banked
against (hull, play).  The next round's play comes from Thompson sampling
over those records: untried plays first, then plays that win get drawn more
often, while a lucky streak cannot lock the others out for good.  Duels read
the verdict back through duel_play() once a hull has DUEL_MIN_ROUNDS.

Persists in data/cloak_learning.json:
  hulls: {class: {plays: {play: stats}, vs: {opponent class: {play: stats}}}}
  log:   the last MAX_LOG banked rounds, side included (a side bias shows)
"""

import json
import os
import random
import time

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "cloak_learning.json")

PLAYS = ("off", "mend", "alpha", "wolfpack")
MIN_TRIES = 2           # rounds each play gets before sampling takes over
DUEL_MIN_ROUNDS = 8     # banked rounds before a duel trusts the verdict
MAX_LOG = 200


def _load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("hulls", {})
    d.setdefault("log", [])
    return d


def _save(d):
    tmp = DATA_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, DATA_PATH)
    except OSError:
        pass


def _blank():
    return {"n": 0, "w": 0, "d": 0, "l": 0, "cloaks": 0, "strikes": 0}


def _plays(d, hull):
    got = d["hulls"].get(hull, {}).get("plays", {})
    return {p: got.get(p) or _blank() for p in PLAYS}


def _mean(a):
    """Posterior mean win rate: Beta(1,1) prior, a draw counts half."""
    return (1.0 + a["w"] + 0.5 * a["d"]) / (2.0 + a["n"])


def choose(hull, rng=random):
    """(play, why) for this hull's next round."""
    arms = _plays(_load(), hull)
    untried = [p for p in PLAYS if arms[p]["n"] < MIN_TRIES]
    if untried:
        least = min(arms[p]["n"] for p in untried)
        pick = rng.choice([p for p in untried if arms[p]["n"] == least])
        return pick, "exploring (%s has %d of its %d trial rounds)" % (
            pick, arms[pick]["n"], MIN_TRIES)
    draw = {}
    for p in PLAYS:
        a = arms[p]
        draw[p] = rng.betavariate(1.0 + a["w"] + 0.5 * a["d"],
                                  1.0 + a["l"] + 0.5 * a["d"])
    pick = max(PLAYS, key=lambda p: draw[p])
    return pick, "sampled from " + summary(hull, arms)


def record(hull, play, outcome, opponent="", key="", side="", ticks=0,
           cloaks=0, strikes=0):
    """Bank one round.  outcome is 'w' | 'd' | 'l' for this ship's side."""
    if not hull or play not in PLAYS or outcome not in ("w", "d", "l"):
        return None
    d = _load()
    h = d["hulls"].setdefault(hull, {})
    vs = h.setdefault("vs", {}).setdefault(opponent or "?", {})
    for tab in (h.setdefault("plays", {}), vs):
        a = tab.setdefault(play, _blank())
        a["n"] += 1
        a[outcome] += 1
        a["cloaks"] += int(cloaks)
        a["strikes"] += int(strikes)
    h["last"] = int(time.time())
    d["log"] = (d["log"] + [{
        "t": int(time.time()), "hull": hull, "vs": opponent, "play": play,
        "outcome": outcome, "side": side, "key": key, "ticks": int(ticks),
        "cloaks": int(cloaks), "strikes": int(strikes)}])[-MAX_LOG:]
    _save(d)
    return dict(h["plays"][play])


def summary(hull, arms=None):
    """'wolfpack 3-1 (75%), alpha 2-2 (50%), mend untried, off 0-2 (0%)'."""
    if arms is None:
        arms = _plays(_load(), hull)
    bits = []
    for p in sorted(PLAYS, key=lambda p: -_mean(arms[p])):
        a = arms[p]
        if not a["n"]:
            bits.append("%s untried" % p)
            continue
        bits.append("%s %d-%d%s (%d%%)" % (
            p, a["w"], a["l"], ("-%d" % a["d"]) if a["d"] else "",
            int(round(100.0 * (a["w"] + 0.5 * a["d"]) / a["n"]))))
    return ", ".join(bits)


def best(hull, min_rounds=DUEL_MIN_ROUNDS):
    """(play, rounds banked).  The best-scoring play that has had its trial
    rounds, or (None, rounds) while the hull has fewer than min_rounds."""
    arms = _plays(_load(), hull)
    n = sum(a["n"] for a in arms.values())
    tried = [p for p in PLAYS if arms[p]["n"] >= MIN_TRIES]
    if n < min_rounds or not tried:
        return None, n
    return max(tried, key=lambda p: _mean(arms[p])), n


def duel_play(hull):
    """The play a duel flagship of this class should use, or None to keep
    doctrine's own speed/hull heuristic."""
    return best(hull)[0]


def lesson(hull, label=""):
    arms = _plays(_load(), hull)
    n = sum(a["n"] for a in arms.values())
    top = best(hull, min_rounds=1)[0]
    return ("Cloak plays measured in sparring for %s over %d rounds: %s.%s"
            % (label or hull, n, summary(hull, arms),
               (" Leading play: %s." % top) if top else ""))


def table():
    return _load()
