"""Difficulty profiles and the deterministic tactical doctrine.

The doctrine here is distilled from the recorded Claude-vs-player duel
history (2026-08-03..05).  The rules layer is what makes the AI dangerous;
the LLM picks among sound options and supplies personality.  Difficulty
gates BOTH: how much doctrine runs, and how fast decisions come.

Hard-won rules encoded below:
- Mass beats a single tough ship; pressure that stops is pressure wasted.
- Break at FIRST aft exposure, not after the facing is gone.
- Never loiter in a cloaked enemy's re-entry envelope; spend their cloak
  time opening range and regenerating.
- Clear a fighter screen before committing to the carrier.
- A one-faced ship (strong fore) should joust nose-on, never orbit.
- Targeting hull beats targeting weapons.
- Cloaking folds weapons power to shields+repair (game side does this).
"""

PROFILES = {
    "holodeck": {
        "label": "holodeck",
        "blurb": "target practice - spawns and wanders on stock AI",
        "brain_cadence": 0,        # LLM never issues orders
        "reflex": False,
        "opening": ["free"],       # hand straight to stock QB AI
        "allowed": [],             # brain may issue nothing
        "autobreak": None,
        "launch_fighters": 0,
        "taunt_chance": 0.15,      # still chats, just doesn't fight smart
        "telegraph": True,
    },
    "cadet": {
        "label": "cadet",
        "blurb": "gentle - passive stance, no focus fire, slow decisions",
        "brain_cadence": 30,
        "reflex": False,
        "opening": ["stance defensive", "power balanced"],
        "allowed": ["stance", "power", "taunt"],
        "autobreak": None,
        "launch_fighters": 0,
        "taunt_chance": 0.5,
        "telegraph": True,         # says what it's about to do
    },
    "officer": {
        "label": "officer",
        "blurb": "a fair fight - balanced doctrine, some reflexes",
        "brain_cadence": 15,
        "reflex": True,
        "opening": ["stance balanced", "target hull", "power balanced",
                    "focus player"],
        "allowed": ["stance", "target", "power", "cloak", "taunt",
                    "break", "circle", "guard", "focus"],
        # AUTOBREAK LADDER (hull%, weakest facing%, hull lost in the window,
        # seconds).  It reads backwards on purpose: HIGHER difficulty breaks
        # off LATER.  It used to widen with difficulty - captain and above
        # disengaged at 40% hull and a 20% facing where officer fought on to
        # 30% and 10% - so every level above officer flinched sooner, spent
        # more of the battle running, and was measurably easier.  One logged duel
        # at captain left the player on 89% hull to officer's 48% the fight before.
        # The facing trigger is OFF (-1) at every level since the autobreak fix: a facing
        # at 0-8% fired break + cloak at 100%, 95% and 68% hull, every cloak
        # aborted, and each one turned her aft to him and dropped her
        # shields for nothing - her worst facing sat at 0-11% for the last
        # 120 ticks.  A collapsed facing is guard's job (reflex 3d), as it
        # already was for the cloak playbook and the brain.  Hull and
        # big-hit triggers stay: those are real escapes.
        "autobreak": (30, -1, 15, 12),
        "launch_fighters": 3,
        "taunt_chance": 0.25,
        "telegraph": False,
    },
    "captain": {
        "label": "captain",
        "blurb": "full doctrine - fast reflexes, subsystem targeting",
        "brain_cadence": 6,
        "reflex": True,
        "opening": ["stance aggressive", "target hull", "power weapons",
                    "focus player"],
        "allowed": ["stance", "target", "power", "cloak", "taunt",
                    "break", "circle", "guard", "focus", "repair", "warp"],
        "autobreak": (25, -1, 18, 12),
        "launch_fighters": 7,
        "taunt_chance": 0.15,
        "telegraph": False,
    },
    "kobayashi": {
        "label": "kobayashi",
        "blurb": "no-win scenario - escorts, counter-picks, max cadence",
        "brain_cadence": 6,
        "reflex": True,
        "opening": ["stance aggressive", "target hull", "power weapons",
                    "focus player"],
        "allowed": ["stance", "target", "power", "cloak", "taunt",
                    "break", "circle", "guard", "focus", "repair", "warp",
                    "reinforce"],
        "autobreak": (20, -1, 20, 12),
        "launch_fighters": 7,
        "taunt_chance": 0.12,
        "telegraph": False,
        "escorts": 3,              # extra hulls spawned alongside the flag
        "fleet_cap": 10,           # max simultaneously-alive ships
        "surprise": True,          # automatic drop-in reinforcements
    },
    "armada": {
        "label": "armada",
        "blurb": "fleet action - opening line of six, relentless waves",
        "brain_cadence": 6,
        "reflex": True,
        "opening": ["stance aggressive", "target hull", "power weapons",
                    "focus player"],
        "allowed": ["stance", "target", "power", "cloak", "taunt",
                    "break", "circle", "guard", "focus", "repair", "warp",
                    "reinforce"],
        "autobreak": (20, -1, 20, 12),
        "launch_fighters": 3,      # object budget goes to warships here
        "taunt_chance": 0.10,
        "telegraph": False,
        "escorts": 6,
        "escorts_heavy": 8,        # opening line vs stations/monster hulls
        "fleet_cap": 14,
        "surprise": True,
        "wave_throttle": 20,       # ticks between waves (35 elsewhere)
        "wave_batch": 3,           # ships per wave (2 elsewhere)
        "topup_frac": 0.67,        # reinforce below 2/3 strength (1/2 else)
    },
}

DIFFICULTY_ORDER = ["holodeck", "cadet", "officer", "captain", "kobayashi",
                    "armada"]

# Default flagship per difficulty when the user picks "dealer's choice".
# Choices reflect the recorded balance findings.
DEFAULT_SHIPS = {
    "holodeck": ("Galaxy", "USS Punching Bag"),
    "cadet": ("Vorcha", "IKS Fable"),
    "officer": ("Warbird", "IRW Fable"),
    "captain": ("ScimitarCVB", "IRW Fable"),
    "kobayashi": ("Neghvar", "IKS Fable"),
    "armada": ("Neghvar", "IKS Fable"),
}

ESCORT_POOL = ["KahlessB", "Sovereign", "Vorcha"]
# Against monster hulls (Cubes, stations - 100k+) only heavies matter:
# fighters and cruisers die on contact (Borg-fight doctrine, 2026-08-05).
HEAVY_POOL = ["KahlessB", "Hutet", "Odyssey", "Sovereign"]
MONSTER_HULL = 100000.0


def duel_pure(plan):
    """True when the player asked for single combat: the flagship fights
    alone - no fighter wing, no escorts, no reinforcement waves."""
    import re
    p = plan or {}
    text = " ".join((p.get("scenario", ""), p.get("player_allies", "")))
    return bool(re.search(
        r"\b1\s*v(?:s)?\s*1\b|one[- ]on[- ]one|single combat|\bduel\b|"
        r"no (?:escorts|fighters|wingmen|reinforcements)", text, re.I))


def heavy_opponent(player_ship_text):
    """A monster hull OR a declared fleet counts as a heavy threat."""
    import re
    from . import intel
    _sid, s = intel.find_ship(player_ship_text)
    if s and s.get("hull", 0) >= MONSTER_HULL:
        return True
    text = (player_ship_text or "").lower()
    if "fleet" in text or "armada" in text:
        return True
    m = re.search(r"\b(\d{1,2})\b", text)
    return bool(m and int(m.group(1)) >= 5)


