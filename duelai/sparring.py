"""Sparring mode: the AI stages both sides of a battle and learns from it.

The player launches QuickBattle and arms the link as usual; instead of one duel
opponent the director spawns a RED force (side 0, the enemy group) and a
BLUE force (side 1, the friendly group).  Both fight under the game's own
compound AI (FedAttack / NonFedAttack / MultiAttack ...) - the stock
brains against each other - while the director watches, records the
track, and banks what each matchup teaches.

Results accumulate in data/sparring.json:
  { "<matchup key>": {"red": .., "blue": .., "rounds": n,
                      "avg_ticks": .., "notes": [..] } }

That table is real measured strength - where the paper stats in
ships.json lie (a 32-phaser hull with terrible arcs), sparring corrects
them.
"""

import json
import os
import random
import time

from . import intel

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "sparring.json")

MAX_SIDE = 4          # ships per side ceiling (engine tolerates ~14 total)
ROUND_TICK_LIMIT = 900
DEFAULT_SPAWN = 5000  # a set with no "@": sent explicitly, the game keeps the last
CLOAK_SPAWN = 1500    # a cloak set with no "@": room to go dark and stalk in
BRAINS_SPAWN = 2000   # a brains set with no "@": at 5000 both commanders pay
                      # for a minute of "keep closing" before anything happens


def _load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
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


def parse_spec(text):
    """'spar borg vs federation 2v2 x3 tng' -> spec dict (None if not spar).

    Accepted anywhere after the keyword: 'RvB' or 'NvN' sizes, 'xN'
    rounds, an era word, and '<red race> vs <blue race>'."""
    words = str(text or "").replace(":", " ").split()
    if not words or words[0].lower() not in ("spar", "sparring", "practice"):
        return None
    words = [w.lower() for w in words[1:]]
    spec = {"red": "", "blue": "", "size": 2, "rounds": 1, "era": "",
            "stance_red": "", "stance_blue": "", "cloak": "", "brains": ""}
    rest = []
    import re
    for w in words:
        m = re.match(r"^(\d)v\d$", w)
        if m:
            spec["size"] = max(1, min(MAX_SIDE, int(m.group(1)))); continue
        m = re.match(r"^x(\d{1,2})$", w)
        if m:
            spec["rounds"] = max(1, min(20, int(m.group(1)))); continue
        if w in ("tng", "tmp", "ent"):
            spec["era"] = w; continue
        m = re.match(r"^@(\d{3,5})$", w)
        if m:
            # engagement distance: "@900" spawns the sides 900m apart instead
            # of the 5000 default.  Decides which half of a ship's armament is
            # allowed to matter at all.
            spec["spawn"] = max(150, min(20000, int(m.group(1)))); continue
        if w in ("nogods", "classic", "fair"):
            spec["gods"] = 0; continue
        if w == "gods":
            spec["gods"] = 1; continue
        # Cloak learning: "cloak=red|blue|both" hands every cloak-capable hull
        # on those sides to doctrine.cloak_reflex under the play the cloak
        # learner picks for it each round ("on" = both, "off" = nobody).
        m = re.match(r"^cloak=(off|on|red|blue|both)$", w)
        if m:
            spec["cloak"] = {"on": "both", "off": ""}.get(m.group(1),
                                                         m.group(1))
            continue
        # Brains sparring (sparbrains.py): "brains=red" = Claude commands red
        # and the local model blue, "brains=blue" the reverse, "brains=swap"
        # (or "brains=on") alternates Claude's side every round.
        m = re.match(r"^brains=(red|blue|swap|on)$", w)
        if m:
            spec["brains"] = "swap" if m.group(1) == "on" else m.group(1)
            continue
        # Per-side stance: "red=aggressive blue=balanced", or "stance=ambush"
        # for both.  Sparring used to slam "balanced" on every hull it spawned
        # (RegisterFleetShip), which is the 0.7 band - so SmartPhasers was
        # never on for ANY spar, and no beam-dwell question could be answered.
        # Omitted or "auto" = DUELAI picks per hull from the weapon profile.
        m = re.match(r"^(red|blue|stance)=([a-z]+)$", w)
        if m:
            side, val = m.group(1), m.group(2)
            if val in intel.STANCE_NAMES or val == "auto":
                if side == "stance":
                    spec["stance_red"] = spec["stance_blue"] = val
                else:
                    spec["stance_" + side] = val
            continue
        rest.append(w)
    if "vs" in rest:
        i = rest.index("vs")
        spec["red"] = " ".join(rest[:i])
        spec["blue"] = " ".join(rest[i + 1:])
    return spec


