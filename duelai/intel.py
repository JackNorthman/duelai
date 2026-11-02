"""Ship intelligence: real hardpoint data turned into fighting doctrine.

Draws on data/ships.json (447 hulls, extracted by QBENGINE by executing
the game's own hardpoint files).  Produces:
  - a doctrine paragraph about the AI's OWN ship (what it fights like)
  - a design-weakness paragraph about the PLAYER's hull, fuzzy-matched
    from the free-text ship description typed at setup

Fair play note: in-game, targeting a ship shows its subsystem status on
the tactical display - scanner data is fair game.  Cloak redaction in the
brief still applies on top of this.
"""

import json
import os
import re

_DB = None
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "ships.json")


def _db():
    global _DB
    if _DB is None:
        try:
            with open(DB_PATH, encoding="utf-8") as f:
                _DB = json.load(f)
        except (OSError, json.JSONDecodeError):
            _DB = {}
    return _DB


_TORPS = None
TORP_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "torpedoes.json")


def _torps():
    global _TORPS
    if _TORPS is None:
        try:
            with open(TORP_PATH, encoding="utf-8") as f:
                _TORPS = json.load(f)
        except (OSError, json.JSONDecodeError):
            _TORPS = {}
    return _TORPS


def spawn_name(class_id):
    """The string loadspacehelper.CreateShip() accepts for this hull.

    The ZZ save-compat scheme decoupled ship-script names from hardpoint
    class ids (ships/ZZVoyagerJ.py vs Hardpoints/Janeway.py); ships.json
    carries the mapping as "spawn".  Sparring round 1 failed on exactly
    this: 'spawn FAILED: Janeway'."""
    d = _db().get(class_id)
    return (d or {}).get("spawn", class_id)


def find_ship(free_text):
    """Fuzzy-match a free-text ship description to a database entry.

    Scores each hull by how many words of the description appear in its
    id or label.  Prefers later-era variants on ties (TNG over ENT)."""
    text = (free_text or "").lower()
    words = [w for w in text.replace("-", " ").replace("_", " ").split()
             if len(w) > 2 and w not in ("uss", "ikс", "iks", "irw", "the",
                                         "refit", "class")]
    if not words:
        return None, None
    best, best_score = None, 0
    for sid, s in _db().items():
        hay = (sid + " " + s.get("label", "")).lower()
        score = sum(1 for w in words if w in hay)
        if score > best_score or (score == best_score and best and
                                  score > 0 and sid.startswith("TNG")):
            best, best_score = sid, score
    if best_score == 0:
        return None, None
    return best, _db()[best]


def _facings(s):
    sh = s.get("shields") or {}
    names = {"front": "fore", "rear": "aft", "left": "port",
             "right": "starboard", "top": "dorsal", "bottom": "ventral"}
    return {names.get(k, k): v for k, v in sh.items() if v}


def ship_race(class_id):
    s = _db().get(class_id)
    return s.get("race", "") if s else ""


def player_ship_race(free_text):
    """Race of the player's hull, era prefixes normalised away."""
    _sid, s = find_ship(free_text)
    if not s:
        return ""
    race = s.get("race", "")
    for p in ("TMP ", "ENT "):
        if race.startswith(p):
            race = race[len(p):]
    return race


def _norm_race(race):
    for p in ("TMP ", "ENT "):
        if race.startswith(p):
            race = race[len(p):]
    return race


def faction_pools(class_id):
    """(escort_pool, heavy_pool) of spawnable same-race combat hulls."""
    s = _db().get(class_id)
    if not s:
        return [], []
    raw = s.get("race", "")
    if raw.startswith(("TMP ", "ENT ")):
        # era flagship: keep her battle group in the same era
        e, h = pools_for_race(raw, exact=True)
        if e or h:
            return e, h
    race = _norm_race(raw)
    if not race or race in ("Other", "Bases", "Freighter", "Monster",
                            "Godships"):
        # Stations and props carry generic races (BorgStarbase -> "Bases");
        # infer the faction from the class name instead. Longer / more
        # specific prefixes first so entf beats fed, ds9 beats nothing.
        cid = class_id.lower()
        for prefix, cand in _NAME_RACE_HINTS:
            if cid.startswith(prefix):
                race = cand
                break
        else:
            return [], []
    return pools_for_race(race)


# Station/prop hull-name prefixes -> the faction whose ships defend them.
# DS9 defends with Starfleet: the station is Cardassian-built but hers.
_NAME_RACE_HINTS = (
    ("ds9", "Federation"), ("deepspace", "Federation"),
    ("entf", "Federation"), ("entk", "Klingon"), ("entr", "Romulan"),
    ("fed", "Federation"), ("borg", "Borg"), ("breen", "Breen"),
    ("dominion", "Dominion"), ("klingon", "Klingon"),
    ("romulan", "Romulan"), ("kessok", "Kessok"),
    ("ferengi", "Ferengi"), ("card", "Cardassian"),
    ("gorn", "Gorn"), ("hydran", "Hydran"), ("isc", "ISC"),
    ("kzinti", "Kzinti"), ("lyran", "Lyran"), ("tholian", "Tholian"),
    ("pirate", "Pirate"),
)


_ERA_HINTS = (
    (r"\btmp\b|movie[- ]era|motion picture", "TMP "),
    (r"\bent(?:erprise)?[- ]era\b|\bnx[- ]era\b", "ENT "),
)


_GENERIC_RACES = ("Other", "Bases", "Freighter", "Monster", "Godships")

# Above officer, dealer's choice drafts a stronger ship than the player's
# (a logged duel: an equal-hull draft with half the broadside was a walkover).
_DIFF_EDGE = {"captain": 1.25, "kobayashi": 1.5, "armada": 1.5}

_FACTION_WORDS = ("federation", "starfleet", "klingon", "romulan", "borg",
                  "cardassian", "dominion", "breen", "ferengi", "gorn",
                  "tholian", "hydran", "kzinti", "lyran", "kessok",
                  "xindi", "vulcan", "andorian", "suliban", "pirate")

# The persona key the setup screen sends is not always the database's race
# word - "hirogen" flies Delta Quadrant hulls, "temporal" flies Future Fleet
# - and two of them name races _GENERIC_RACES keeps out of unprompted drafts.
_PERSONA_RACE = {
    "hirogen": "delta quadrant", "temporal": "future fleet",
    "omega": "omega fleet",
    "godship": "godships", "instructor": "federation",
}


def _family(d):
    """The QB submenu groups a hull with its own variants - "California"
    covers the tactical variant, "Prometheus" covers its three MVAM
    sections.  Hulls outside the menus ("N/A") stand alone as themselves."""
    fam = str(d.get("submenu", "")).strip().lower()
    if not fam or fam == "n/a":
        fam = str(d.get("class", "")).strip().lower()
    return fam


# A separated Galaxy saucer or a Prometheus MVAM section is a piece of a
# ship, not an opponent: the fairness sort loves them (a 12k Prometheus
# Saucer is the closest Fed hull to a 9k California) and fielding one as a
# flagship makes no sense.  Label-matched - it is exactly these five hulls.
_SECTION_WORDS = re.compile(r"\b(saucer|stardrive|dorsal|ventral)\b", re.I)


