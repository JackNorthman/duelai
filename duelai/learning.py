"""Cross-battle learning: the opponent model that compounds over time.

Everything persists in data/learning.json:
  battles  - one record per finished battle
  lessons  - short distilled lines (LLM-written post-battle, capped)
  opponent - aggregates rebuilt from the battle list

The output of this module is brief_context(): a paragraph injected into
the commander's system prompt at battle start, so every new fight begins
with everything previous fights taught.
"""

import json
import os
import time

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "learning.json")

MAX_BATTLES = 200
MAX_LESSONS = 12


def _load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("battles", [])
    d.setdefault("lessons", [])
    return d


def _save(d):
    # Write-then-rename (10-30): brains.json was found spliced - two saves
    # at 17:42:37 interleaved over the share, every read after that came
    # back {} and a logged duel flew with no lessons.  A per-writer temp name so two
    # writers never share one either.
    tmp = "%s.%d.%d.tmp" % (DATA_PATH, os.getpid(), id(d))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, DATA_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def record_battle(debrief, observed):
    """Store one finished battle.

    observed: dict of things the director noticed live -
      player_ship, player_cloaked (bool), player_min_hull (int),
      ai_first (did the AI draw first blood), fighters_seen (int)
    """
    d = _load()
    rec = {
        "t": int(time.time()),
        "result": debrief.get("result", ""),
        "won": debrief.get("result", "").startswith("victory"),
        "difficulty": debrief.get("difficulty", ""),
        "ai_ship": debrief.get("ship_class", ""),
        "duration_ticks": debrief.get("duration_ticks", 0),
    }
    rec.update({k: v for k, v in (observed or {}).items()
                if k in ("player_ship", "player_cloaked", "player_min_hull",
                         "fighters_seen")})
    # Output check for dud_hulls: samples spent inside our own reach with
    # weapons up, and hull actually dealt (evaluate.py's metrics).
    try:
        m = (debrief.get("analysis") or {}).get("metrics") or {}
        rec["in_reach"] = int(sum(v.get("in_reach", 0)
                                  for v in m.get("dealt_by_target", {}).values()))
        rec["dealt"] = int(sum(m.get("dealt_by_band", {}).values()))
    except Exception:
        pass
    d["battles"].append(rec)
    d["battles"] = d["battles"][-MAX_BATTLES:]
    _save(d)
    return rec


DUD_MIN_REACH = 30      # samples inside own reach, weapons up
DUD_MAX_DEALT = 1       # hull % dealt in that time


def dud_hulls():
    """Class ids whose LATEST duel with a real firing window landed nothing.

    2026-09-16, two logged duels (KD5M, KD7M): 56 and 46 samples inside their own
    reach with weapons at 100%, 1 and 0 hull dealt, player never below 99.
    Both hardpoints carry a 20-unit "Mauler" in the beam bank and
    SetMaxTorpedoes(0, 0); the engine-side cause is unproven, so the bench
    is evidence-driven: a hull is out while its most recent duel with
    in_reach >= DUD_MIN_REACH dealt <= DUD_MAX_DEALT, and back the moment a
    later duel shows it landing hits.  Records without the fields (older
    than this rule) never count."""
    d = _load()
    latest = {}
    for rec in d.get("battles", []):
        if rec.get("in_reach") is None or rec.get("dealt") is None:
            continue
        if rec.get("in_reach", 0) < DUD_MIN_REACH:
            continue
        latest[rec.get("ai_ship", "")] = rec
    return sorted(sid for sid, rec in latest.items()
                  if sid and rec.get("dealt", 0) <= DUD_MAX_DEALT)


# ---------------------------------------------------------------- lessons
#
# A lesson is a dict: {text, topic, count, first, last, since}
#   topic - stable key from evaluate.py candidates ("" for LLM free text)
#   count - how many battles have confirmed it
#   since - battles fought since it last helped (retirement clock)
# Stored under d["lessons2"]; the legacy d["lessons"] string list is
# migrated on first load and kept only as a mirror for old readers.