# Non-canon / overtuned hulls barred from nogods drafting BY NAME, whatever
# their hull number says (the Borg Hunter is 32k hull with a 66 broadside -
# Scimitar guns under the cap).  Extend via "god_ships": [...] in
# data/config.json without touching code.
GOD_CLASSES = {"kquonvecbh", "axiom", "danubergod"}


def _god_classes():
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(DATA_PATH),
                                          "config.json"),
                             encoding="utf-8"))
        extra = set(str(x).lower() for x in cfg.get("god_ships", []))
    except (OSError, ValueError):
        extra = set()
    return GOD_CLASSES | extra


GOD_HULL_CAP = 80000     # nogods mode: excludes the future-era superships
                         # (Janeway 205k, Relativity 180k, CubeSP 130k,
                         # EntK 105k, Wells 85k, DS9Wings 240k) while keeping
                         # the top conventional hulls (Scimitar 72k, Vulture 70k)


def _pool(race, era, gods=1):
    """Combat hulls for one side, era-respecting; falls back to any race."""
    if era and era != "tng":
        exact = intel.pools_for_race("%s %s" % (era.upper(), race),
                                     exact=True)
        cands = (exact[0] + exact[1])
        if cands:
            return _god_filter(cands, gods)
    esc, heavy = intel.pools_for_race(race)
    return _god_filter(esc + heavy, gods)


def _god_filter(cands, gods):
    if gods:
        return cands
    barred = _god_classes()
    kept = [c for c in cands
            if intel._db().get(c, {}).get("hull", 0) <= GOD_HULL_CAP
            and c.lower() not in barred]
    return kept or cands     # never empty a pool entirely


def _resolve_side(token, era, gods):
    """(pool, label, manual) for one side of a matchup.

    A side normally names a RACE and draws from that race's pool.  If it does
    not match a race, it is tried as a HULL - so "spar sovereign vs neghvar"
    fights those two ships specifically instead of drafting.  Exact class id
    first (the picker sends ids), then intel's fuzzy match for typed text.

    A named hull is an explicit invitation, so the god filter does not apply
    to it: there is no pool to filter, and barring a ship the player asked for by
    name would just be baffling.  The matchup key is tagged "(manual)" so
    hand-picked results never mix with drafted ones in sparring.json."""
    pool = _pool(token, era, gods)
    if pool:
        return pool, token, 0
    want = (token or "").strip().lower()
    if not want:
        return [], token, 0
    db = intel._db()
    for sid in db:
        if sid.lower() == want:
            return [sid], db[sid].get("label", sid), 1
    sid, ship = intel.find_ship(token)
    if sid:
        return [sid], ship.get("label", sid), 1
    return [], token, 0


# The auto-explorer's rotation.  Built from the database via
# intel.all_races() rather than a hand-kept list, so a race added to a Bedrock
# stub joins the rotation on the next ships.json refresh - the old seven-name
# list is why Omega Fleet never turned up in an auto set after 09-03.
#
# Only the UNSPECIFIED side of a set is drawn from here; every race remains
# available as a typed or capsule pick, exclusions included.  Extend or
# override with "auto_race_exclude": [...] in data/config.json.
AUTO_RACE_EXCLUDE = (
    # All four hulls are OVER GOD_HULL_CAP, so a nogods draft cannot filter
    # them: _god_filter falls back to the unfiltered pool rather than empty
    # one, and a "fair" random set would field a Wells and its 2.34M shields.
    "future fleet",
    # One hull. Not a faction.
    "godships",
    # The junk drawer: stations, asteroid husks, and a Ghost Shuttle rated
    # 999,999,999 hull.  Species 8472 lives here too, which is why it stays
    # pickable by name.
    "other",
)