def escorts_for(profile, player_ship_text, own_class=""):
    """Opening escorts: same faction as the flagship, scaled to the
    player's threat level."""
    n = profile.get("escorts", 0)
    if not n:
        return []
    from . import intel
    heavy = heavy_opponent(player_ship_text)
    if heavy:
        # a Cube (or a fleet) laughs at four
        n = max(n, profile.get("escorts_heavy", 5))
    f_escorts, f_heavies = intel.faction_pools(own_class)
    pool = (f_heavies if heavy else f_escorts) or \
           (HEAVY_POOL if heavy else ESCORT_POOL)
    return [(pool[i % len(pool)], "") for i in range(n)]


# Scene-load governor: the 2002 engine's memory-corruption crash correlates
# with heavy multi-ship + torpedo traffic.  Hold reinforcements while hot.
# 20 (was 16): armada's 8-ship opening plus fighters tripped 16 instantly
# and starved every wave (a logged duel); the 17-ship siege ran ~25 objects
# for 900+ ticks, so 20 keeps margin under proven load.
MAX_SCENE_OBJECTS = 20
MAX_SCENE_TORPS = 50


def scene_hot(snap):
    raw = getattr(snap, "raw", {}) or {}
    return (raw.get("ObjCount", 0) > MAX_SCENE_OBJECTS or
            raw.get("TorpCount", 0) > MAX_SCENE_TORPS)


def reinforcement_wave(snap, mem, profile, flag, tick, count):
    """Build a surprise-wave order batch, respecting cap, throttle, and
    the engine's stability ceiling."""
    from . import bridge
    if scene_hot(snap):
        return []
    cap = mem.get("fleet_cap_override") or profile.get("fleet_cap", 8)
    alive = len(snap.live_fleet())
    room = cap - alive
    if room <= 0 or count <= 0:
        return []
    if tick - mem.get("last_wave_tick", -999) <= \
            profile.get("wave_throttle", 35):
        return []
    heavy = mem.get("heavy_opponent")
    pool = (mem.get("pool_heavy") if heavy else mem.get("pool_escort")) or \
           (HEAVY_POOL if heavy else ESCORT_POOL)
    orders = []
    for j in range(min(count, room, profile.get("wave_batch", 2))):
        cls = pool[(mem.get("wave_n", 0) + j) % len(pool)]
        from . import intel
        orders.append(bridge.spawn(intel.spawn_name(cls), ""))
    mem["wave_n"] = mem.get("wave_n", 0) + len(orders)
    mem["last_wave_tick"] = tick
    # New arrivals inherit the fight: proven fleet-battle pattern.  -1 is
    # every fleet slot, the flagship included, so give it its own stance
    # straight back or a wave would re-arm SmartPhasers on a flash-beam flag.
    orders.append(bridge.stance("aggressive", -1))
    orders.append(hull_stance_order("aggressive", mem.get("ship_class", ""),
                                    mem.get("flag", 0)))
    orders.append(bridge.focus("player", -1))
    return orders


# ------------------------------------------------------------------ reflexes

