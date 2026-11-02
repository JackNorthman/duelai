"""Ally mode - Claude flies ONE wingman on the player's side against a DUELAI-spawned red force.

The design brief: DUELAI spawns the reds (race / size / era, like sparring),
one wingman, "follow your lead": focus whatever the player has targeted, and peel off to
defend him when his shields drop or someone is hammering him.

Scenario text (the ALLY MODE panel composes it; typing it works too):

    ally <red race or hull> [N] [tng|tmp|ent] [@metres] [nogods] [wing=<race or hull>]

    ally                       -> random red race, 2 ships, auto wingman
    ally borg 3                -> three Borg, auto wingman
    ally dominion 2 wing=akira -> two Dominion, Claude flies an Akira

The wingman is a normal side-1 fleet ship (ClaudeCommander DoSpawn(1) = the player's
friendly group, targeting the enemy group).  The reds are side-0 fleet ships under the
game's own AI with NO spar focus, so they fight the player and the wingman both.

Everything the wingman decides is deterministic (wingman_orders) - "follow your lead"
has to answer inside a poll, and the LLM's ~4 s per decision is too slow to cover anyone.
"""
import random
import re

from . import bridge, intel, sparring

MAX_RED = 6                 # reds per battle (engine tolerates ~14 ships in total)
DEFAULT_RED = 2
DEFAULT_SPAWN = 3000        # reds this far ahead; the wingman spawns beside the player

# wingman doctrine thresholds (percent / game ticks, ~1 tick per second)
DEFEND_SHIELDS = 40         # The player's average shields below this -> defend
DEFEND_HULL = 70            # ...or his hull below this
DEFEND_HIT_TICKS = 5        # a hit on the player this recent counts as "being hammered"
DEFEND_DROP = 12            # ...when his shields also fell this much over ~6 s
DEFEND_EXIT_SHIELDS = 60    # stand down once shields recover past this and no recent hits
SWITCH_TICKS = 6            # minimum ticks between voluntary focus switches
WING_REPAIR_HULL = 50       # wingman orders repair below this hull
WING_WITHDRAW_HULL = 25     # ...and goes defensive below this
SAY_TICKS = 8               # minimum ticks between in-game wingman callouts


def parse_spec(text):
    """'ally borg 3 tng wing=akira' -> spec dict, None if the text is not ally mode."""
    words = str(text or "").replace(":", " ").split()
    if not words or words[0].lower() not in ("ally", "wingman", "allymode"):
        return None
    spec = {"red": "", "size": DEFAULT_RED, "era": "", "spawn": 0, "gods": 1, "wing": ""}
    rest = []
    for w in (x.lower() for x in words[1:]):
        m = re.match(r"^(\d)$", w) or re.match(r"^x?(\d)$", w) or re.match(r"^(\d)v\d$", w)
        if m:
            spec["size"] = max(1, min(MAX_RED, int(m.group(1)))); continue
        if w in ("tng", "tmp", "ent"):
            spec["era"] = w; continue
        m = re.match(r"^@(\d{3,5})$", w)
        if m:
            spec["spawn"] = max(150, min(20000, int(m.group(1)))); continue
        if w in ("nogods", "classic", "fair"):
            spec["gods"] = 0; continue
        if w == "gods":
            spec["gods"] = 1; continue
        m = re.match(r"^wing=(.+)$", w)
        if m:
            spec["wing"] = m.group(1).replace("_", " "); continue
        rest.append(w)
    spec["red"] = " ".join(rest)
    return spec


def _armed_pool(pool):
    db = intel._db()
    out = []
    for cid in pool:
        d = db.get(cid) or {}
        if d.get("hclass") == "SB" or d.get("hull", 0) < 9000:
            continue
        if not (d.get("phasers") or d.get("pulses") or d.get("ammo")):
            continue
        out.append(cid)
    return out or list(pool)


