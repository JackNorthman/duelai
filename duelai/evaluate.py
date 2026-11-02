"""Post-battle evaluation: turn the raw battle track into facts.

Deterministic - no model required.  The director records one track sample
per poll (~1.5s): tick, own hull/shields/cloak/battery, player hull/cloak,
range.  analyze() reduces that to metrics, human-readable facts, and
topic-keyed lesson candidates that learning.py can deduplicate against.

A topic key names WHAT the lesson is about ("range:knife",
"cloak:no_dividend", ...) so the same insight expressed in different
words on different nights lands on the same lesson instead of a new one.
"""


RANGE_BANDS = ((0, 350, "knife (<350)"), (350, 900, "close (350-900)"),
               (900, 2000, "mid (900-2000)"), (2000, 10 ** 9, "long (2000+)"))


_TSUB_NAMES = {0: "none", 1: "hull", 2: "shields", 3: "weapons", 4: "torps",
               5: "engines", 6: "warp", 7: "sensors", 8: "power", 9: "cloak"}


def _band(rng):
    for lo, hi, name in RANGE_BANDS:
        if lo <= rng < hi:
            return name
    return "unknown"


def _band_holds(name, reach):
    """True when the hull's own guns live inside this band.

    The bands are absolute metres but "a good range" is relative to what a
    ship can hit from.  Everything a 125m flash-beam hull ever does happens
    in "knife (<350)", so a losing knife fight always concluded "fight from
    a different band" - advice it cannot take, and a logged duel is what taking
    it looks like: 137 ticks held at 500-900 in a 125m hull, 1 hull dealt."""
    if not reach:
        return False
    for lo, hi, nm in RANGE_BANDS:
        if nm == name:
            return lo <= reach < hi
    return False