def auto_races():
    """Races the explorer may draw an unspecified side from."""
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(DATA_PATH),
                                          "config.json"), encoding="utf-8"))
        skip = cfg.get("auto_race_exclude", None)
        if skip is None:
            skip = AUTO_RACE_EXCLUDE
    except (OSError, ValueError):
        skip = AUTO_RACE_EXCLUDE
    skip = set(str(s).lower() for s in skip)
    # min_pool=4: a two-hull race cannot fill both sides of a 4v4 without
    # repeating itself, which makes for a poor exploratory set.
    return [r for r, _n, _p in intel.all_races(min_pool=4) if r not in skip]


def build_scenario(spec):
    """spec -> {red: [(class,name)..], blue: [..], key} or error string."""
    races = auto_races()
    red_race = spec.get("red")
    blue_race = spec.get("blue")
    if not red_race and not blue_race:
        # Full auto explores: pick the LEAST-TESTED pairing for this era,
        # so repeated auto sets walk the whole matchup space instead of
        # rolling the same dice every time.
        table = _load()
        era_tag = (spec.get("era") or "tng")
        def tested(a, b):
            n = 0
            for k, v in table.items():
                if not k.startswith(era_tag + "-"):
                    continue
                if ("-%s vs %s" % (a, b)) in k or ("-%s vs %s" % (b, a)) in k:
                    n += v.get("rounds", 0)
            return n
        pairs = [(a, b) for i, a in enumerate(races)
                 for b in races[i + 1:]]
        least = min(tested(a, b) for a, b in pairs)
        a, b = random.choice([p for p in pairs if tested(*p) == least])
        red_race, blue_race = random.choice([(a, b), (b, a)])
    elif not red_race:
        red_race = random.choice([r for r in races if r != blue_race])
    elif not blue_race:
        blue_race = random.choice([r for r in races if r != red_race])
    size = spec.get("size", 2)
    gods = spec.get("gods", 1)
    era = spec.get("era", "")
    red_pool, red_race, red_manual = _resolve_side(red_race, era, gods)
    blue_pool, blue_race, blue_manual = _resolve_side(blue_race, era, gods)
    if not red_pool:
        return "no race or hull matches red '%s'" % red_race
    if not blue_pool:
        return "no race or hull matches blue '%s'" % blue_race
    def side(pool, tag):
        picks = []
        chosen = random.sample(pool, min(size, len(pool)))
        while len(chosen) < size:
            chosen.append(random.choice(pool))
        for i, cid in enumerate(chosen):
            picks.append((cid, "%s %s-%d"
                          % (tag, intel._db().get(cid, {}).get("label", cid)
                             .split()[0], i + 1)))
        return picks
    if red_manual or blue_manual:
        tag = " (manual)"
    elif not gods:
        tag = " (nogods)"
    else:
        tag = ""
    if spec.get("brains"):
        # the commanders own the cloak in a brains set; the learner would
        # be a third brain on the board
        spec["cloak"] = ""
        if not spec.get("spawn"):
            spec["spawn"] = BRAINS_SPAWN
    if spec.get("cloak") and not spec.get("spawn"):
        # a cloak play needs room to go dark and stalk: at the 5000 default a
        # Defiant spends most of a round closing, at 400 it starts in the guns
        spec["spawn"] = CLOAK_SPAWN
    if spec.get("spawn"):
        tag = tag + " @%d" % spec["spawn"]
    if spec.get("cloak"):
        tag = tag + " cloak=%s" % spec["cloak"]
    if spec.get("brains"):
        # kept out of the stock-AI table: these rounds are commanded
        tag = tag + " brains"
    key = "%s-%dv%d-%s vs %s%s" % (spec.get("era") or "tng", size, size,
                                   red_race, blue_race, tag)
    out_spawn = spec.get("spawn")
    return {"spawn": out_spawn,
            "red": side(red_pool, "RED"), "blue": side(blue_pool, "BLUE"),
            "red_race": red_race, "blue_race": blue_race,
            "manual_red": red_manual, "manual_blue": blue_manual,
            "size": size, "gods": gods, "key": key,
            "stance_red": spec.get("stance_red", ""),
            "stance_blue": spec.get("stance_blue", ""),
            "cloak": spec.get("cloak", ""),
            "brains": spec.get("brains", "")}