def _pick_wing(spec, player_ship, red_ids):
    """(class id, why) for the wingman.

    wing=<race|hull> is honoured as typed.  Auto: a ship of the player's own race whose
    strength is closest to 80% of his - a wingman slightly lighter than the lead.
    No known player ship: match the average red strength instead."""
    db = intel._db()
    era = spec.get("era", "")
    if spec.get("wing"):
        pool, label, manual = sparring._resolve_side(spec["wing"], era, 1)
        pool = _armed_pool(pool)
        if not pool:
            return None, "no race or hull matches wing '%s'" % spec["wing"]
        if manual:
            return pool[0], "your pick"
        want = sum(intel._strength(db[c]) for c in red_ids) / max(1, len(red_ids))
        return min(pool, key=lambda c: abs(intel._strength(db[c]) - want)), \
            "%s, matched to the red force" % label
    p = intel._resolve_player_ship(player_ship) if player_ship else None
    if p:
        race = p.get("race", "")
        target = 0.8 * intel._strength(p)
        pool = [cid for cid, d in db.items() if d.get("race") == race and cid != p.get("spawn")]
        pool = [c for c in _armed_pool(pool) if db[c].get("hclass") != "SB"]
        if pool:
            best = min(pool, key=lambda c: abs(intel._strength(db[c]) - target))
            return best, "%s escort at ~80%% of your %s" % (race, p.get("label", player_ship))
    want = sum(intel._strength(db[c]) for c in red_ids) / max(1, len(red_ids))
    pool = [cid for cid, d in db.items() if d.get("race") in ("Federation", "Future Fleet")]
    pool = _armed_pool(pool)
    best = min(pool, key=lambda c: abs(intel._strength(db[c]) - want))
    return best, "Federation escort matched to the red force"


def build_scenario(spec, player_ship=""):
    """spec -> {red: [(cid, name)..], wing: (cid, name), key, ...} or an error string."""
    db = intel._db()
    era = spec.get("era", "")
    red = spec.get("red", "")
    if not red:
        own = ""
        p = intel._resolve_player_ship(player_ship) if player_ship else None
        if p:
            own = p.get("race", "").lower()
        races = [r for r in sparring.auto_races() if r.lower() != own]
        red = random.choice(races)
    pool, red_label, manual = sparring._resolve_side(red, era, spec.get("gods", 1))
    pool = _armed_pool(pool)
    if not pool:
        return "no race or hull matches red '%s'" % red
    size = spec.get("size", DEFAULT_RED)
    chosen = random.sample(pool, min(size, len(pool)))
    while len(chosen) < size:
        chosen.append(random.choice(pool))
    reds = []
    for i, cid in enumerate(chosen):
        reds.append((cid, "RED %s-%d" % (db.get(cid, {}).get("label", cid).split()[0], i + 1)))
    wcid, why = _pick_wing(spec, player_ship, chosen)
    if not wcid:
        return why
    wlabel = db.get(wcid, {}).get("label", wcid)
    wing = (wcid, "Wingman %s" % wlabel.split(" (")[0][:24])
    key = "%s-ally-%d %s" % (era or "tng", size, red_label)
    return {"red": reds, "wing": wing, "wing_why": why, "red_race": red_label,
            "manual_red": manual, "size": size, "key": key,
            "spawn": spec.get("spawn") or DEFAULT_SPAWN}


# ------------------------------------------------------------------ doctrine

def _is_red(fleet, idx):
    s = fleet.get(idx)
    return bool(s) and s.get("st") == bridge.SLOT_ALIVE and s.get("Side", 0) == 0