def reflexes(snap, mem, profile):
    """Deterministic per-poll reactions.  Returns a list of bridge.Order.

    `mem` is a mutable dict the director owns, persisting between polls:
      flag        - slot index of the flagship
      launched    - fighters launched yet?
      player_cloaked_at - tick when player cloak was first seen
      last_break_tick   - throttle for reflex breaks
      power/power_tick  - live power profile and when it was last moved
      reach             - this hull's own effective gun reach, in metres
    """
    from . import bridge

    orders = []
    if not profile["allowed"]:      # holodeck: stock AI, no interference
        return orders

    flag_slot = mem.get("flag", 0)
    flag = snap.fleet.get(flag_slot)
    if flag is None or flag["st"] != bridge.SLOT_ALIVE:
        return orders

    tick = snap.tick

    # 0. Blindness (all commanded difficulties, fairness rule): the engine's
    #    fire scripts stub target visibility to "always visible", so a
    #    sensor-dead ship would keep shooting.  Play it honestly - sensors
    #    destroyed -> disengage and hold off until repairs restore them.
    station = mem.get("is_station", False)
    sens = flag.get("Sens", 100)
    if 0 <= sens < 15:
        if not mem.get("blind"):
            mem["blind"] = True
            orders.append(bridge.focus("none", flag_slot))
            orders.append(bridge.repair(flag_slot))
        if not station and tick - mem.get("blind_break_tick", -999) > 12:
            orders.append(bridge.brk(15, flag_slot))
            set_power(orders, mem, profile, flag_slot, "shields", tick, True)
            mem["blind_break_tick"] = tick
        return orders               # blind: no other reflex may re-engage
    elif mem.get("blind"):
        if sens > 40:
            mem["blind"] = False
            orders.append(bridge.focus("player", flag_slot))
            orders.append(hull_stance_order(
                "aggressive", mem.get("ship_class", ""), flag_slot))
            set_power(orders, mem, profile, flag_slot, "weapons", tick, True)
        else:
            return orders           # partially repaired: stay disengaged

    # 0b. Kobayashi surprise reinforcements: replace losses, and answer a
    #     turning tide with friends dropping in.
    if profile.get("surprise") and not mem.get("duel_pure"):
        alive = len(snap.live_fleet())
        prev = mem.get("prev_alive", alive)
        mem["prev_alive"] = alive
        want = 0
        if alive < prev:
            want = 1
        # Sustained-pressure doctrine: never let the group thin out.
        cap = profile.get("fleet_cap", 8)
        if alive <= int(cap * profile.get("topup_frac", 0.5)):
            want = max(want, 2)
        if flag.get("Hull", 100) < 60 and not mem.get("wave_hull60"):
            mem["wave_hull60"] = 1
            want = 2
        if flag.get("Hull", 100) < 30 and not mem.get("wave_hull30"):
            mem["wave_hull30"] = 1
            want = 2
        if want:
            orders.extend(reinforcement_wave(snap, mem, profile, flag,
                                             tick, want))

    if not profile["reflex"]:
        return orders

    # 1. Launch the wing at first contact (range under 3000, once).
    max_f = profile["launch_fighters"]
    if max_f and not mem.get("launched") and not mem.get("duel_pure"):
        rng = bridge.slot_range(flag)
        if rng < 3000 and not scene_hot(snap):
            orders.append(bridge.launch(max_f, flag_slot))
            mem["launched"] = True

    # 1b. Anti-repair discipline: the learning file's oldest lesson
    #     ("target engines - they repair hull too quickly"), enforced by
    #     the rules layer. An enemy whose hull climbs 8% off its low
    #     gets their engines shot out, not a breather (a logged duel: the
    #     player repaired 85->100 mid-duel unpunished).
    #     ...but not while the cloak playbook has the ship: she cannot shoot
    #     engines cloaked, and the stance this sends is UseCloaking 0, which
    #     is exactly what decloaked her four times in a logged duel.
    if "target" in profile["allowed"] and not mem.get("cloak_phase"):
        phull = snap.raw.get("YouHull", -1)
        if phull >= 0 and \
                snap.raw.get("YouCloak", 0) != bridge.CLOAK_CLOAKED:
            low = mem.get("phull_low")
            if low is None or phull < low:
                mem["phull_low"] = phull
            elif phull >= low + 8 and \
                    tick - mem.get("antirepair_tick", -999) > 30:
                mem["antirepair_tick"] = tick
                mem["phull_low"] = phull
                orders.append(bridge.target(valid_target(mem, "engines"),
                                            flag_slot))
                orders.append(hull_stance_order(
                    "aggressive", mem.get("ship_class", ""), flag_slot))

    # 2. Player cloaks -> genuine ceasefire + open range.  The engine's
    #    fire scripts never stop an ALREADY-ENGAGED ship when its target
    #    cloaks (visibility is stubbed always-on), so beams would track him
    #    through cloak.  Releasing focus forces an AI rebuild, and a fresh
    #    target selection genuinely cannot see a cloaked ship - fairness
    #    restored.  Broadcast (-1): fighters share the same defect.
    #    ...unless she is in her own cloak.  One logged duel: his cloak at t287 and
    #    his decloak at t485 each sent her a refocus while she was dark,
    #    the AI rebuild decloaked her both times, and the second walked her
    #    out into his alpha (43 -> 6 in eight ticks).  Dark, she cannot
    #    fire, so holding the ceasefire refocus costs no fairness; it is
    #    sent the moment she is visible again if he is still cloaked, and
    #    when she comes out the cloak playbook re-commits her itself.
    player_cloak = snap.raw.get("YouCloak", 0)
    own_dark = bool(mem.get("cloak_phase")) or flag.get("Cloak", 0) in (
        bridge.CLOAK_CLOAKING, bridge.CLOAK_CLOAKED, bridge.CLOAK_DECLOAKING)
    if player_cloak == bridge.CLOAK_CLOAKED:
        if not mem.get("player_cloaked_at"):
            mem["player_cloaked_at"] = tick
            if own_dark:
                # the wing still gets its ceasefire now - only she waits
                mem["ceasefire_pending"] = True
                _gd = mem.setdefault("gate_drops", {})
                _k = "his cloak/decloak reactions held while she was dark"
                _gd[_k] = _gd.get(_k, 0) + 1
                for i in snap.live_fleet():
                    if i != flag_slot:
                        orders.append(bridge.focus("none", i))
            else:
                orders.append(bridge.focus("none", -1))
                if not station and flag.get("Rng", 99999) < 800 and \
                        _can(profile, "break"):
                    if tick - mem.get("last_break_tick", -999) > 30:
                        orders.append(bridge.brk(15, flag_slot))
                        set_power(orders, mem, profile, flag_slot, "shields",
                                  tick, True)
                        mem["last_break_tick"] = tick
        elif mem.get("ceasefire_pending") and not own_dark:
            mem["ceasefire_pending"] = False
            orders.append(bridge.focus("none", -1))
    elif player_cloak != bridge.CLOAK_CLOAKING:
        # CLOAKING after CLOAKED is him re-cloaking, not reappearing: a logged duel
        # read it as a decloak at t315 and flipped focus and power back
        # three ticks before he went dark again.
        if mem.get("player_cloaked_at"):
            mem["player_cloaked_at"] = 0
            mem["ceasefire_pending"] = False
            mem["player_decloaked_at"] = tick
            # He's back on the board - re-commit everyone.  Not her while
            # she is dark: the playbook picks the strike (see above).
            if own_dark:
                _gd = mem.setdefault("gate_drops", {})
                _k = "his cloak/decloak reactions held while she was dark"
                _gd[_k] = _gd.get(_k, 0) + 1
                if _can(profile, "focus"):
                    for i in snap.live_fleet():
                        if i != flag_slot:
                            orders.append(bridge.focus("player", i))
            else:
                if _can(profile, "focus"):
                    orders.append(bridge.focus("player", -1))
                set_power(orders, mem, profile, flag_slot, "weapons", tick,
                          True)
            mem["screen_clearing"] = False

    # 3. Cloak playbook - delegated to cloak_reflex() below.
    #    The old inline version read flag["Rng"] and flag["Closing"], neither
    #    of which exists on a slot dict (they are RngYou/RngNear), so AMBUSH
    #    could never be entered and ESCAPE always fell through to a blind
    #    "held > 25 ticks" decloak - i.e. it popped back out mid-repair.
    orders.extend(cloak_reflex(snap, mem, profile, flag, flag_slot, tick))

    # 3b. Power grid.  Runs after the cloak play so a decloak-and-strike has
    #     already forced weapons; the dwell then holds it there.
    power_reflex(orders, mem, profile, flag, flag_slot, tick)

    # 3c. Hold-fire watchdog.  Four fights this week read "in range, guns
    #     up, nothing landing", and each time the cause was an order of our
    #     own - a stance flag, then a subsystem the guns could not see.
    #     Whatever the next one is: inside our own reach, weapons up, and
    #     the player's hull not moving for 15 polls -> drop any subsystem
    #     target back to hull, once, and flag it for the channel.
    reach = mem.get("reach", 0)
    phull = snap.raw.get("YouHull", -1)
    if reach and phull >= 0 and bridge.slot_range(flag) <= reach \
            and flag.get("Weap", 100) >= 60:
        if phull < mem.get("dry_phull", 101):
            mem["dry"] = 0                       # something landed
        else:
            mem["dry"] = mem.get("dry", 0) + 1
        mem["dry_phull"] = phull
        if mem["dry"] >= 15 and flag.get("TSub", 1) not in (0, 1) \
                and _can(profile, "target"):
            orders.append(bridge.target("hull", flag_slot))
            mem["dry"] = 0
            # ...and that subsystem stays off the list for the fight.  One logged duel:
            # the plan said "sensors first", the watchdog put hull back three
            # times, and the brain re-ordered sensors each time - 61 polls
            # in reach on sensors, 6 hull dealt.  valid_target reads this.
            bad = {v: k for k, v in bridge.TARGETS.items()}.get(flag.get("TSub"))
            banned = mem.setdefault("tsub_banned", [])
            if bad and bad not in banned:
                banned.append(bad)
            mem["holdfire_note"] = ("hold-fire watchdog: 15 polls inside %dm "
                                    "with nothing landing - target back to "
                                    "hull%s" % (reach, (", %s off the list for "
                                                        "this fight" % bad)
                                                if bad else ""))
    else:
        mem["dry"] = 0

    # 3d. Fresh facing.  Every plan this week said "rotate the moment a
    #     facing drops under a third" and nothing ever ordered it: the only
    #     thing that turned a shield away was autobreak, which is a retreat.
    #     One logged duel: one facing eaten 59 -> 24 -> 10 -> 0 while she pointed it at
    #     him the whole time.  GUARD is the game's own orbit-strongest-
    #     shield-out manoeuvre; six seconds of it, while the hull is still
    #     worth protecting, and not while another manoeuvre is running.
    if _can(profile, "guard") and not mem.get("is_station"):
        worst = _worst_shield(flag)
        # never mid-cloak: a guard SetAI during the ramp aborts it (a logged duel t342)
        if (worst < 22 and flag.get("Hull", 100) > 40
                and not flag.get("Man", 0)
                and flag.get("Cloak", 0) in (bridge.CLOAK_NO_DEVICE,
                                             bridge.CLOAK_READY)
                and tick - mem.get("guard_tick", -999) > 40):
            orders.append(bridge.guard(6, flag_slot))
            mem["guard_tick"] = tick

    # 3e. Impulse drive and aft (two logged duels): see drive_reflex.
    orders.extend(drive_reflex(snap, mem, profile, flag, flag_slot, tick))

    # 4. Fighter screen doctrine: if foes (his escorts/fighters) outnumber
    #    us in close orbit, release focus so the AI clears the screen.
    if _can(profile, "focus"):
        close_foes = 0
        for s in snap.live_foes().values():
            if s.get("RngNear", 99999) < 400:
                close_foes += 1
        if close_foes >= 3 and not mem.get("screen_clearing"):
            orders.append(bridge.focus("none", flag_slot))
            mem["screen_clearing"] = True
        elif close_foes <= 1 and mem.get("screen_clearing"):
            orders.append(bridge.focus("player", flag_slot))
            mem["screen_clearing"] = False

    return orders