def _is_section(d):
    return bool(_SECTION_WORDS.search(str(d.get("label", ""))))


def _staying(d):
    """What has to be chewed through: hull plus the whole shield envelope.
    The Defiant carries 36k of shields over a 12k hull - counting the hull
    alone sees a quarter of the ship."""
    return d.get("hull", 0) + sum((d.get("shields") or {}).values())


def _warhead(d):
    """A tube reports damage 0 - the warhead lives in the projectile script
    named by the hull's `ammo` block, so the tubes have to be priced from
    torpedoes.json or a torpedo boat reads as unarmed."""
    for a in (d.get("ammo") or []):
        key = str(a.get("script", "")).rsplit(".", 1)[-1]
        t = _torps().get(key)
        if t and t.get("damage"):
            return float(t["damage"])
    return 300.0        # unknown warhead: a photon-ish placeholder


_COVER = {}


def _arc_coverage(d):
    """Share of horizontal bearings at least one beam or pulse can bear on.

    Challenger 70%, Defiant 44%, Steamrunner / California / Sovereign 100%.
    A gun that cannot be pointed does not count for much: four Challenger
    fights at captain, inside its own reach most of the time, and the guns
    that could bear were the minority (2026-09-09).  Hulls with no arc data
    read as full coverage."""
    import math
    key = d.get("class") or id(d)
    if key in _COVER:
        return _COVER[key]
    hit = [0] * 72
    seen = False
    for w in (d.get("phasers") or []) + (d.get("pulses") or []):
        f, u, aw, ah = (w.get("fwd"), w.get("up"), w.get("arc_w"),
                        w.get("arc_h"))
        if not (f and u and aw and ah):
            continue
        seen = True
        r = (f[1] * u[2] - f[2] * u[1], f[2] * u[0] - f[0] * u[2],
             f[0] * u[1] - f[1] * u[0])
        lo_w, hi_w = sorted(aw)
        lo_h, hi_h = sorted(ah)
        for i in range(72):
            a = math.radians(i * 5)
            t = (math.sin(a), math.cos(a), 0.0)
            tf = sum(t[j] * f[j] for j in range(3))
            tr = sum(t[j] * r[j] for j in range(3))
            tu = sum(t[j] * u[j] for j in range(3))
            yaw = math.atan2(tr, tf)
            pitch = math.atan2(tu, math.hypot(tf, tr))
            if lo_w <= yaw <= hi_w and lo_h <= pitch <= hi_h:
                hit[i] = 1
    cov = (sum(hit) / 72.0) if seen else 1.0
    _COVER[key] = cov
    return cov