def wingman_orders(snap, mem, wing_slot):
    """One poll of wingman doctrine.  Returns (orders, callout-or-None).

    mem is the director's per-battle dict; this owns the "wing_*" keys in it.
    Priority: DEFEND the player when he is in trouble (whoever last hit him, else any red
    locked onto him) > FOLLOW his target > let the stock AI pick the nearest red."""
    fleet = snap.live_fleet()
    me = fleet.get(wing_slot)
    if not me:
        return [], None
    tick = snap.tick
    you = snap.player
    orders, say = [], None

    # The player's shield history (~6 s window) for "being hammered"
    hist = mem.setdefault("wing_you_sh", [])
    sh = you.get("ShAvg", 100)
    hist.append((tick, sh))
    while hist and tick - hist[0][0] > 6:
        hist.pop(0)
    drop = max(h[1] for h in hist) - sh if hist else 0
    recent_hit = you.get("HitTick", 0) and (tick - you.get("HitTick", 0) <= DEFEND_HIT_TICKS)
    hull = you.get("Hull", 100)

    attackers = [i for i, s in fleet.items() if s.get("Side", 0) == 0 and s.get("OnYou")]
    hit_by = you.get("HitByS", -1)
    mode = mem.get("wing_mode", "follow")
    in_trouble = (sh < DEFEND_SHIELDS or hull < DEFEND_HULL or
                  (recent_hit and drop >= DEFEND_DROP))
    if mode == "defend":
        calm = sh >= DEFEND_EXIT_SHIELDS and not recent_hit and hull >= DEFEND_HULL
        if calm or not (attackers or _is_red(fleet, hit_by)):
            mode = "follow"
    elif in_trouble and (attackers or _is_red(fleet, hit_by)):
        mode = "defend"

    # pick the target for this mode
    want, why = None, ""
    if mode == "defend":
        if _is_red(fleet, hit_by) and recent_hit:
            want, why = hit_by, "defend"
        elif attackers:
            want = min(attackers, key=lambda i: bridge.slot_range(fleet[i]))
            why = "defend"
    if want is None:
        yt = you.get("TgtS", -1)
        if _is_red(fleet, yt):
            want, why = yt, "follow"
    cur = mem.get("wing_focus")          # fleet slot we last ordered, None = released
    last = mem.get("wing_switch_tick", -999)
    target_gone = cur is not None and not _is_red(fleet, cur)
    urgent = (why == "defend" and mem.get("wing_mode") != "defend") or target_gone
    if want != cur and (urgent or tick - last >= SWITCH_TICKS):
        if want is None:
            orders.append(bridge.focus("none", ship=wing_slot))
        else:
            orders.append(bridge.focus(str(bridge.SPAR_TGT_BASE + want), ship=wing_slot))
        mem["wing_switch_tick"] = tick
        if target_gone and mem.get("wing_focus") is not None:
            say = "Target destroyed."
        name = fleet[want].get("Name", "") if want is not None else ""
        if why == "defend":
            say = "Breaking to cover you - engaging %s." % name if name else "Covering you."
        elif why == "follow" and not say:
            say = "On your target - %s." % name if name else None
        mem["wing_focus"] = want
    mem["wing_mode"] = mode

    # self-preservation: repair / withdraw / power
    mh = me.get("Hull", 100)
    msh = me.get("ShAvg", 100)
    if mh < WING_REPAIR_HULL and tick - mem.get("wing_repair_tick", -999) >= 60:
        orders.append(bridge.repair(ship=wing_slot))
        mem["wing_repair_tick"] = tick
    stance = "defensive" if mh < WING_WITHDRAW_HULL else "aggressive"
    if stance != mem.get("wing_stance"):
        orders.append(bridge.stance(stance, ship=wing_slot))
        if stance == "defensive":
            say = "Taking heavy damage - falling back to defensive."
        mem["wing_stance"] = stance
    power = "shields" if msh < 25 else "weapons"
    if power != mem.get("wing_power"):
        orders.append(bridge.power(power, ship=wing_slot))
        mem["wing_power"] = power

    # defend / kill / withdraw callouts always get through; routine ones are rate-limited
    urgent_say = bool(say) and (say.startswith("Breaking") or say.startswith("Target destroyed")
                                or say.startswith("Taking heavy"))
    if say and (urgent_say or tick - mem.get("wing_say_tick", -999) >= SAY_TICKS):
        mem["wing_say_tick"] = tick
    else:
        say = None
    return orders, say