def redraft_side(scen, side_key, era=""):
    """Redraw one side's hulls from its race pool, avoiding classes it has
    already fielded this set.  The losing side redrafts between rounds -
    a set becomes "can this race field ANYTHING that wins?", not a replay.
    Returns the new labels for the announcement."""
    if scen.get("manual_%s" % side_key):
        return []          # hand-picked hull: there is nothing to redraft to
    race = scen.get("%s_race" % side_key, "")
    pool = _pool(race, era, scen.get("gods", 1))
    if not pool:
        return []
    tried = scen.setdefault("tried_%s" % side_key, [])
    for cid, _ in scen.get(side_key, []):
        if cid not in tried:
            tried.append(cid)
    fresh = [c for c in pool if c not in tried]
    if not fresh:
        fresh = pool                     # pool exhausted: allow repeats
    size = scen.get("size", 2)
    chosen = random.sample(fresh, min(size, len(fresh)))
    while len(chosen) < size:
        chosen.append(random.choice(pool))
    tag = side_key.upper()
    scen[side_key] = [(cid, "%s %s-%d"
                       % (tag, intel._db().get(cid, {}).get("label", cid)
                          .split()[0], i + 1))
                      for i, cid in enumerate(chosen)]
    return [intel._db().get(c, {}).get("label", c)
            for c, _ in scen[side_key]]


def labels_for(scen, side_key):
    return [intel._db().get(c, {}).get("label", c)
            for c, _ in scen.get(side_key, [])]


def record_round(key, winner, ticks, note=""):
    d = _load()
    rec = d.setdefault(key, {"red": 0, "blue": 0, "draw": 0, "rounds": 0,
                             "avg_ticks": 0, "notes": []})
    rec["rounds"] += 1
    rec[winner] = rec.get(winner, 0) + 1
    rec["avg_ticks"] = int((rec["avg_ticks"] * (rec["rounds"] - 1) + ticks)
                           / rec["rounds"])
    if note:
        rec["notes"] = (rec["notes"] + [note])[-5:]
    rec["last"] = int(time.time())
    _save(d)
    return rec


def table():
    return _load()


def analyze_round(track, winner, key):
    """Deterministic facts about a finished sparring round.

    track rows: {tick, red_hull, blue_hull, red_alive, blue_alive}"""
    facts = []
    if len(track) < 4:
        return facts
    # first blood side
    for a, b in zip(track, track[1:]):
        if b["red_hull"] < a["red_hull"] - 2:
            facts.append("blue drew first blood"); break
        if b["blue_hull"] < a["blue_hull"] - 2:
            facts.append("red drew first blood"); break
    # shield collapse timing (needs the _sh fields, present since pt7)
    dur0 = track[-1]["tick"] - track[0]["tick"]
    for side in ("red", "blue"):
        col = [s for s in track if s.get(side + "_sh", 100) <= 25]
        if col and dur0:
            frac = (col[0]["tick"] - track[0]["tick"]) / float(dur0)
            facts.append("%s shields collapsed %d%% of the way in"
                         % (side, int(frac * 100)))
    # attrition or alpha?
    dur = track[-1]["tick"] - track[0]["tick"]
    if dur and winner in ("red", "blue"):
        loser = "blue" if winner == "red" else "red"
        half = [s for s in track
                if s["%s_hull" % loser] <= 50]
        if half:
            frac = (half[0]["tick"] - track[0]["tick"]) / float(dur)
            facts.append("the losing side was at half strength %d%% of the "
                         "way in" % int(frac * 100))
    return facts