# ------------------------------------------------------- impulse drive
#
# two logged duels (Fek'lhr, 10-29): The player shot out her impulse engines and she sat
# dead at 101m for the last 25 ticks of a logged duel, through a break and a warp
# order, while the telemetry read Eng 100 - it reported the parent system,
# and the player shoots its child engines.  The staged ClaudeCommander fix reports
# the worst child plus EngOut/EngN and ImpMax/ImpFull (top speed x10, now /
# as built).  Readings older than that fix simply never trip this.

DRIVE_HIT_PCT = 60            # an engine at or below this = drive hit
DRIVE_MENDED_PCT = 85         # all engines back above this = mended
# Top speed is NOT a damage signal.  One logged duel: ImpMax swung 98 -> 53-63 with
# every engine at 87-91%, because it follows the power split (shields,
# cloaking); the old "under 70% of as-built" rule called three false drive
# hits and two "mended", toggling the autobreak five times.
AFT_GUARD_PCT = 45            # front-loaded hull: aft facing this low -> guard
AFT_GUARD_DRIVE = 70          # ...this low once the drive is hit
AFT_GUARD_GAP = 30            # ticks between those guards


def drive_state(flag):
    """'hit', 'mended' or '' (in between, or no reading)."""
    eng = flag.get("Eng", -1)
    out = flag.get("EngOut", 0) or 0
    if (0 <= eng <= DRIVE_HIT_PCT) or out > 0:
        return "hit"
    if eng >= DRIVE_MENDED_PCT and not out:
        return "mended"
    return ""


def drive_reflex(snap, mem, profile, flag, flag_slot, tick):
    """Protect the drive, and on a front-loaded hull, the aft.

    The player: "protect its arse - a behemoth is taken down easily when it
    can't manoeuvre".  On a drive hit: stand the in-game autobreak down (a
    break shows him the aft and goes nowhere slowed), run repairs, and keep
    the strongest facing on him with guard - which on a front-loaded hull is
    the bow and the guns.  Re-arm the autobreak once the engines are mended."""
    from . import bridge
    orders = []
    if mem.get("is_station"):
        return orders
    state = drive_state(flag)
    if state == "hit" and not mem.get("drive_hit"):
        mem["drive_hit"] = tick
        mem["drive_hits"] = mem.get("drive_hits", 0) + 1
        out, n = flag.get("EngOut", 0) or 0, flag.get("EngN", 0) or 0
        mem["drive_note"] = (
            "impulse drive hit (engines %d%%%s) - standing the autobreak "
            "down, repairs on, keeping the bow on him" % (
                flag.get("Eng", -1),
                ", %d of %d out" % (out, n) if out and n else ""))
        if profile.get("autobreak"):
            orders.append(bridge.autobreak_off(flag_slot))
        if _can(profile, "repair"):
            orders.append(bridge.repair(flag_slot))
    elif state == "mended" and mem.get("drive_hit"):
        mem["drive_hit"] = None
        mem["drive_note"] = "impulse drive mended - autobreak re-armed"
        if profile.get("autobreak"):
            orders.append(bridge.autobreak(*profile["autobreak"],
                                           ship=flag_slot))
    elif mem.get("drive_hit") and _can(profile, "repair") and \
            (tick - mem["drive_hit"]) % 20 == 0:
        orders.append(bridge.repair(flag_slot))

    shape = mem.get("hull_shape") or {}
    if not _can(profile, "guard"):
        return orders
    aft = flag.get("ShA", 100)
    limit = AFT_GUARD_DRIVE if mem.get("drive_hit") else AFT_GUARD_PCT
    if ((shape.get("front_heavy") or mem.get("drive_hit"))
            and 0 <= aft < limit
            and flag.get("Hull", 100) > 25
            and not flag.get("Man", 0)
            and flag.get("Cloak", 0) in (bridge.CLOAK_NO_DEVICE,
                                         bridge.CLOAK_READY)
            and tick - mem.get("guard_tick", -999) > AFT_GUARD_GAP):
        orders.append(bridge.guard(6, flag_slot))
        mem["guard_tick"] = tick
        mem["aft_guards"] = mem.get("aft_guards", 0) + 1
    return orders


# --------------------------------------------------------------- cloak
#
# The player's doctrine, encoded:
#   "cloak is useful for fleeing and repairing - remain cloaked until
#    repair completes then decloak - when decloaking it can do a closeup
#    surprise attack but this has to be done carefully"
#
# Three phases, tracked in mem["cloak_phase"]:
#   escape  - hurt: cloak, disengage, repair, and STAY hidden until the
#             hull and subsystems are actually mended
#   stalk   - mended (or healthy): close the range while still invisible
#   strike  - inside the ambush band and lined up: decloak and alpha
#
# Shields are down and weapons are offline for the whole cloak cycle, and
# decloaking is not instant, so "carefully" means: only pop inside a
# sane range band, never while the player is right on top of us, and
# never with the battery too flat to fight afterwards.

CLOAK_HURT_HULL = 60          # cloak to escape below this hull %
CLOAK_HURT_SHIELD = 30        # ...or below this on any one facing
CLOAK_MENDED_HULL = 85        # repairs count as done at/above this
CLOAK_MIN_BATT = 30           # decloak rather than run the battery flat
CLOAK_STRIKE_MAX = 900        # ambush band: decloak inside this...
CLOAK_STRIKE_MIN = 350        # ...but never closer than this
CLOAK_MIN_HOLD = 8            # ticks: never pop straight back out
CLOAK_ESCAPE_RANGE = 250      # mend-cloak freely beyond this...
CLOAK_WINDOW_TICKS = 8        # ...or inside it, this soon after his salvo lands
CLOAK_ABORT_RANGE = 150       # a torpedo-sized hit while cloaked this close ends it
CLOAK_ABORT_HIT = 12          # hull lost in one poll that counts as a torpedo
SPAR_STALK_MAX = 90           # sparring: a stalk that finds no band by now ends...
SPAR_STALK_BLIND = 30         # ...sooner while the enemy is cloaked too (both blind)
CLOAK_MEND_MIN_HOLD = 20      # ticks a mend stays dark: shields recharge unseen
CLOAK_JAM_ABORTS = 2          # cloak cycles in a row that never go dark...
CLOAK_JAM_TICKS = 90          # ...park the cloak this long
MEND_STALL_TICKS = 40         # a mend whose engines gain nothing this long ends
CLOAK_STALE_TICKS = 20        # a phase still set this long after the order,
                              # with the ship visible, is a play that is over
DUEL_STALK_MAX = 90           # duels: a stalk that finds no band by now ends
WOLF_REACH_TICKS = 6          # wolfpack: ticks in her own reach after a strike
                              # before she may vanish again (she has to shoot)


def _worst_shield(flag):
    vals = [flag.get(k, 100) for k in ("ShF", "ShA", "ShL", "ShR", "ShT", "ShB")]
    return min(vals) if vals else 100


def _subsystems_hurt(flag):
    """True while anything important is still being repaired."""
    if flag.get("Dmg", 0) > 0:
        return True
    for k in ("Eng", "Weap", "Sens", "Pow", "Warp", "Torp"):
        if flag.get(k, 100) < 90:
            return True
    return False