def _weapon_weight(d):
    """Alpha strike: what one full salvo actually lands.

    This was an emitter count (beams + half a pulse + three per tube), which
    scored the California's 6x480 phasers ABOVE the Defiant's pair of 1200
    pulse cannons and drafted the Defiant as the "fair" answer to her -
    One logged duel ended 100-to-2 without the AI's hull being scratched
    (2026-09-08).  Per-emitter damage is capped at 5x the hull's own median,
    the same guard weapon_profile uses against the Vulture's 1e17 launcher.
    """
    # Main battery only: the KD7M's 5000-damage, 20-unit Mauler priced it
    # above the KD7 that carries the same beams PLUS 150 torpedoes (a logged duel).
    beams = main_battery(d)[0]
    pulses = d.get("pulses") or []
    dmgs = sorted((w.get("damage", 0) or 0) for w in beams + pulses)
    med = dmgs[len(dmgs) // 2] if dmgs else 0
    cap = (med * 5) if med else None

    def one(w):
        v = w.get("damage", 0) or 0
        return min(v, cap) if cap else v

    # Guns are discounted by how much of the sky they can be pointed at -
    # half weight on coverage, because the AI does steer to bring them to
    # bear and a nose-heavy pulse boat (Defiant, 44%) is not 44% of a ship.
    # Torpedoes are guided and take no discount.
    guns = sum(one(w) for w in beams + pulses)
    guns *= 0.5 + 0.5 * _arc_coverage(d)
    return guns + _warhead(d) * len(d.get("tubes") or [])


_VOCAB = None


def _spellfix(text):
    """Map typo'd words onto the ship vocabulary ('voygaer' -> 'voyager')."""
    global _VOCAB
    if _VOCAB is None:
        words = set()
        for sid, d in _db().items():
            for w in ([sid] + str(d.get("label", "")).split()):
                w = w.lower().strip()
                if len(w) >= 4:
                    words.add(w)
        _VOCAB = sorted(words)
    import difflib
    out, changed = [], False
    for w in str(text).split():
        wl = w.lower()
        if len(wl) >= 5 and wl not in _VOCAB:
            close = difflib.get_close_matches(wl, _VOCAB, n=1, cutoff=0.78)
            if close:
                out.append(close[0]); changed = True; continue
        out.append(w)
    return " ".join(out) if changed else ""


def _resolve_player_ship(text):
    """find_ship with an era/faction-noise retry ("TMP FED Yamato BBX"
    fuzzy-matched a 1500-hull prop until the noise words go)."""
    import re
    _sid, p = find_ship(text or "")
    if not p or p.get("hull", 0) < 3000:
        fixed = _spellfix(text)
        if fixed:
            _, rp = find_ship(fixed)
            if rp and rp.get("hull", 0) >= 3000:
                return rp

        cleaned = re.sub(r"\b(tmp|ent|tos|tng|era|movie|fed|federation|"
                         r"klingon|romulan)\b", " ", text or "",
                         flags=re.I).strip()
        if cleaned:
            _sid, retry = find_ship(cleaned)
            if retry and retry.get("hull", 0) >= 3000:
                return retry
    if not p or p.get("hull", 0) < 3000:
        # "NX" names a 2500-hull prop while the flyable hull is ENTNX -
        # try exact class ids (era prefixes included) before giving up
        q = re.sub(r"[^a-z0-9]", "", (text or "").lower())
        for cand in (q, q.replace("class", "")):
            if not cand:
                continue
            for pre in ("", "ent", "tmp"):
                for sid, d in _db().items():
                    if sid.lower() == pre + cand and \
                            d.get("hull", 0) >= 3000:
                        return d
    return p


def _strength(d):
    """One comparable number for a hull: staying power x hitting power."""
    return _staying(d) * (_weapon_weight(d) + 1.0)


# What to suggest when a solo draft cannot answer the player's ship.
_ELEVATE_TO = {
    "holodeck": "officer", "cadet": "officer",
    "officer": "captain", "captain": "kobayashi",
    "kobayashi": "armada",
}


def best_solo_strength(player_side, scenario, persona="", era_pick="",
                       no_carrier=False, difficulty=""):
    """Strength of the strongest single hull the draft rules can offer."""
    best = 0.0
    sid, _ = dealer_choice("", player_side, scenario, persona=persona,
                           era_pick=era_pick, no_carrier=no_carrier,
                           difficulty=difficulty)
    if sid:
        best = _strength(_db().get(sid, {}))
    return best


def matchup_note(class_id, player_ship_text, difficulty="", escorts=0):
    """One honest system line when the paper matchup is lopsided.

    Reports hull AND broadside (broadside alone missed the 205k-hull
    Voyager J vs a 130k CubeSP, which read as 'fair' on guns), and when a
    SOLO draft simply cannot answer the player's ship it says so and names
    the difficulty that would bring numbers instead. Advisory only - the
    user chooses whether to elevate.
    """
    d = _db().get(class_id)
    p = _resolve_player_ship(player_ship_text)
    if not d or not p:
        return ""
    wd, wp = _weapon_weight(d), _weapon_weight(p)
    hd, hp = d.get("hull", 0), p.get("hull", 0)
    if wp < 1 or hp < 1:
        return ""
    label = d.get("label", class_id)
    ratio = _strength(d) / float(_strength(p)) if _strength(p) else 1.0

    # --- outmatched 1v1, but numbers are already coming ---------------------
    if ratio < 0.75 and escorts:
        return ("matchup note: %s cannot answer your %s alone (hull %s / "
                "broadside %d against your %s / %d, about %d%% of your "
                "strength) - so it brings %d escort%s."
                % (label, p.get("label", "ship"), "{:,}".format(int(hd)),
                   round(wd), "{:,}".format(int(hp)), round(wp),
                   round(ratio * 100), escorts, "" if escorts == 1 else "s"))

    # --- the solo-draft-can't-cope case ------------------------------------
    if ratio < 0.75 and not escorts:
        nxt = _ELEVATE_TO.get(difficulty or "officer", "kobayashi")
        if nxt == "armada":
            advice = ("armada always fields a battle group, which is what "
                      "this hull needs answering with")
        else:
            advice = ("kobayashi adds escorts and counter-picks as it judges "
                      "necessary; armada always fields a battle group")
            nxt = "kobayashi"
        return ("matchup note: this is not a fair 1v1. %s brings hull %s / "
                "broadside %d against your %s / %d - about %d%% of your "
                "strength, and the strongest single hull the draft rules "
                "allow. elevate to %s for a real fight (%s)."
                % (label, "{:,}".format(int(hd)), round(wd),
                   "{:,}".format(int(hp)), round(wp), round(ratio * 100),
                   nxt, advice))

    # --- the player is the underdog ----------------------------------------
    if ratio > 1.5:
        return ("matchup note: %s outclasses you - hull %s / broadside %d "
                "against your %s / %d. good hunting."
                % (label, "{:,}".format(int(hd)), round(wd),
                   "{:,}".format(int(hp)), round(wp)))

    # --- guns-only imbalances (original behaviour) -------------------------
    if wd < 0.75 * wp:
        return ("matchup note: %s carries broadside %d against your %d - "
                "the strongest hull the draft rules allow. expect a "
                "desperate fight, not a fair one." % (label, round(wd), round(wp)))
    if wd > 1.35 * wp:
        return ("matchup note: %s outguns you (broadside %d vs %d). "
                "good hunting." % (label, round(wd), round(wp)))
    return ""


def dealer_choice(player_ship, player_side, scenario,
                  persona="", era_pick="", no_carrier=False,
                  difficulty=""):
    """Era-aware dealer's choice (an early duel's lesson: 'a 1v1 TMP era
    battle' drew the TNG ScimitarCVB carrier from the static defaults).

    era_pick is the setup screen's era capsule ('tng'/'tmp'/'ent'); when
    empty the scenario text is read for era hints. Returns (class_id,
    race) or ("", "") so the caller can fall back to the old paths.
    Honours 'not another X' exclusions, avoids mirroring the player's
    own faction, and drafts a hull comparable to theirs.
    """
    import re
    text = " ".join((player_ship or "", player_side or "",
                     scenario or "")).lower()
    # "TNG" = the mod's unprefixed baseline era
    era = {"tmp": "TMP ", "ent": "ENT ", "tng": "TNG"}.get(era_pick, None)
    if not era:
        for pat, prefix in _ERA_HINTS:
            if re.search(pat, text):
                era = prefix
                break
    if not era:
        # AUTO capsule with no era hint in the text: draft from the mod's
        # baseline (TNG) fleet rather than giving up - returning ("","")
        # here meant AUTO-era stagings ALWAYS fell back to the static
        # per-difficulty defaults (kobayashi = Neghvar, etc).
        era = "TNG"
    exclude = set()
    pship = _resolve_player_ship(player_ship)
    if pship:
        own = _norm_race(pship.get("race", "")).lower()
        if own:
            exclude.add(own)
    # the declared side IS the player's faction - never mirror it
    side_l = (player_side or "").lower()
    for fac in _FACTION_WORDS:
        if fac in side_l:
            exclude.add(fac)
    for m in re.finditer(r"not (?:another |a |an )?([a-z]{4,})", text):
        exclude.add(m.group(1))
    # the mod names the Fed side differently across eras
    if exclude & {"federation", "starfleet"}:
        exclude.update(("federation", "starfleet"))
    # An explicitly chosen persona outranks every exclusion above: asking for
    # a Federation opponent while flying Federation is asking for an honest
    # 1v1, not a licence to deal the Breen.  Without this the setup screen
    # staged "USS Fable (BreenCL)" against a California class - every Fed
    # hull had been excluded as a mirror and candidates("") swallowed it.
    want = _PERSONA_RACE.get((persona or "").lower(), (persona or "").lower())
    if want:
        exclude = set(x for x in exclude if x not in want and want not in x)

    def asks_for(pattern):
        """True when the scenario asks for this and is not refusing it -
        "not the same ship class" must never read as a mirror request."""
        for m in re.finditer(pattern, text):
            if re.search(r"\bnot\b|n't\b|\bnever\b|\bno\b|\bdifferent\b|"
                         r"\bavoid\b|\bexcept\b|\bother than\b",
                         text[max(0, m.start() - 16):m.start()]):
                continue
            return True
        return False

    # Dealer's choice is a draft, not a mirror.  The fairness sort scores the
    # player's own class at zero distance, so the moment personas started
    # drafting in-faction it handed the player his own CaliforniaT straight back
    # ("not the same ship class but something similar in class", 2026-09-08).
    # Bar the whole submenu family, or the plain California would answer the
    # tactical variant - unless a mirror match is what was actually asked for.
    mirror_ok = asks_for(r"mirror|same (?:ship|class|hull)|"
                         r"identical (?:ship|class|hull)")
    strict = asks_for(r"different (?:ship|class|hull)|another class|"
                      r"not the same")
    own_family = _family(pship) if pship and not mirror_ok else ""
    state = {"family": own_family}

    # Hulls that demonstrably cannot fight under the AI (inside their own
    # reach, weapons up, nothing landing - two logged duels, KD5M and KD7M) sit out
    # until a later duel clears them.  See learning.dud_hulls.
    try:
        from . import learning as _learning
        duds = set(_learning.dud_hulls())
    except Exception:
        duds = set()

    def candidates(race_word):
        out = []
        for sid, d in _db().items():
            if sid in duds:
                continue
            race = d.get("race", "")
            if era == "TNG":
                # Monster and Godships stay out of an unprompted draft, but
                # the setup screen can still ask for them by name
                if (race.startswith(("TMP ", "ENT ")) or not race
                        or (race in _GENERIC_RACES
                            and race.lower() != race_word)):
                    continue
                bare = race.lower()
            else:
                if not race.startswith(era):
                    continue
                bare = race[len(era):].lower()
            if any(x in bare or bare in x for x in exclude):
                continue
            if race_word and race_word not in bare:
                continue
            if state["family"] and _family(d) == state["family"]:
                continue
            if _is_section(d):
                continue
            hull = d.get("hull", 0)
            armed = (len(d.get("phasers", [])) + len(d.get("pulses", [])) +
                     len(d.get("tubes", []))) > 0
            if not armed or hull < 9000 or hull > 300000:
                continue
            if d.get("max_speed", 0) < 4.5:
                continue
            hcl = str(d.get("hclass", ""))
            # "SB" is a starbase; "SBB" is a super-battleship and IS draftable
            # (startswith wrongly excluded Axiom/Odyssey/Neghvar H/Scimitar D...)
            if hcl == "SB":
                continue
            if no_carrier and hcl.startswith("CV"):
                continue
            out.append((sid, _staying(d), _weapon_weight(d)))
        return out

    cands = candidates(want)
    if want and not cands and not era_pick:
        # several races exist in one era only (the mod ships no TNG-era Gorn):
        # a named persona is worth crossing the era line for, which is not
        # true of an unprompted draft
        base_era = era
        for alt in ("TMP ", "ENT "):
            if alt == base_era:
                continue
            era = alt
            cands = candidates(want)
            if cands:
                break
        if not cands:
            # nothing there either - hand the era back, or the retries below
            # go looking for Godships in the ENT era and find nothing
            era = base_era
    if not cands and state["family"] and not strict:
        # some pools are one hull deep (Godships is exactly WorldRazer):
        # a mirror beats no opponent at all, unless the scenario ruled it out
        state["family"] = ""
        cands = candidates(want)
    if not cands:
        # a named persona never falls back to another faction - dealing a
        # Breen cruiser and printing "USS Fable" on it is worse than letting
        # stage_plan's flagship_for_persona answer instead
        return "", ""
    edge = _DIFF_EDGE.get(difficulty, 1.0)
    t_stay = _staying(pship) * edge if pship else 0
    t_weap = _weapon_weight(pship) * edge if pship else 0
    if t_stay:
        # Fairness is staying power AND hitting power, each as a fraction of
        # the player's own number.  Summing them in raw units meant whichever
        # had the larger magnitude was the only one that counted at all.
        # ...and being UNDER-gunned costs double: the AI has to be able to
        # hurt him.  2026-09-16: the TMP Klingon pool answered a Miranda
        # (weapon 8583) with the Insurrection DD - staying matched to 20%,
        # guns 87% short - ahead of the KD7 (2x the staying, guns 22% short).
        # An armour-matched hull with an eighth of the guns is that walkover again.
        def fairness(c):
            stay = abs(c[1] - t_stay) / max(t_stay, 1.0)
            gap = (c[2] - t_weap) / max(t_weap, 1.0)
            return stay + (gap if gap >= 0 else 2.0 * -gap)
        cands.sort(key=fairness)
    else:
        cands.sort(key=lambda c: -c[1])
    sid = cands[0][0]
    return sid, _norm_race(_db()[sid].get("race", ""))


def all_races(min_pool=1):
    """[(race, hulls_in_db, pool_size)] for every race that can field a side.

    Derived from the database rather than a hand-kept list, so a new Race flag
    on a Bedrock stub reaches the UI picker, the auto-explorer and the persona
    map the moment ships.json is refreshed - which is exactly what did NOT
    happen when Omega Fleet and Future Fleet were split out on 2026-09-03.

    Era prefixes are normalised away ("TMP Gorn" -> "gorn"): a side names a
    race and chooses its era separately.  Bases and Freighter drop out on
    their own - pools_for_race filters stations and unarmed hulls, so their
    pools come back empty and they never reach a picker."""
    counts = {}
    for d in _db().values():
        raw = d.get("race", "")
        if not raw:
            continue
        r = _norm_race(raw).lower()
        counts[r] = counts.get(r, 0) + 1
    out = []
    for r in sorted(counts):
        esc, heavy = pools_for_race(r)
        n = len(set(esc + heavy))
        if n >= min_pool:
            out.append((r, counts[r], n))
    return out


def pools_for_race(race, exact=False):
    """Same-race combat pools by race name (case-insensitive).

    exact=True matches the raw race string (era prefix and all), for
    era-pure battle groups. Filters: armed, hull >= 9k, speed >= 4.5
    (excludes stations and the untuned 2.38-speed Assimilation Cube),
    hull <= 300k."""
    race_l = race.lower()
    cands = []
    for sid, d in _db().items():
        if exact:
            if d.get("race", "").lower() != race_l:
                continue
        elif _norm_race(d.get("race", "")).lower() != race_l:
            continue
        armed = (len(d.get("phasers", [])) + len(d.get("pulses", [])) +
                 len(d.get("tubes", []))) > 0
        hull = d.get("hull", 0)
        if not armed or hull < 9000 or hull > 300000:
            continue
        if d.get("max_speed", 0) < 4.5:
            continue
        if str(d.get("hclass", "")) == "SB":
            continue        # stations never arrive as reinforcements
        cands.append((sid, hull))
    cands.sort(key=lambda kv: -kv[1])
    ids = [sid for sid, _ in cands]
    heavies = ids[:5] or ids
    escorts = ids[2:8] or ids
    return escorts, heavies


AI_STYLE = {
    "FedAttack": "Federation line tactics: your ship repositions in a "
                 "tight band and holds formation discipline",
    "NonFedAttack": "close assault: your ship presses straight in and "
                    "brawls",
    "MultiAttack": "multi-target engagement: your ship spreads fire "
                   "across several contacts at once",
    "StarbaseAttack": "you are a STATION - you cannot maneuver at all; "
                      "fight with firing arcs and let your escorts move",
    "QueenSwarm": "Queen's Swarm: you sting nose-first, swirl close while you roll your "
                  "strongest shield toward the enemy and every point fires, then break away "
                  "on a new bearing and come again - under cloak when hurt or closing from range",
}


# How to FIGHT each of the game's stock compound AIs.  This is the game's
# own data turned against it: every hull in ships.json carries the AI
# script the engine assigns it ("ai"), so any stock-driven contact - the
# player's called-in allies, sparring fleets, escort waves - fights in one
# of these four known patterns.
AI_COUNTER = {
    "FedAttack": "runs Federation line tactics - disciplined banding and "
                 "torpedo runs on cooldown; it holds predictable range "
                 "bands, so punish the turn-in moments and focus fire",
    "NonFedAttack": "runs close assault - it will press straight in and "
                    "brawl; keep it at reach, make it chase through your "
                    "broadside, never let it pick the range",
    "MultiAttack": "spreads fire across several targets at once - it "
                   "rarely commits to one kill; concentrate yours, it "
                   "will lose the trade",
    "StarbaseAttack": "cannot maneuver at all - stay out of its heaviest "
                      "arcs and take it apart from a chosen bearing",
    "QueenSwarm": "swarms like a wasp - dives in, circles close for 20-30 seconds rolling fresh "
                  "shields toward you, then breaks away and returns from a new bearing, often "
                  "cloaked. Punish the swirl and chase the break: the facings it turns away are "
                  "the weak ones",
}


# Faction-level doctrine, above the per-hull AI assignment.  "style" is what
# that faction's ships do; "counter" is how to fight them.  Keyed on the
# normalised ships.json race string (lowercase).  Only factions with a real
# doctrine worth stating need an entry - everything else falls back to the
# per-hull AI_STYLE / AI_COUNTER above.
FACTION_DOCTRINE = {
    "omega fleet": {
        "style": "Omega Fleet doctrine: omega-particle powered hulls, and "
                 "the power goes into shields, not guns. Every ship carries "
                 "two to eight times its hull rating in shielding - the "
                 "Typhoon has 324k of shields over 40k of hull - so you do "
                 "not die quickly and you should fight like it. But most of "
                 "the fleet reaches only 40-100: outside the four capitals "
                 "(Vulture, Typhoon, Infernal, Nemesis at 250) your beams "
                 "are knife-range weapons. Close, soak, and stay closed. "
                 "Twelve of the twenty-one can cloak - use it to choose the "
                 "opening range, because you cannot chase what outranges "
                 "you.",
        "counter": "OMEGA FLEET DOCTRINE (how these ships fight): they are "
                   "shield tanks, not gunships. Expect two to eight times "
                   "the shielding of a normal hull of that size and do not "
                   "expect to burn one down in a single pass - bring "
                   "torpedoes and drain a facing rather than spreading "
                   "damage. Their weakness is reach: most of the fleet "
                   "cannot answer past 100, and the small Cardassian-derived "
                   "hulls (Hideki, Galor, Kimal, Legate) top out at 40-60. "
                   "Stay outside that band and they are unarmed. Only the "
                   "Vulture, Typhoon, Infernal, Nemesis and Seraphim reach "
                   "250 and can genuinely trade at distance. Many cloak, so "
                   "the engagement will open at a range of their choosing - "
                   "the answer is to break away and re-establish yours.",
    },
    "future fleet": {
        "style": "Future Fleet doctrine: 29th-century temporal hulls, and "
                 "the shielding is beyond anything contemporary - the Wells "
                 "carries 2.34 million over an 85k hull, twenty-seven times "
                 "its own displacement. Nothing in this era can meaningfully "
                 "hurt you, so fight patiently and do not manoeuvre for "
                 "advantage you do not need. Watch your reach, though: the "
                 "Wells and Enterprise K only strike at 60-70 and must be "
                 "close, while the Relativity works at 300 and Voyager J "
                 "reaches absurdly further. Three of the four cloak.",
        "counter": "FUTURE FLEET DOCTRINE (how these ships fight): you "
                   "almost certainly cannot kill one. Shields run from "
                   "450,000 to 2.34 million and hulls from 85k to 205k; a "
                   "conventional broadside is noise against that. Do not "
                   "fight a war of attrition you will lose - go for the "
                   "objective, disengage, or take the escorts and leave the "
                   "capital alone. The one real weakness is reach: the Wells "
                   "cannot answer past 60 and the Enterprise K past 70, so "
                   "distance neutralises both. The Relativity (300) and "
                   "Voyager J are the ones that can actually touch you at "
                   "range.",
    },
}


# Rick's Space Cruiser + its holograms (race "Rick and Morty", added 2026-10-03).
# Two hulls, so the race is never auto-drawn (min_pool=4) and both are god
# ships (data/config.json "god_ships"); it fights by explicit invitation.
FACTION_DOCTRINE["rick and morty"] = {
    "style": "Rick and Morty doctrine: you are a 15-metre garage saucer with "
             "a flagship's shields and the fastest engines in the game (speed "
             "20). Two shield grids per facing - a 36k outer and a 21.6k "
             "inner that recharges 540 a second - over a 36k hull. PHASE "
             "CLOAK: while cloaked nothing can hurt you and you pass through "
             "ships, so cloak to repair, never to run. Your beams reach only "
             "150 at full strength (26 km): close in, you choose the range. "
             "The blue ventral beam drains every shield facing of the target "
             "about 3% a second as well as its own damage - hold it on one "
             "ship. Portal rounds open a green portal for 6-8 s that drags "
             "everything within 60-110 into its centre and tears at what is "
             "there; fire them into a formation, not at a lone ship. Up to "
             "ten holograms fly with you at a sixth of your strength - launch "
             "them to split fire. Three Meeseeks boxes repair the ship. You "
             "have a cruiser's mass now, so a collision no longer erases you - "
             "but you are tiny and fast: do not ram, you still lose the trade.",
    "counter": "RICK AND MORTY DOCTRINE (how this thing fights): a tiny saucer "
               "with two shield grids per facing - 36k slow outer, then a "
               "21.6k inner that recharges 540 a second, so once a facing's "
               "outer grid is down you must KEEP hitting it or it is back. "
               "It has a PHASE CLOAK: while cloaked it cannot be damaged and "
               "flies straight through you; it cloaks to repair, so when it "
               "vanishes re-position and get your own shields back, do not "
               "chase. Its beams reach only 150 at full strength and it is "
               "one of the fastest hulls there is, so it will try to dictate "
               "the range - keep moving and keep it beyond 150 when you can. "
               "Holograms: up to ten copies at a sixth of its strength (6k "
               "hull) fighting as a team - decoys; the real one is the one "
               "that cloaks and whose beams hurt most. Portal rounds show up "
               "as 333/334-damage torpedoes but each opens a portal that "
               "drags everything within 60-110 toward its centre for 6-8 s "
               "and tears at ships inside - full impulse out of it, at once. "
               "The blue beam drains all six of your facings at once, so your "
               "shields fall everywhere, not just where it points. It has a "
               "cruiser's mass: a tractor beam holds it but no longer throws it.",
}


def faction_doctrine(class_id, which):
    """'style' or 'counter' blurb for the hull's faction, or ''."""
    s = _db().get(class_id)
    if not s:
        return ""
    d = FACTION_DOCTRINE.get(_norm_race(s.get("race", "")).lower())
    return d.get(which, "") if d else ""


def contact_ai_intel(foes):
    """Counter-doctrine lines for stock-AI contacts on the player's side.

    foes: snap.foes dict (F-slots).  Uses each contact's Type -> ships.json
    "ai" field - the engine's own AI assignment, employed against it."""
    lines = []
    for i, s in sorted(foes.items()):
        if not s.get("HasAI"):
            continue                      # the player themselves, or derelict
        cid = s.get("Type", "")
        d = _db().get(cid)
        if not d:
            continue
        counter = AI_COUNTER.get(d.get("ai", ""))
        if counter:
            lines.append("- %s (%s) %s."
                         % (s.get("Name", cid), d.get("label", cid), counter))
        if len(lines) >= 4:
            break
    if lines:
        return ("Known stock-AI behaviour of their support ships (from the "
                "game's own AI assignments):\n" + "\n".join(lines))
    return ""


def is_station(class_id):
    s = _db().get(class_id)
    if not s:
        return False
    return s.get("ai") == "StarbaseAttack" or s.get("max_speed", 99) < 1.0


def main_battery(s):
    """(main_beams, specials) for a ship record.

    2026-09-16, two logged duels: KD5M and KD7M list a "Mauler" FIRST in the
    hardpoint - 5000 damage, range 20, 400 s of dwell - a point-blank special
    the attack AI can never line up.  Reading it as "the beams" classed both
    hulls sustained-beam and sent them into the 1.0 band; both then dealt
    0 and 1 hull from inside their own reach for 56 and 46 samples.  (Why
    the beams never landed is NOT proven - SmartPhasers itself is inert,
    nothing under AI/ or Conditions/ reads it; both hulls also carry
    SetMaxTorpedoes(0, 0).  learning.dud_hulls benches such hulls.)
    The main battery is every beam reaching at least half the beam bank's
    MEDIAN range (count median, so one long-range outlier - the Yamato's
    1009-unit beam among 33 at 200 - cannot disqualify the rest either).
    Specials are reported, never counted towards reach, dwell or alpha."""
    beams = s.get("phasers") or []
    rngs = sorted((w.get("range") or 0) for w in beams if (w.get("range") or 0) > 0)
    if not rngs:
        return list(beams), []
    med = rngs[len(rngs) // 2]
    main, specials = [], []
    for w in beams:
        (main if (w.get("range") or 0) >= 0.5 * med else specials).append(w)
    return main, specials


def weapon_profile(class_id):
    """What this hull's guns actually ARE, from the real hardpoint numbers.

    The game's compound AIs never read any of this - their range bands are
    hardcoded constants (FedAttack 50/100/150/200/350, MultiAttack a flat 500)
    and a hull with 60-unit beams runs identical banding to one with 500-unit
    pulses.  Every "the AI is being stupid" case this project has hit turned out
    to be a hull fighting outside its own reach.  DUELAI can do better because
    ships.json carries range/damage/max_charge/discharge/recharge per emitter."""
    s = _db().get(class_id)
    if not s:
        return None
    beams, specials = main_battery(s)
    pulses = s.get("pulses") or []
    tubes = s.get("tubes") or []

    def bank(ws):
        dmg = sum(w.get("damage", 0) or 0 for w in ws)
        rng = [w.get("range") for w in ws if w.get("range")]
        return dmg, (max(rng) if rng else 0)

    b_dmg, b_rng = bank(beams)
    p_dmg, p_rng = bank(pulses)

    # Effective reach: weight EACH emitter by its own damage and its own range,
    # not the bank maximum.  Taking the bank max let one long-range outlier
    # (the Vulture has 4 pulses out at 2000 among 32 beams at 250) drag the
    # whole hull's "reach" to 1999 and would have staged it 2km out.
    # ...and clamp per-emitter damage before weighting.  The Vulture carries a
    # single pulse rated 1e17 (a delete button, not a weapon); unclamped it
    # owns 100% of the weight and reports the hull's "reach" as 1999.  Cap each
    # emitter at 5x the hull's own median so one god-weapon cannot define it.
    dmgs = sorted((w.get("damage", 0) or 0) for w in beams + pulses)
    med = dmgs[len(dmgs) // 2] if dmgs else 0
    cap = (med * 5) if med else None

    def wdmg(w):
        d = w.get("damage", 0) or 0
        return min(d, cap) if cap else d

    num = sum(wdmg(w) * (w.get("range", 0) or 0) for w in beams + pulses)
    den = sum(wdmg(w) for w in beams + pulses)
    reach = int(num / den) if den else 0
    total = den                       # alpha reported on the clamped figure

    # Beam dwell = how long one shot stays on target (charge / discharge).
    # Damage-weighted MEDIAN across the main battery, not the first beam in
    # the file (see main_battery): two 6 s assault phasers among 32 flash
    # phasers do not make the Yamato a sustained-beam hull.
    dw = []
    for w in beams:
        mc, dr = w.get("max_charge"), w.get("discharge")
        if mc and dr:
            dw.append((mc / float(dr), wdmg(w)))
    dwell = None
    if dw:
        dw.sort()
        half = sum(x[1] for x in dw) / 2.0
        acc = 0.0
        for d, wt in dw:
            acc += wt
            if acc >= half:
                dwell = d
                break

    if beams and dwell and dwell >= 1.0:
        kind = "sustained-beam"
    elif p_dmg > b_dmg:
        kind = "pulse"
    elif beams:
        kind = "flash-beam"
    elif tubes:
        kind = "torpedo"
    else:
        kind = "unarmed"

    # Where this hull wants the fight.  Sit at ~85% of effective reach: inside
    # the guns, outside the point where a bigger hull simply out-trades you.
    # Honest number - no floor.  A Crystalline Entity really does want to be
    # at ~50; flooring it at 200 would reproduce the exact mistake the game's
    # own hardcoded bands make.  The spawn-distance picker clamps separately.
    want = int(reach * 0.85) if reach else 800
    return {
        "class": class_id, "reach": reach, "beam_reach": b_rng,
        "pulse_reach": p_rng, "dwell": dwell, "kind": kind,
        "alpha": int(total), "beams": len(beams), "pulses": len(pulses),
        "tubes": len(tubes), "want_range": want,
        # Point-blank / oddball beams left out of everything above, so the
        # brief can say "ignore these" instead of planning around them.
        "specials": [(w.get("name", "?"), int(w.get("range") or 0),
                      int(w.get("damage") or 0)) for w in specials],
        # SmartPhasers ("may hold phasers for a good shot") only exists at
        # difficulty 1.0 - i.e. the aggressive/ambush/cripple stances.  It is
        # only worth reaching for on a hull whose beams actually dwell.
        "smart_phasers_worth": bool(dwell and dwell >= 1.0 and b_dmg >= p_dmg),
    }


def weapon_profile_text(class_id):
    w = weapon_profile(class_id)
    if not w:
        return ""
    bits = ["- Gun profile: %s, %d emitters, effective reach %d, alpha %d."
            % (w["kind"], w["beams"] + w["pulses"], w["reach"], w["alpha"])]
    if w["kind"] == "sustained-beam":
        bits.append("- Your beams HOLD (%.0fs of dwell). Damage comes from "
                    "staying on target, not from opening fire - close, settle, "
                    "and do not break the burst to manoeuvre."
                    % w["dwell"])
    elif w["kind"] == "flash-beam":
        bits.append("- Your beams are instantaneous flashes on a recharge "
                    "cycle. Dwell is worthless to you; firing arcs and rate "
                    "are everything.")
    elif w["kind"] == "pulse":
        bits.append("- Pulse-dominant: you hit hardest close and roughly "
                    "nose-on.")
    if w["beam_reach"] and w["pulse_reach"] and \
            abs(w["beam_reach"] - w["pulse_reach"]) > 150:
        bits.append("- Two engagement rings: pulses to %d, beams to %d. "
                    "Inside %d you fight with everything."
                    % (w["pulse_reach"], w["beam_reach"],
                       min(w["beam_reach"], w["pulse_reach"])))
    if w.get("specials"):
        bits.append("- Not counted above: %s. Point-blank specials the AI "
                    "cannot line up - never plan around them."
                    % ", ".join("%s (range %d, %d dmg)" % sp
                                for sp in w["specials"][:3]))
    bits.append("- Fight at about %d. Outside that you are firing blanks; "
                "the game's own AI will not work this out for you."
                % w["want_range"])
    return "\n".join(bits)


# Stance is the ONLY lever that switches SmartPhasers on, and the default
# stance keeps it off.  BasicAttack's g_lFlagThresholds (verified 2026-09-02 in
# the install, not assumed):
#
#     1.0  aggressive / ambush / cripple  ->  ...SmartPhasers, AggressivePulseWeapons
#     0.7  balanced / defensive           ->  ...AggressivePulseWeapons   (no SmartPhasers)
#
# AggressivePulseWeapons is on in BOTH bands, so a pulse hull gains nothing by
# leaving balanced.  SmartPhasers ("if 1 may hold phasers for a good shot")
# appears ONLY at 1.0 - so a long-dwell beam hull is being actively degraded by
# the default stance, and every spar we ran before today was fought that way.
# ...but NOT "ambush" as an automatic pick for a cloaker, which is what this
# function did for its first hour of life.  BasicAttack.py:35 reads:
#
#     if dKeywords["UseCloaking"] and pShip.GetCloakingSubsystem():
#         sAttackModule = "CloakAttackWrapper"
#
# and ClaudeCommander's dStances sets UseCloaking=1 for "ambush" AND
# "defensive".  So handing a cloak-capable hull either of those does not tune
# the attack AI - it REPLACES it with CloakAttack, which is a hide-and-strike
# behaviour, not a kill behaviour.  That is the whole reason a cloaked ship
# looks like it is only evading.  (It also overrides the hull's own
# SetAIString.)  Auto therefore stays on "aggressive" for cloakers too;
# "ambush" is a deliberate choice, not a default.
STANCE_NAMES = ("aggressive", "balanced", "defensive", "ambush", "cripple",
                "gunboat")


# NonFedAttack's close band: 100 plus a one-sided jitter of +20..+50.  A hull
# whose forward guns fall short of this floor never fights inside them.
NONFED_CLOSE_FLOOR = 120


def forward_reach(class_id):
    """Damage-weighted range of the emitters that face forward (0 if none)."""
    s = _db().get(class_id)
    if not s:
        return 0
    num = den = 0.0
    for w in (s.get("pulses") or []) + main_battery(s)[0]:
        f = w.get("fwd") or ()
        if len(f) == 3 and f[1] > 0.5 and w.get("range"):
            d = w.get("damage", 0) or 0
            num += d * w["range"]
            den += d
    return int(num / den) if den else 0


def gunboat_close(class_id):
    """The gunboat close band for a hull: 80% of its forward reach."""
    reach = forward_reach(class_id)
    return max(20, int(reach * 0.8)) if reach else 0


def gunboat_profile(class_id):
    """{close, reach, why} for a short-reach NonFedAttack hull, else None.

    2026-09-10, DefiantNX mirror: stock NonFedAttack closes to 120-150, and at
    the 0.7 band it hands that close band to TorpedoRun, which crawls at 20%
    impulse whenever the target is off the nose.  Two such hulls circled
    point-blank with each other on the beam - where four forward cannons
    (100m, +/-10 degrees) never bear - for 787 ticks without a hit.  The
    gunboat stance moves the close band inside the guns and lets the ship
    stop and pivot there (GunboatAttack + GunboatHold, game side)."""
    s = _db().get(class_id)
    if not s or s.get("ai") != "NonFedAttack":
        return None
    reach = forward_reach(class_id)
    if not reach or reach >= NONFED_CLOSE_FLOOR:
        return None
    close = gunboat_close(class_id)
    return {"close": close, "reach": reach,
            "why": "forward guns reach %d, inside NonFedAttack's %d+ close "
                   "band - gunboat: close band %d, stop and pivot"
                   % (reach, NONFED_CLOSE_FLOOR, close)}


def preferred_stance(class_id):
    """(stance, why) for a hull, derived from its own weapon profile."""
    w = weapon_profile(class_id)
    if not w:
        return "balanced", "no weapon profile for %s" % class_id
    if not w.get("smart_phasers_worth"):
        gb = gunboat_profile(class_id)
        if gb:
            return "gunboat", gb["why"]
        if w["kind"] == "pulse":
            why = ("pulse-dominant - AggressivePulseWeapons is already on at "
                   "balanced, and SmartPhasers would buy it nothing")
        elif w["kind"] == "flash-beam":
            why = ("flash-beam (%.3fs dwell) - holding fire for a good phaser "
                   "shot is worthless to it" % (w.get("dwell") or 0))
        else:
            why = "%s - no beam dwell to protect" % w["kind"]
        return "balanced", why
    s = _db().get(class_id) or {}
    why = ("%.1fs beam dwell - needs the 1.0 band so SmartPhasers lets it "
           "hold for a good shot" % (w.get("dwell") or 0))
    if s.get("cloak"):
        why = why + " (cloaked, but NOT ambush: that swaps it to CloakAttack)"
    return "aggressive", why


# Cloak doctrine, per the player's taxonomy (2026-09-02).
CLOAK_DOCTRINE = {
    "alpha": "ALPHA STRIKE - close to point-blank while dark, decloak, and "
             "dump everything into them before they can raise shields or get "
             "a firing solution. The whole value is that the engagement "
             "starts at a range and geometry YOU chose.",
    "wolfpack": "HIT AND RUN - strike, recloak, reposition, strike again from "
                "a new bearing. Never give them two attacks from the same "
                "line. A squadron doing this makes their tactical picture "
                "useless: they cannot tell three ships from twelve.",
    "break": "BREAKING CONTACT - take the damage, cloak, disengage and "
             "repair. Nobody can pursue what they cannot track.",
    "lurk": "GOING TO GROUND - crippled and silent, repairing, waiting for "
            "the hunt to move on. Do NOT hold station while you do it - a "
            "cloaked ship that stays put is a cloaked ship they will find.",
}


def cloak_profile(class_id):
    """Which cloak play this hull is built for."""
    s = _db().get(class_id)
    if not s or not s.get("cloak"):
        return None
    w = weapon_profile(class_id) or {}
    speed = s.get("max_speed", 0)
    hull = s.get("hull", 0)
    # A fast light hull can strike and vanish repeatedly; a heavy one gets
    # one good opening and should spend it at point-blank.
    if speed >= 15 and hull <= 60000:
        mode = "wolfpack"
    else:
        mode = "alpha"
    return {"mode": mode, "text": CLOAK_DOCTRINE[mode],
            "strike_at": max(150, int((w.get("reach") or 400) * 0.6)),
            "speed": speed}


def cloak_profile_text(class_id):
    c = cloak_profile(class_id)
    if not c:
        return ""
    return ("- YOU HAVE A CLOAK. Primary play: %s\n"
            "- Decloak at about %d - inside your own guns, close enough that "
            "the first volley lands before they can answer.\n"
            "- Defensive fallbacks: %s / %s"
            % (c["text"], c["strike_at"],
               CLOAK_DOCTRINE["break"].split(" - ")[0],
               CLOAK_DOCTRINE["lurk"].split(" - ")[0]))


FRONT_HEAVY_RATIO = 1.5    # fore shield vs the mean of the other five


def hull_shape(class_id):
    """Is this hull built to fight nose-on?  From its real ratings.

    Player feedback: "klingon ships in particular are loaded front heavy - it
    needs to learn that - and protect its arse".  The numbers agree: 17 of
    19 TMP Klingon and 17 of 18 TMP Romulan hulls carry a fore shield 2.5x
    every other facing, where Federation hulls sit at 1.25x.  The Fek'lhr:
    fore 15000, every other facing 6000, two of three tubes forward."""
    s = _db().get(class_id)
    sh = (s or {}).get("shields") or {}
    if not sh.get("front"):
        return None
    others = [sh.get(k, 0) or 0 for k in ("rear", "left", "right", "top",
                                          "bottom")]
    avg = sum(others) / 5.0
    ratio = sh["front"] / avg if avg else 1.0
    tubes = s.get("tubes") or []

    def ahead(t):
        d = t.get("dir") or t.get("fwd") or ()
        return d[1] if len(d) == 3 else 0

    return {"front_ratio": round(ratio, 2),
            "front_heavy": ratio >= FRONT_HEAVY_RATIO,
            "fwd_tubes": sum(1 for t in tubes if ahead(t) > 0.5),
            "aft_tubes": sum(1 for t in tubes if ahead(t) < -0.5),
            "tubes": len(tubes)}


def hull_shape_text(class_id):
    hs = hull_shape(class_id)
    if not hs or not hs["front_heavy"]:
        return ""
    tubes = ""
    if hs["tubes"] and hs["fwd_tubes"] > hs["aft_tubes"]:
        tubes = ", and %d of your %d torpedo tubes face forward" % (
            hs["fwd_tubes"], hs["tubes"])
    return ("- FRONT-LOADED HULL: your fore shield is %.1fx every other "
            "facing%s. Fight nose-on. Your aft is the soft spot. Guard your "
            "impulse engines: lose them and you cannot turn your nose back "
            "onto him - a big ship that cannot manoeuvre is taken apart. Never break away inside his reach while the hull "
            "is sound; turn a fresh facing in (guard) instead. If the "
            "impulse drive is hit, stop running - you cannot outrun him "
            "slowed - and keep the bow on him while repairs run."
            % (hs["front_ratio"], tubes))


def own_ship_doctrine(class_id):
    """What the AI's own ship fights like, from its real hardpoints."""
    s = _db().get(class_id)
    if not s:
        return ""
    lines = ["YOUR SHIP'S REAL CHARACTERISTICS (%s):" % s.get("label",
                                                              class_id)]
    style = AI_STYLE.get(s.get("ai", ""))
    if style:
        lines.append("- Native combat doctrine (from the game's own AI "
                     "assignment): %s." % style)
    n_ph, n_pu, n_tu = (len(s.get("phasers", [])), len(s.get("pulses", [])),
                        len(s.get("tubes", [])))
    lines.append("- Weapons: %d beam, %d pulse, %d torpedo tubes."
                 % (n_ph, n_pu, n_tu))
    if n_pu + n_tu > n_ph:
        lines.append("- Pulse/torpedo heavy: you hit hardest CLOSE and "
                     "roughly nose-on. Brawl, don't snipe.")
    shape = hull_shape_text(class_id)
    f = _facings(s)
    if shape:
        lines.append(shape)
    elif f:
        weak = min(f, key=f.get)
        strong = max(f, key=f.get)
        if f[strong] > 1.4 * f[weak]:
            lines.append("- Your %s shield is much weaker than your %s - "
                         "keep your %s toward them." % (weak, strong, strong))
    wp = weapon_profile_text(class_id)
    if wp:
        lines.append(wp)
    cp = cloak_profile_text(class_id)
    if cp:
        lines.append(cp)
    own = faction_doctrine(class_id, "style")
    if own:
        lines.append("- %s" % own)
    return "\n".join(lines)


def player_ship_intel(free_text):
    """Design weaknesses of the player's hull, for the system prompt."""
    sid, s = find_ship(free_text)
    if not s:
        return ""
    lines = ["INTEL ON THE ENEMY HULL (matched '%s' to %s):"
             % (free_text.strip(), s.get("label", sid))]
    f = _facings(s)
    if f:
        weak = min(f, key=f.get)
        strong = max(f, key=f.get)
        if f[strong] > 1.3 * f[weak]:
            weak_all = sorted(f, key=f.get)[:3]
            lines.append("- Shield design flaw: %s facings are ~%d%% weaker "
                         "than their %s. Work their flanks and rear."
                         % ("/".join(weak_all),
                            int(100 * (1 - f[weak] / f[strong])), strong))
        else:
            lines.append("- Shields are evenly distributed - no soft facing.")
    lines.append("- Hull rating %dk. Top speed %.1f."
                 % (int(s.get("hull", 0) / 1000), s.get("max_speed", 0)))
    n_ph, n_pu, n_tu = (len(s.get("phasers", [])), len(s.get("pulses", [])),
                        len(s.get("tubes", [])))
    lines.append("- Armament: %d beam, %d pulse, %d torpedo tubes%s."
                 % (n_ph, n_pu, n_tu,
                    " - torpedo-reliant, killing their torpedo bays "
                    "defangs them" if n_tu >= 4 and n_ph <= 6 else ""))
    if not s.get("cloak"):
        lines.append("- The stock hull has NO cloak (theirs may be refitted).")
    rp = s.get("repair_points")
    if rp:
        lines.append("- Repair rate %d/s: %s" % (
            int(rp), "they out-repair you in long fights - never let "
            "them disengage and heal" if rp >= 30 else "modest repairs - "
            "sustained damage sticks"))
    counter = faction_doctrine(sid, "counter")
    if counter:
        lines.append("- %s" % counter)
    return "\n".join(lines)


def battle_intel(own_class, player_free_text):
    parts = [own_ship_doctrine(own_class),
             player_ship_intel(player_free_text)]
    out = "\n\n".join(p for p in parts if p)
    # speed comparison if both known
    s_own = _db().get(own_class)
    _sid, s_pl = find_ship(player_free_text)
    if s_own and s_pl:
        vo, vp = s_own.get("max_speed", 0), s_pl.get("max_speed", 0)
        if vo and vp:
            if vo > vp * 1.1:
                out += ("\n\nSPEED: you are the faster ship (%.1f vs %.1f) - "
                        "YOU dictate the range. Disengage and re-engage at "
                        "will; they cannot chase you down." % (vo, vp))
            elif vp > vo * 1.1:
                out += ("\n\nSPEED: they are faster (%.1f vs %.1f) - you "
                        "cannot outrun them. Commit fully when you engage; "
                        "a half-hearted retreat just gives them your aft."
                        % (vp, vo))
    return out