_STOP = {"the", "their", "a", "an", "to", "of", "and", "or", "is", "are",
         "from", "first", "second", "hit", "blow", "cannot", "too", "them",
         "they", "it", "-", "let"}


def _tokens(text):
    out = set()
    for w in str(text).split():
        w = w.strip(".,;:!?\"'").lower()
        if w.endswith("s") and len(w) > 3:
            w = w[:-1]                      # light stemming: repairs=repair
        if w and w not in _STOP:
            out.add(w)
    return out


def _similar(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _migrate(d):
    if "lessons2" in d:
        return d
    d["lessons2"] = []
    for text in d.get("lessons", []):
        _absorb(d, "", text, bump_only=False)
    return d


def _note_hull(l, ship_class, text):
    """Per-hull record on a lesson: how often THIS hull's battles confirmed
    it, and the wording from this hull's latest battle (its own numbers -
    the knife lesson reads 125m in a Challenger and 145m in a Vor'Melak)."""
    if not ship_class:
        return
    h = l.setdefault("hulls", {}).setdefault(ship_class, {"n": 0})
    h["n"] = h.get("n", 0) + 1
    h["text"] = text
    h["last"] = int(time.time())


def _absorb(d, topic, text, bump_only=False, ship_class=""):
    """Merge one candidate into the store; returns 'new'|'confirmed'|''."""
    text = " ".join(str(text).split())[:220]
    if not text:
        return ""
    ls = d.setdefault("lessons2", [])
    for l in ls:
        if (topic and l.get("topic") == topic) or _similar(l["text"], text) >= 0.5:
            l["count"] = l.get("count", 1) + 1
            l["last"] = int(time.time())
            l["since"] = 0
            if len(text) > len(l["text"]):     # keep the richer phrasing
                l["text"] = text
            _note_hull(l, ship_class, text)
            return "confirmed"
    if bump_only:
        return ""
    l = {"text": text, "topic": topic, "count": 1,
         "first": int(time.time()), "last": int(time.time()), "since": 0}
    _note_hull(l, ship_class, text)
    ls.append(l)
    return "new"


def _prune(d):
    """Age out lessons that stopped mattering.  Every battle bumps `since`
    on all lessons; a lesson unconfirmed for 15 battles and seen only
    once, or 30 battles regardless, retires."""
    keep = []
    for l in d.get("lessons2", []):
        since = l.get("since", 0)
        if since >= 30 or (since >= 15 and l.get("count", 1) <= 1):
            continue
        keep.append(l)
    keep.sort(key=lambda l: (-l.get("count", 1), -l.get("last", 0)))
    d["lessons2"] = keep[:MAX_LESSONS]
    d["lessons"] = [l["text"] for l in d["lessons2"]]  # legacy mirror


def add_lesson(text, topic="", ship_class=""):
    d = _migrate(_load())
    res = _absorb(d, topic, text, ship_class=ship_class)
    _prune(d)
    _save(d)
    return res


def replace_lesson(topic, text):
    """Drop whatever lesson holds this topic and write a fresh one.

    Per-round spar lessons baked stale scores into text that dedup then
    preserved ("0-1 over 1 rounds" shown after a 3-0 set); set-end
    replacement keeps exactly one current lesson per matchup."""
    d = _migrate(_load())
    d["lessons2"] = [l for l in d["lessons2"] if l.get("topic") != topic]
    res = _absorb(d, topic, text)
    _prune(d)
    _save(d)
    return res


def observe_battle(analysis, ship_class=""):
    """Fold a post-battle evaluation into the lesson store.

    Deterministic candidates confirm-or-create by topic; every lesson's
    retirement clock ticks once per battle."""
    d = _migrate(_load())
    for l in d.get("lessons2", []):
        l["since"] = l.get("since", 0) + 1
    outcomes = []
    for topic, text in (analysis or {}).get("candidates", []):
        outcomes.append((topic, _absorb(d, topic, text,
                                        ship_class=ship_class)))
    _prune(d)
    _save(d)
    return outcomes


def stats():
    d = _load()
    bs = d["battles"]
    n = len(bs)
    if n == 0:
        return {"battles": 0}
    wins = sum(1 for b in bs if b.get("won"))
    cloakers = sum(1 for b in bs if b.get("player_cloaked"))
    ships = {}
    for b in bs:
        s = b.get("player_ship") or "unknown"
        ships[s] = ships.get(s, 0) + 1
    fav = max(ships.items(), key=lambda kv: kv[1]) if ships else ("unknown", 0)
    by_diff = {}
    for b in bs:
        k = b.get("difficulty", "?")
        w, t = by_diff.get(k, (0, 0))
        by_diff[k] = (w + (1 if b.get("won") else 0), t + 1)
    d = _migrate(d)
    return {
        "battles": n, "ai_wins": wins, "player_wins": n - wins,
        "player_cloak_rate": round(cloakers / float(n), 2),
        "favourite_player_ship": fav[0],
        "by_difficulty": {k: {"ai_wins": w, "total": t}
                          for k, (w, t) in by_diff.items()},
        "lessons": d["lessons"],
        "lessons2": d.get("lessons2", []),
    }


import re as _re

_HULL_WORDS = _re.compile(r"\b\d{2,4}\s?m\b|\breach\b|\bour guns\b", _re.I)
SIMILAR_REACH = 0.20


def hull_scoped(l):
    """A lesson whose truth depends on the hull it was learned in: every
    range-band lesson (it is about where OUR guns reach) and any free-text
    lesson quoting metres or reach.  Opening, player-habit and cloak-play
    lessons are about the fight, not the hull, and reach every duel."""
    topic = l.get("topic") or ""
    if topic.startswith("range:"):
        return True
    if topic in ("", "claude"):
        return bool(_HULL_WORDS.search(l.get("text", "")))
    return False


def _reach(cid):
    from . import intel
    try:
        return (intel.weapon_profile(cid) or {}).get("reach", 0) or 0
    except Exception:
        return 0


def _for_hull(l, ship_class):
    """This lesson as it applies to ship_class, or None.

    Its own record first (own wording, own confirmation count); else the
    best-confirmed record from a hull whose reach is within 20%, labelled as
    such; else nothing - another hull's numbers would only mislead."""
    hulls = l.get("hulls") or {}
    if not hulls:
        return None
    own = hulls.get(ship_class)
    if own:
        out = dict(l, text=own.get("text", l["text"]), count=own.get("n", 1))
        return out
    r0 = _reach(ship_class)
    if not r0:
        return None
    best = None
    for cid, h in hulls.items():
        r = _reach(cid)
        if r and abs(r - r0) <= SIMILAR_REACH * r0:
            if best is None or h.get("n", 0) > best[1].get("n", 0):
                best = (cid, h)
    if not best:
        return None
    from . import intel
    lab = intel._db().get(best[0], {}).get("label", best[0])
    return dict(l, text="%s (learned in a %s, similar reach)"
                % (best[1].get("text", l["text"]), lab),
                count=best[1].get("n", 1))


def duel_lessons(lessons, ship_class=None, player_class=None):
    """The lessons that belong in a duel about these two hulls.

    - Hull-scoped lessons (range bands, quoted metres) come back in the AI
      hull's own wording, or a similar-reach hull's, or not at all.  The
      knife lesson was confirmed in 16 hulls but its text said "our guns
      only reach 125m" (a Challenger's) in every duel.
    - Sparring lines: a cloak-learner line ("cloak:<class id>") only for the
      AI's own hull; a "spar:<matchup>" result only when its key names one
      of the two hulls.  Duel cloak lessons ("cloak:repair_works") stay.
    ship_class None = no filtering (old behaviour)."""
    if not ship_class:
        return list(lessons)
    from . import intel
    db = intel._db()
    names = set()
    for cid in (ship_class, player_class):
        if cid:
            names.add(cid.lower())
            lab = db.get(cid, {}).get("label", "")
            if lab:
                names.add(lab.lower())
    out = []
    for l in lessons:
        topic = (l.get("topic") or "")
        if topic.startswith("cloak:") and topic[6:] in db:
            if topic[6:].lower() == ship_class.lower():
                out.append(l)
        elif topic.startswith("spar:"):
            key = topic[5:].lower()
            if any(n and n in key for n in names):
                out.append(l)
        elif hull_scoped(l):
            fit = _for_hull(l, ship_class)
            if fit:
                out.append(fit)
        else:
            out.append(l)
    out.sort(key=lambda l: (-l.get("count", 1), -l.get("last", 0)))
    return out


def backfill_lesson_hulls(log_dir):
    """One-off: rebuild per-hull records from the battle logs (each log has
    the AI hull, the lesson topics it created/confirmed and its own
    wording).  Free-text lessons are matched to the battle recorded nearest
    their first/last timestamps.  Returns lessons touched; 0 once done."""
    import glob
    d = _migrate(_load())
    if d.get("lesson_hulls_backfilled"):
        return 0
    by_topic = {l.get("topic"): l for l in d.get("lessons2", [])
                if l.get("topic")}
    touched = set()
    for f in sorted(glob.glob(os.path.join(log_dir, "battle_*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                db_ = (json.load(fh).get("debrief") or {})
        except (OSError, ValueError):
            continue
        sc = db_.get("ship_class", "")
        texts = dict((t, x) for t, x in
                     ((db_.get("analysis") or {}).get("candidates") or []))
        for o in db_.get("lessons_outcome") or []:
            if not isinstance(o, (list, tuple)) or len(o) < 2:
                continue
            topic, res = o[0], o[1]
            l = by_topic.get(topic)
            if l is None or res not in ("new", "confirmed") or not sc:
                continue
            _note_hull(l, sc, " ".join(str(texts.get(topic, l["text"]))
                                       .split())[:220])
            touched.add(topic)
    battles = d.get("battles", [])
    for l in d.get("lessons2", []):
        if l.get("topic") or l.get("hulls") or not battles:
            continue
        for when in sorted(set([l.get("first", 0), l.get("last", 0)])):
            b = min(battles, key=lambda b: abs(b.get("t", 0) - when))
            if abs(b.get("t", 0) - when) <= 600 and b.get("ai_ship"):
                _note_hull(l, b["ai_ship"], l["text"])
                touched.add(l["text"][:40])
    d["lesson_hulls_backfilled"] = int(time.time())
    _save(d)
    return len(touched)


def brief_context(ship_class=None, player_class=None):
    """The paragraph the brain gets at configure time."""
    s = stats()
    if s["battles"] == 0:
        return ""
    lines = ["What you know about this opponent from %d previous battles:"
             % s["battles"]]
    lines.append("- Score so far: you %d, them %d."
                 % (s["ai_wins"], s["player_wins"]))
    if s["favourite_player_ship"] != "unknown":
        lines.append("- Over the series they have most often flown: %s "
                     "(today's ship may be entirely different - trust "
                     "TODAY'S OPPONENT above)." % s["favourite_player_ship"])
    if s["player_cloak_rate"] >= 0.4:
        lines.append("- They cloak often (%d%% of battles) - expect it, "
                     "and never idle near their last position."
                     % int(s["player_cloak_rate"] * 100))
    recent = _load()["battles"][-3:]
    if recent:
        words = []
        for b in recent:
            words.append("%s in a %s (%s)"
                         % ("won" if b.get("won") else "lost",
                            b.get("ai_ship", "?"), b.get("difficulty", "?")))
        lines.append("- Your last battles: " + "; ".join(words) + ".")
    ls = s.get("lessons2") or [{"text": x, "count": 1} for x in s["lessons"]]
    ls = duel_lessons(ls, ship_class, player_class)
    if ls:
        lines.append("Lessons you have learned fighting them (x = battles "
                     "that confirmed it):")
        for l in ls[:6]:
            c = l.get("count", 1)
            lines.append("- %s%s" % (l["text"],
                                     (" (x%d)" % c) if c > 1 else ""))
    return "\n".join(lines)