def cloak_reflex(snap, mem, profile, flag, flag_slot, tick, play=None,
                 spar=False, enemy_hidden=False):
    """Deterministic cloak handling.  Returns a list of bridge.Order.

    play - one of cloaklearn.PLAYS, or None.  Sparring always passes the
           learner's pick for the round.  A duel passes None and gets the
           learner's verdict for its hull once there are enough rounds, else
           the speed/hull heuristic below.  "off" never cloaks, "mend" only
           runs and repairs, "alpha"/"wolfpack" force that strike mode.
    spar - the enemy is the opposing fleet: a strike refocuses with spar
           focus (never the player) and there is no wing to press.
    enemy_hidden - the ship this one measures against is cloaked itself, so
           its range is unknown: never START an ambush or wolfpack pass.
    """
    from . import bridge

    orders = []
    if not _can(profile, "cloak"):
        return orders
    if mem.get("is_station"):
        return orders

    state = flag.get("Cloak", bridge.CLOAK_NO_DEVICE)
    if state == bridge.CLOAK_NO_DEVICE:
        return orders

    from . import intel
    if play is None and not spar:
        if "duel_cloak_play" not in mem:
            from . import cloaklearn
            mem["duel_cloak_play"] = cloaklearn.duel_play(
                flag.get("Type", "") or mem.get("ship_class", ""))
        play = mem["duel_cloak_play"]
    if play == "off":
        return orders
    ambush = play != "mend"
    # Which cloak play this hull is built for, and the band its own guns
    # define.  The stock 350-900 band was a guess that suited nothing: it is
    # outside a Crystalline Entity's reach entirely and well short of where a
    # DS9Wings wants to be.
    cp = intel.cloak_profile(flag.get("Type", "")) or {}
    mode = play if play in ("alpha", "wolfpack") else cp.get("mode", "alpha")
    strike_at = cp.get("strike_at", CLOAK_STRIKE_MIN)
    # ALPHA: point-blank is the whole point - no minimum standoff.
    # WOLFPACK: keep a little room so it can leave again after the pass.
    if mode == "alpha":
        strike_min, strike_max = 0, max(strike_at, 250)
    else:
        strike_min, strike_max = int(strike_at * 0.5), int(strike_at * 2.2)
    # One logged duel: wolfpack decloaked at 313m and 234m with a 174m reach, and both
    # passes ended before a shot.  Decloak only where her guns reach.
    reach = mem.get("reach") or 0
    # ...but the ambush ENTRY keeps the uncapped band.  One logged duel: the cap took
    # the entry line (1.5x the band) from 495m to 261m, so on a 397m spawn
    # she cloaked on the approach at 278m, and his opening volley caught
    # her shieldless mid-cloak: hull 100 -> 52, power plant 100 -> 17, dead
    # in 52 ticks.
    entry_band = strike_max
    if reach:
        strike_max = min(strike_max, max(int(reach), strike_min + 50))
    # A cloak that never goes dark.  One logged duel: nine cycles went CLOAKING
    # -> DECLOAKING without once reaching CLOAKED, each time another order
    # (break, target, guard) rebuilt the AI mid-ramp, and the reflex re-sent
    # "defensive, cloak, break, repair" every twelve ticks for the rest of
    # the fight.  The queue now holds those orders back (director
    # _flush_orders); if the engine eats the cloak anyway, stop asking - two
    # aborts in a row park it and the ship fights visible.
    tick_jam = mem.get("cloak_jam_until", -1)
    mem["cloak_now"] = tick              # _uncloak stamps cloak_off_tick
    prev_state = mem.get("cloak_prev_state", state)
    mem["cloak_prev_state"] = state
    if state == bridge.CLOAK_CLOAKED:
        mem["cloak_aborts"] = 0
    elif prev_state == bridge.CLOAK_CLOAKING and state in (
            bridge.CLOAK_DECLOAKING, bridge.CLOAK_READY):
        mem["cloak_aborts"] = mem.get("cloak_aborts", 0) + 1
        if mem["cloak_aborts"] >= CLOAK_JAM_ABORTS:
            mem["cloak_aborts"] = 0
            mem["cloak_jam_until"] = tick + CLOAK_JAM_TICKS
            mem["cloak_jams"] = mem.get("cloak_jams", 0) + 1
            mem["cloak_phase"] = None
            mem["cloak_jam_note"] = (
                "cloak jammed: %d cycles in a row never went dark - cloak "
                "parked for %d ticks, fighting visible"
                % (CLOAK_JAM_ABORTS, CLOAK_JAM_TICKS))
            # hand the hull its fight back: the escape left it defensive
            if _can(profile, "stance"):
                orders.append(_restore_stance(mem, flag_slot))
            if not spar and _can(profile, "focus"):
                orders.append(bridge.focus("player", flag_slot))
            return orders
    # Mid-transition: let it finish, never interrupt a cloak/decloak cycle.
    if state in (bridge.CLOAK_CLOAKING, bridge.CLOAK_DECLOAKING):
        return orders

    hull = flag.get("Hull", 100)
    shield = _worst_shield(flag)
    batt = flag.get("Batt", 100)
    # A fleet slot carries Rng and Closing; RngYou belongs to foe slots and
    # never resolved here, so this ran on a constant 99999 with closing 0.
    rng = bridge.slot_range(flag)
    prev_rng = mem.get("cloak_prev_rng", rng)
    mem["cloak_prev_rng"] = rng
    closing = bridge.slot_closing(flag, prev_rng - rng)
    phase = mem.get("cloak_phase")
    cloaked = state == bridge.CLOAK_CLOAKED

    # ---------------------------------------------------------- cloaked
    if cloaked:
        if not phase and tick - mem.get("cloak_off_tick", -999) > 6:
            # Dark with no play: a cloak nobody here entered (the brain
            # before that duel's fix, the game's own AI).  With no phase it fell
            # through every branch below and was never brought out - a logged duel
            # sat cloaked on top of him from tick 102 to 166.  Adopt it on a
            # fresh clock; a collapsed facing gets the mend hold to recharge.
            # One logged duel: adopted at 68% hull a tick after his alpha took her
            # engines to 56% - as a stalk, so she waited for a band she
            # could not reach.  A hit drive is a mend.
            phase = ("escape" if hull < CLOAK_HURT_HULL
                     or drive_state(flag) == "hit" else "stalk")
            mem["cloak_phase"] = phase
            mem["own_cloaked_at"] = tick
            mem["cloak_hull_at"] = hull
            mem["cloak_hold_min"] = (CLOAK_MEND_MIN_HOLD
                                     if shield < CLOAK_HURT_SHIELD
                                     else CLOAK_MIN_HOLD)
            drops = mem.setdefault("gate_drops", {})
            k = "unclaimed cloaks taken over by the playbook"
            drops[k] = drops.get(k, 0) + 1
        held = tick - mem.get("own_cloaked_at", tick)
        # Battery is the hard stop - being caught decloaking with no power
        # to fight is worse than staying hurt.
        if batt < CLOAK_MIN_BATT:
            mem["cloak_phase"] = None
            _uncloak(orders, mem, profile, flag_slot, spar,
                     cancel_break=False, focus=False)
            return orders

        prev_hull = mem.get("cloak_prev_hull", hull)
        mem["cloak_prev_hull"] = hull
        if phase == "escape" and rng < CLOAK_ABORT_RANGE and \
                prev_hull - hull >= CLOAK_ABORT_HIT:
            # A torpedo just found her cloaked and close - phaser scratches
            # are the price of a mend, a torpedo hit means he has the range
            # and the next one is loading.  Back into the fight, shields up.
            mem["cloak_phase"] = None
            _uncloak(orders, mem, profile, flag_slot, spar, focus=False)
            return orders
        if phase == "escape":
            # The hold: shields recharge while dark but read 0 through the
            # cloak, so a mend entered for a collapsed facing used to count
            # as finished the moment it went dark - 09-10, a Defiant at 98%
            # hull cloaked and decloaked inside four ticks.
            still_hurt = ((hull < CLOAK_MENDED_HULL) or _subsystems_hurt(flag)
                          or held < CLOAK_MEND_MIN_HOLD)
            # Hide as long as the mend is working - that is the game (the player,
            # 10-29).  But if the drive is down and the bay is getting
            # nowhere with it, the hide only buys HIM repair time: a logged duel sat
            # dark 147 ticks while he went 89 -> 100.  No engine gain in
            # MEND_STALL_TICKS -> come out and fight.
            eng = flag.get("Eng", -1)
            if mem.get("mend_since") != mem.get("own_cloaked_at"):
                mem["mend_since"] = mem.get("own_cloaked_at")
                mem["mend_eng_best"], mem["mend_eng_tick"] = eng, tick
            elif eng > mem.get("mend_eng_best", -1):
                mem["mend_eng_best"], mem["mend_eng_tick"] = eng, tick
            if (still_hurt and drive_state(flag) == "hit"
                    and held >= CLOAK_MEND_MIN_HOLD
                    and tick - mem.get("mend_eng_tick", tick)
                    > MEND_STALL_TICKS):
                mem["cloak_phase"] = None
                mem["mend_stalls"] = mem.get("mend_stalls", 0) + 1
                mem["drive_note"] = (
                    "mend stalled: engines stuck at %d%% for %d ticks - "
                    "coming out to fight" % (eng, MEND_STALL_TICKS))
                _uncloak(orders, mem, profile, flag_slot, spar)
                return orders
            if still_hurt:
                # Keep mending.  Repair orders are idempotent; only nudge
                # occasionally so we do not spam the bridge.
                if _can(profile, "repair") and held % 10 == 0:
                    orders.append(bridge.repair(flag_slot))
                # Going to ground is not the same as sitting still: a cloaked
                # ship that holds station is a cloaked ship they will find.
                # Re-break every ~20 ticks so it drifts to fresh space.
                if _can(profile, "break") and held and held % 20 == 0:
                    orders.append(bridge.brk(12, flag_slot))
                return orders
            if not ambush:
                # The mend play: repairs done, straight back into the fight
                # from wherever the break left her - no stalk, no ambush.
                mem["cloak_phase"] = None
                _uncloak(orders, mem, profile, flag_slot, spar)
                return orders
            # Mended - graduate to stalking rather than popping on the spot.
            mem["cloak_phase"] = "stalk"
            phase = "stalk"
            if spar:
                mem["own_cloaked_at"] = tick    # the stalk clock starts now

        if phase == "stalk":
            if spar and held > (SPAR_STALK_BLIND if enemy_hidden
                                else SPAR_STALK_MAX):
                # Sparring only.  A stalk that has not found its band by now
                # will not: two cloaked mirrors can lose each other for the
                # rest of the round.  Come out, fight, and stay out a while.
                mem["cloak_phase"] = None
                mem["cloak_abort_at"] = tick
                mem["stalk_aborts"] = mem.get("stalk_aborts", 0) + 1
                _uncloak(orders, mem, profile, flag_slot, spar,
                         cancel_break=False)
                return orders
            if not spar and held > DUEL_STALK_MAX:
                # One logged duel: 180 ticks dark at 14-38m from him, every break to
                # open the range held, while he repaired 87 -> 100.  A stalk
                # that has not found its band by now will not - come out.
                mem["cloak_phase"] = None
                mem["stalk_aborts"] = mem.get("stalk_aborts", 0) + 1
                drops = mem.setdefault("gate_drops", {})
                k = "stalks timed out (no band in %d ticks)" % DUEL_STALK_MAX
                drops[k] = drops.get(k, 0) + 1
                _uncloak(orders, mem, profile, flag_slot, spar)
                return orders
            if held < mem.get("cloak_hold_min", CLOAK_MIN_HOLD):
                return orders
            # Careful decloak: inside the band, not on top of them, and
            # only while actually closing (a receding target means the
            # strike would land on empty space).
            too_close = rng < strike_min
            in_band = strike_min <= rng <= strike_max
            if too_close:
                # Overshot - open the range and try the approach again.
                if _can(profile, "break"):
                    orders.append(bridge.brk(10, flag_slot))
                return orders
            if in_band and closing >= 0:
                mem["cloak_phase"] = None
                mem["cloak_struck_at"] = tick
                mem["cloak_mode_used"] = mode
                mem["strikes"] = mem.get("strikes", 0) + 1
                _uncloak(orders, mem, profile, flag_slot, spar)  # cancels any flee
                set_power(orders, mem, profile, flag_slot, "weapons",
                          tick, True)
                return orders
            return orders
        return orders

    # -------------------------------------------------------- uncloaked
    # Enter the cloak only for the two plays that pay: run-and-mend, or
    # set up an ambush from outside weapons range.
    if state != bridge.CLOAK_READY:
        return orders
    if phase and tick - mem.get("own_cloaked_at", tick) > CLOAK_STALE_TICKS:
        # Visible, but the play is still on the books.  One logged duel: a stalk
        # entered at tick 175 never cleared when she came out at 184, so
        # the brain gate dropped every manoeuvre for the rest of the fight
        # and her next cloak (251) was "struck" one tick after it went dark.
        mem["cloak_phase"] = None
        mem.pop("cloak_hold_min", None)
        phase = None
        drops = mem.setdefault("gate_drops", {})
        k = "stale cloak plays cleared"
        drops[k] = drops.get(k, 0) + 1
    if tick < tick_jam:
        return orders                     # parked after a jam (see above)
    hurt = (hull < CLOAK_HURT_HULL) or (shield < CLOAK_HURT_SHIELD)
    # One logged duel: the cloak finally held - twenty ticks - at 37 to 105
    # metres with him sitting on top of her.  A cloaked ship has no shields;
    # her hull went 79 -> 49 -> 23 while "hidden", she dealt 16.  A Vmelak
    # cannot outrun a California (8.45 to 8.99), so a close-range cloak never
    # breaks contact.  The mend needs range, and a hull wound: a collapsed
    # facing at full hull is a job for guard, not the cloak.
    # ...and that holds at any range.  This used to apply only inside 250m,
    # so in a logged duel a Fek'lhr at 88% hull with one facing at 7% - 270 metres
    # out - cloaked and ran, and spent the next 200 ticks running.  A
    # shield-only wound is guard's job (reflex 3d rolls a fresh facing in).
    if hurt and hull >= CLOAK_HURT_HULL:
        return orders                     # shields only: fight, rotate a facing
    # The player's doctrine, not mine: the cloak is not only about outrunning the
    # gun.  Phasers barely scratch a cloaking ship - torpedoes kill it - so
    # the other sweet spot is the moment his salvo has just resolved and his
    # tubes are reloading.  TorpCount is the scene's torpedoes in flight: a
    # spike that has just fallen to zero is that moment.
    torps = snap.raw.get("TorpCount", 0)
    if torps > 0:
        mem["torp_seen_tick"] = tick
    window = (torps == 0 and
              0 < tick - mem.get("torp_seen_tick", -999) <= CLOAK_WINDOW_TICKS)
    if hurt and rng < CLOAK_ESCAPE_RANGE and not window \
            and batt >= CLOAK_MIN_BATT:
        # close, and no reload window yet: open range, keep watching for it
        if _can(profile, "break") and \
                tick - mem.get("cloak_break_tick", -999) > 20:
            mem["cloak_break_tick"] = tick
            orders.append(bridge.brk(10, flag_slot))
        return orders
    if hurt and batt >= CLOAK_MIN_BATT:
        # One logged duel (Vmelak): this fired fourteen times in 94 ticks and the
        # cloak went READY -> CLOAKING -> DECLOAKING -> READY every time,
        # reaching CLOAKED once.  Two faults.  The engine answers a cloak
        # order over several ticks, and the reflex re-sent it on the next
        # poll; and balanced and aggressive both carry UseCloaking 0, so the
        # game's own AI decloaked her to shoot each time we cloaked her to
        # mend.  Stance and cloak have to agree: mend under defensive
        # (UseCloaking 1, SmartShields), and the strike hands the hull its
        # own stance back.
        if tick - mem.get("cloak_ordered_tick", -999) < 12:
            return orders
        mem["cloak_ordered_tick"] = tick
        mem["cloak_phase"] = "escape"
        mem["own_cloaked_at"] = tick
        mem["cloak_hull_at"] = hull
        if _can(profile, "stance"):
            orders.append(bridge.stance("defensive", flag_slot))
        orders.append(bridge.cloak(1, flag_slot))
        if not spar:
            _wing_pressure(snap, orders, flag_slot, profile)
        if _can(profile, "break"):
            orders.append(bridge.brk(12, flag_slot))
        if _can(profile, "repair"):
            orders.append(bridge.repair(flag_slot))
        return orders
    # WOLFPACK re-entry: a fast light hull does not stay out after a pass.
    # Strike, vanish, come back on a different bearing - they cannot tell
    # three ships from twelve.  Heavier "alpha" hulls get one good opening
    # and stay committed, so this only applies to the wolfpack profile.
    since_strike = tick - mem.get("cloak_struck_at", -999)
    if mem.get("wolf_strike") != mem.get("cloak_struck_at"):
        mem["wolf_strike"] = mem.get("cloak_struck_at")
        mem["wolf_reach_ticks"] = 0
    if reach and rng <= reach:
        mem["wolf_reach_ticks"] = mem.get("wolf_reach_ticks", 0) + 1
    # One logged duel: the second re-entry began at 148m with him closing; a cloaking
    # ship has no shields and he alpha'd her 73 -> 0.  Vanish only once she
    # has fought the pass, and only from outside his guns or in the reload
    # window - the same rule the mend keeps.
    wolf_ok = ((not reach or mem.get("wolf_reach_ticks", 0)
                >= WOLF_REACH_TICKS)
               and (rng >= CLOAK_ESCAPE_RANGE or window))
    # a sparring stalk that timed out earns a minute in the open first
    fresh_abort = tick - mem.get("cloak_abort_at", -999) <= 60
    if (ambush and not enemy_hidden and not fresh_abort
            and mode == "wolfpack" and not hurt and wolf_ok
            and batt >= 50 and 12 <= since_strike <= 60):
        mem["cloak_phase"] = "stalk"
        mem["own_cloaked_at"] = tick
        mem["cloak_struck_at"] = -999
        orders.append(bridge.cloak(1, flag_slot))
        if not spar:
            _wing_pressure(snap, orders, flag_slot, profile)
        if _can(profile, "break"):
            orders.append(bridge.brk(8, flag_slot))   # new bearing
        return orders

    # Healthy ambush setup: only from beyond the strike band, so there is
    # room to creep in while invisible.
    if (ambush and not enemy_hidden and not fresh_abort and not hurt
            and rng > entry_band * 1.5
            and batt >= 60 and since_strike > 60):
        mem["cloak_phase"] = "stalk"
        mem["own_cloaked_at"] = tick
        orders.append(bridge.cloak(1, flag_slot))
        if not spar:
            _wing_pressure(snap, orders, flag_slot, profile)
        return orders
    return orders