def analyze(track, debrief, events=None, reach=0):
    """track: list of dicts (tick,hull,player_hull,rng,cloak,batt,worst_shield)
    events: list of (tick, kind) the director noted (cloak_on, decloak,
    repair, wave, player_cloak, player_decloak).
    Returns {"metrics": .., "facts": [..], "candidates": [(topic, text), ..]}
    """
    events = events or []
    won = str(debrief.get("result", "")).startswith("victory")
    m = {"won": won, "samples": len(track)}
    facts, cands = [], []
    if len(track) < 4:
        return {"metrics": m, "facts": ["battle too short to analyse"],
                "candidates": []}

    # ---- damage by range band (own hull lost / player hull taken)
    lost_by, dealt_by, time_by = {}, {}, {}
    for a, b in zip(track, track[1:]):
        band = _band(b.get("rng", 99999))
        dt_own = max(0, a.get("hull", 100) - b.get("hull", 100))
        dt_pl = max(0, a.get("player_hull", 100) - b.get("player_hull", 100))
        lost_by[band] = lost_by.get(band, 0) + dt_own
        dealt_by[band] = dealt_by.get(band, 0) + dt_pl
        time_by[band] = time_by.get(band, 0) + 1
    m["lost_by_band"], m["dealt_by_band"] = lost_by, dealt_by

    # ---- damage per in-reach sample, by what the flagship was told to aim
    #      at.  The track carries tsub/weap since a logged duel, and this table is what
    #      exposed "target shields" as a hold-fire order: 0.10 hull per sample
    #      against 4.5 on sensors.  Kept in every debrief so the next cause
    #      of "in range, not firing" is read off the record, not guessed.
    if reach and any("tsub" in r for r in track):
        by = {}
        for a, b in zip(track, track[1:]):
            if a.get("rng", 99999) > reach:
                continue
            k = _TSUB_NAMES.get(a.get("tsub"), str(a.get("tsub")))
            cell = by.setdefault(k, [0, 0])
            cell[0] += 1
            cell[1] += max(0, a.get("player_hull", 100)
                           - b.get("player_hull", 100))
        m["dealt_by_target"] = {k: {"in_reach": v[0], "dealt": v[1]}
                                for k, v in by.items()}
        n_in = sum(v[0] for v in by.values())
        n_dealt = sum(v[1] for v in by.values())
        weap = [r.get("weap", 100) for r in track
                if r.get("rng", 99999) <= reach]
        if n_in >= 30 and n_dealt < 0.15 * n_in and weap and min(weap) >= 60:
            facts.append("held fire: %d samples inside %dm with weapons up, "
                         "%d hull dealt (%s)"
                         % (n_in, reach, n_dealt,
                            ", ".join("%s %d/%d" % (k, v[1], v[0]) for k, v in
                                      sorted(by.items(),
                                             key=lambda kv: -kv[1][0]))))
    worst = max(lost_by.items(), key=lambda kv: kv[1]) if lost_by else None
    best = max(dealt_by.items(), key=lambda kv: kv[1]) if dealt_by else None
    if worst and worst[1] >= 20:
        net = worst[1] - dealt_by.get(worst[0], 0)
        if net > 15:
            facts.append("took most damage at %s range: -%d hull for -%d dealt"
                         % (worst[0], worst[1], dealt_by.get(worst[0], 0)))
            if _band_holds(worst[0], reach):
                cands.append(("range:contested_%s" % worst[0].split()[0],
                              "We lose the %s exchange (traded %d hull for "
                              "%d) but our guns only reach %dm, so it is the "
                              "only band we have. Do not leave it - win the "
                              "merge instead: enter on a fresh facing, and "
                              "break to rotate rather than to withdraw."
                              % (worst[0], worst[1],
                                 dealt_by.get(worst[0], 0), reach)))
            else:
                cands.append(("range:avoid_%s" % worst[0].split()[0],
                              "The %s band bleeds us: traded %d hull for %d "
                              "there. Fight from a different band."
                              % (worst[0], worst[1],
                                 dealt_by.get(worst[0], 0))))
    if best and best[1] >= 20:
        net = best[1] - lost_by.get(best[0], 0)
        if net > 15:
            facts.append("dealt most damage at %s range: -%d them for -%d us"
                         % (best[0], best[1], lost_by.get(best[0], 0)))
            cands.append(("range:prefer_%s" % best[0].split()[0],
                          "The %s band works: dealt %d for %d taken. "
                          "Steer the fight there."
                          % (best[0], best[1], lost_by.get(best[0], 0))))

    # ---- cloak dividends (ours)
    cloaks = [e for e in events if e[1] == "cloak_on"]
    decloaks = [e for e in events if e[1] == "decloak"]
    m["cloaks"] = len(cloaks)
    if cloaks:
        by_tick = {s["tick"]: s for s in track}
        ticks = sorted(by_tick)
        def nearest(t):
            c = min(ticks, key=lambda x: abs(x - t))
            return by_tick[c]
        healed = struck = 0
        for ct, _ in cloaks:
            dt = min((d for d, _ in decloaks if d > ct), default=track[-1]["tick"])
            healed += max(0, nearest(dt).get("hull", 0) - nearest(ct).get("hull", 0))
            after = [s for s in track if dt < s["tick"] <= dt + 12]
            if after:
                struck += max(0, nearest(dt).get("player_hull", 100)
                              - after[-1].get("player_hull", 100))
        m["cloak_healed"], m["cloak_alpha"] = healed, struck
        if healed >= 15:
            facts.append("cloak repaired %d hull across %d uses" % (healed, len(cloaks)))
            cands.append(("cloak:repair_works",
                          "Cloak-and-mend pays: recovered %d hull. Break off and "
                          "cloak when hurt; stay dark until repairs finish." % healed))
        if decloaks and struck >= 15:
            facts.append("decloak alphas took %d player hull" % struck)
            cands.append(("cloak:ambush_works",
                          "The decloak alpha lands: %d hull in the opening seconds. "
                          "Set it up carefully and keep using it." % struck))
        # Coming OUT is where a logged duel died: he waited cloaked, she decloaked as
        # he did, and lost 43 -> 6 in eight ticks with her shields still
        # coming up.  Score the twelve ticks after each decloak.
        pdecl = [t for t, k in events if k == "player_decloak"]
        caught = into = 0
        for dt, _ in decloaks:
            h0 = nearest(dt).get("hull", 0)
            after = [s.get("hull", 0) for s in track if dt < s["tick"] <= dt + 12]
            lost = h0 - min(after) if after else 0
            if lost >= 15:
                caught = max(caught, lost)
                if any(dt - 12 <= t <= dt + 2 for t in pdecl):
                    into = max(into, lost)
        m["decloak_caught"], m["decloak_into_alpha"] = caught, into
        if into:
            facts.append("decloaked as he decloaked and lost %d hull in 12 ticks"
                         % into)
            cands.append(("cloak:decloak_into_alpha",
                          "He waits cloaked for your decloak: coming out as he "
                          "reappeared cost %d hull in seconds, shields still "
                          "rising. Stay dark until he has fired, then strike "
                          "while his tubes reload." % into))
        elif caught:
            facts.append("a decloak cost %d hull in 12 ticks" % caught)
            cands.append(("cloak:decloak_caught",
                          "Your decloak was punished: %d hull in seconds while "
                          "the shields came back. Decloak only outside his "
                          "torpedo salvo, with the bow on him." % caught))
        if healed < 5 and struck < 5:
            facts.append("cloaked %dx for no repair or alpha dividend" % len(cloaks))
            cands.append(("cloak:no_dividend",
                          "Cloaking gained nothing (%dx, no repair, no alpha) - "
                          "cloak only with a purpose: mend or ambush." % len(cloaks)))

    # ---- when the fight was decided
    gap = [(s["tick"], s.get("hull", 100) - s.get("player_hull", 100)) for s in track]
    decided = None
    for i in range(len(gap)):
        tail = gap[i:]
        if len(tail) >= 3 and all((g < -15) == (gap[-1][1] < -15) for _, g in tail) \
                and abs(tail[0][1]) > 15:
            decided = gap[i][0]; break
    if decided is not None and track[-1]["tick"] > track[0]["tick"]:
        frac = float(decided - track[0]["tick"]) / (track[-1]["tick"] - track[0]["tick"])
        m["decided_frac"] = round(frac, 2)
        if frac < 0.35:
            facts.append("the fight was decided in the opening third")
            cands.append(("opening:decisive",
                          "The opening exchange decides these fights - enter it "
                          "with full shields and a plan, never mid-repair."))

    # ---- player habits
    p_cloaks = [e for e in events if e[1] == "player_cloak"]
    if len(p_cloaks) >= 2:
        facts.append("player cloaked %d times" % len(p_cloaks))
        cands.append(("player:cloaks_often",
                      "They cloak repeatedly - hold fire, reposition to their "
                      "predicted exit, and never idle at their last position."))
    reps = [e for e in events if e[1] == "player_repair_spike"]
    if reps:
        facts.append("player repaired mid-fight %d times" % len(reps))
        cands.append(("player:repairs",
                      "They repair mid-fight - target engines when their hull "
                      "climbs and do not give them quiet time."))
    return {"metrics": m, "facts": facts, "candidates": cands}