def _wing_pressure(snap, orders, flag_slot, profile):
    """While the flagship is dark, the wing keeps the pressure on -
    pressure that stops is pressure wasted (old playbook, kept)."""
    from . import bridge
    for i, s in snap.live_fleet().items():
        if i != flag_slot:
            if _can(profile, "stance"):
                orders.append(hull_stance_order(
                    "aggressive", s.get("Type", ""), i))
            if _can(profile, "focus"):
                orders.append(bridge.focus("player", i))


def _can(profile, verb):
    return verb in profile["allowed"]


# ------------------------------------------------------------ targeting
def valid_target(mem, want):
    """A subsystem target the FireScript can actually service on THIS enemy.

    "shields" and "power" are never valid: a shield FACING is chosen by the
    approach, but the keyword aims the guns at the shield generator - like
    the warp core, a part buried inside the hull - and the AI holds its
    beams for a line to it that it almost never gets.  One logged duel: 90
    in-reach samples on "shields" dealt 9 hull, 2 on "sensors" dealt 9.

    Engines, weapons and sensors are exposed parts and stay valid whenever
    the enemy hull HAS them.  Presence, not the database's targetable flag:
    the extractor records only the group node, and Orion hardpoints mark
    every group untargetable while its parts are not - "Impulse Engines"
    reads 0 with PortImpulse and StarImpulse both 1.  Trusting the flag
    turned "target engines" into "target sensors" against a California.
    """
    if want in ("hull", "none", "", None):
        return want or "hull"
    if want in ("shields", "power"):
        return "hull"
    if want in mem.get("tsub_banned", ()):
        return "hull"           # the hold-fire watchdog caught it landing nothing
    have = mem.get("player_kinds")
    if have is None:            # unknown enemy: only hull is safe
        return "hull"
    return want if want in have else "hull"


# --------------------------------------------------------------- power grid
# ClaudeCommander.DoPower installs four profiles, and they are real trades:
#   weapons   guns 1.25  torps 1.25 / shields 0.85 engines 0.80
#   shields   shields 1.25          / guns 0.85    engines 0.85
#   engines   engines 1.25 warp 1.10 / guns 0.85   shields 0.90
#   balanced  everything 1.00
POWER_DWELL = 10        # ticks a setting holds before the reflex may move it
BRAIN_POWER_HOLD = 30   # ...and a setting the brain chose holds this long


def set_power(orders, mem, profile, flag_slot, want, tick, force=False):
    """The one gate every power change goes through.

    Nothing else may call bridge.power(): the blind and cloak handlers each
    used to issue their own without knowing what the other had just set, and
    the reflex below would have spent the whole battle arguing with them.
    Emits only on a real change.  `force` is for the urgent handlers - they
    may preempt the dwell, but still never re-send a setting already live.
    """
    from . import bridge
    if not _can(profile, "power") or mem.get("power") == want:
        return False
    if not force and tick - mem.get("power_tick", -9999) < POWER_DWELL:
        return False
    # One owner at a time.  One logged duel: the brain set shields six times and the
    # reflex put balanced/weapons back on each dwell expiry - ten flips in
    # 144 ticks, every one a ramp that never arrived.  The brain's pick now
    # holds 30 ticks; only the urgent handlers (force) may cut in.
    if not force and mem.get("power_by") == "brain" and \
            tick - mem.get("power_tick", -9999) < BRAIN_POWER_HOLD:
        return False
    mem["power"] = want
    mem["power_tick"] = tick
    mem["power_by"] = "reflex"
    orders.append(bridge.power(want, flag_slot))
    return True


def power_reflex(orders, mem, profile, flag, flag_slot, tick):
    """Fight the power grid the way a bridge crew would.

    Power used to move only on a cloak transition or a sensor kill, so a
    straight uncloaked slugfest - which is most of them - was fought on the
    profile's opening setting from the first tick to the last (the player, after
    a logged duel: "can it not also use power macros").  The inputs this needs
    are range and closing, which only started reporting truthfully when the
    RngYou/Rng key mix-up was fixed the same evening.
    """
    from . import bridge
    hull = flag.get("Hull", 100)
    shield = _worst_shield(flag)
    batt = flag.get("Batt", 100)
    rng = bridge.slot_range(flag)
    reach = mem.get("reach", 0)
    cur = mem.get("power", "")

    # The game breaks off on its own - AutoBreakOne - and forces power to
    # shields on its way out, without telling this side.  `AbFires` counts
    # those breaks, so a bump means shields is what is actually installed
    # whatever we last sent.  Resync, or the gate believes weapons is still
    # live and never restores it: a logged duel was fought at captain, which
    # autobreaks far more readily than officer, and she spent it stuck on
    # shield power - 13 hull dealt in 166 ticks, battery never below 96.
    fires = flag.get("AbFires", 0)
    if fires != mem.get("ab_fires", fires):
        cur = mem["power"] = "shields"
        mem["power_tick"] = tick
    mem["ab_fires"] = fires

    # Hysteresis: the threshold to LEAVE a setting sits clear of the one to
    # enter it.  A facing regenerating either side of a single number had the
    # grid flipping on every dwell expiry - four changes in 35 ticks - which
    # spends the whole battle ramping and never arrives anywhere.
    hurt = (shield < (40 if cur == "shields" else 25)
            or hull < (50 if cur == "shields" else 45))
    healthy = shield > (45 if cur == "weapons" else 55)

    if hull < 25 or tick - mem.get("last_break_tick", -9999) < 15:
        # running, or nearly dead: everything into leaving
        want = "engines"
    elif hurt:
        # a facing is collapsing and the hull behind it is thin
        want = "shields"
    elif reach and rng > reach * 3:
        # outside my own reach - guns are dead weight until I close
        want = "engines"
    elif reach and rng <= reach * 1.5 and healthy and batt > 25:
        # in the band, healthy, reserve to spend: put it in the guns
        want = "weapons"
    else:
        want = "balanced"
    return set_power(orders, mem, profile, flag_slot, want, tick)


# ------------------------------------------------------------- opening batch

# The difficulty profile's stance IS the difficulty ladder: cadet/officer sit
# in the 0.7 flag band, captain and above in 1.0.  So the hull is allowed to
# pick the stance that suits its guns, but NOT to change bands - otherwise an
# officer-level long-dwell beam ship would silently promote itself to captain
# difficulty, and a captain-level pulse boat would demote itself.  In practice
# this swaps aggressive -> ambush for a cloaked beam hull at captain and above,
# which is the decloak-and-hold opening rather than a straight charge.
_STANCE_BAND = {"aggressive": 1.0, "ambush": 1.0, "cripple": 1.0,
                "balanced": 0.7, "defensive": 0.7, "gunboat": 0.7}


def hull_stance(profile_stance, ship_class):
    """The hull's preferred stance, capped at the profile's difficulty band.

    The cap only stops PROMOTION: an officer-level beam ship must not quietly
    give itself the 1.0 band.  It used to block demotion as well, and that
    is how captain went soft.  A flash-beam hull prefers balanced because the
    1.0 band is the only tier where BasicAttack turns SmartPhasers on - "may
    hold phasers for a good shot" - and a 0.005s-dwell beam holds for a shot
    it never judges good enough.  Three captain fights in a Challenger:
    inside its own reach for 89 of 100 samples, battery never
    below 96, 13 / 1 / 5 hull dealt.  Officer sent balanced and fought.
    The 0.7 tier is the 1.0 tier minus that single flag, so a hull that
    takes it gives up nothing else.
    """
    from . import intel
    try:
        pick = intel.preferred_stance(ship_class)[0]
    except Exception:
        return profile_stance
    if _STANCE_BAND.get(pick, 0.7) <= _STANCE_BAND.get(profile_stance, 0.7):
        return pick
    return profile_stance


def stance_order(name, ship_class, slot):
    """bridge.stance for one slot, carrying the close band when the stance is
    gunboat.  The game keeps a slot's close band once it has one, but sending
    it with every gunboat order means no ordering of batches can leave a hull
    on the stock 120-150 band."""
    from . import bridge, intel
    close = intel.gunboat_close(ship_class) if name == "gunboat" else 0
    return bridge.stance(name, slot, close=close)


def hull_stance_order(profile_stance, ship_class, slot):
    """hull_stance() as an order for one slot."""
    return stance_order(hull_stance(profile_stance, ship_class), ship_class,
                        slot)


def _restore_stance(mem, slot):
    """The stance a hull goes back to when a cloak play hands it back: the one
    sparring gave it, else its own pick under the aggressive cap."""
    if mem.get("restore_stance"):
        return stance_order(mem["restore_stance"], mem.get("ship_class", ""),
                            slot)
    return hull_stance_order("aggressive", mem.get("ship_class", ""), slot)


def _uncloak(orders, mem, profile, slot, spar, cancel_break=True, focus=True):
    """Come out of the cloak and hand the hull its fight back.

    Duels keep the sequence proven in two logged duels: decloak, cancel the flee,
    stance, focus the player.  Sparring rebuilds the attack AI FIRST, while
    the ship is still settled in its cloak, and decloaks after - and sends no
    focus, because a spar ship is already spar-focused and the stance rebuild
    picks that up: one AI rebuild, not two.  The one crash of the 09-10 cloak
    sets came four seconds after a decloak, a stance and a focus landed on a
    single ship in the same tick."""
    from . import bridge
    mem.pop("cloak_hold_min", None)
    mem["cloak_off_tick"] = mem.get("cloak_now", 0)
    if spar:
        if _can(profile, "stance"):
            orders.append(_restore_stance(mem, slot))
        if cancel_break:
            orders.append(bridge.brk(0, slot))
        orders.append(bridge.cloak(0, slot))
        return
    orders.append(bridge.cloak(0, slot))
    if cancel_break:
        orders.append(bridge.brk(0, slot))
    if _can(profile, "stance"):
        orders.append(_restore_stance(mem, slot))
    if focus and _can(profile, "focus"):
        orders.append(bridge.focus("player", slot))


# The verbs a sparring ship's cloak play may use.  Sparring is otherwise the
# "holodeck" profile (no doctrine at all): this grants cloak_reflex what its
# plays need, and nothing the other side's stock AI does not also have.
SPAR_CLOAK_PROFILE = {"label": "spar-cloak", "reflex": True,
                      "allowed": ["stance", "power", "cloak", "break",
                                  "repair", "focus"]}


def opening_orders(profile, ship_class, ship_name, escorts=None):
    """The spawn-day batch: spawn flag (+escorts), apply opening doctrine."""
    from . import bridge

    from . import intel
    orders = [bridge.spawn(intel.spawn_name(ship_class), ship_name)]
    for esc_class, esc_name in (escorts or []):
        orders.append(bridge.spawn(intel.spawn_name(esc_class), esc_name))

    for step in profile["opening"]:
        parts = step.split()
        verb, arg = parts[0], parts[1] if len(parts) > 1 else ""
        if verb == "stance":
            pick = hull_stance(arg, ship_class)
            if pick == "gunboat":
                # This broadcast reaches the escorts too, and the gunboat fork
                # would replace THEIR attack AI.  The flagship gets gunboat by
                # slot once it is on the board (director._gunboat_opening).
                pick = "balanced"
            orders.append(bridge.stance(pick))
        elif verb == "target":
            orders.append(bridge.target(arg))
        elif verb == "power":
            orders.append(bridge.power(arg))
        elif verb == "focus":
            orders.append(bridge.focus(arg))
        elif verb == "free":
            orders.append(bridge.free())

    ab = profile["autobreak"]
    if ab:
        orders.append(bridge.autobreak(*ab))
    return orders
