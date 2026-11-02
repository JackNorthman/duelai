#!/usr/bin/env python3
"""DUELAI offline self-check: run after editing duelai/*.py, before a restart.

Exercises the doctrine, brain-gate, cloak, learning and sparring rules
without the game running: drafting (persona, mirror, sections, fairness),
stance clamp, target validation, power gate + reflex, hold-fire watchdog,
range reader, order log, and replays of situations seen in real duels.
Exit code 1 on any failure.  A few seconds.

It is a regression suite, not a unit-test fixture set: it needs your own
hull database at data/ships.json (+ data/torpedoes.json), extracted from
your install, and it expects the stock and Orion Delta hulls it names
(Galaxy, Defiant, CaliforniaT, Feklhr, KD5M ...) to be in it.  Without that
file it stops with exit code 2.  Learning-file checks read data/*.json if
present; every check that writes uses a temporary copy.
"""
import sys, os, json, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from duelai import intel, doctrine, brain, bridge, evaluate, director, learning

if not intel._db():
    print("selfcheck needs data/ships.json (the hull database extracted from "
          "your own install) - see README, 'Hull database'.")
    sys.exit(2)

FAILS = []
COUNT = [0]
def check(name, ok, detail=""):
    COUNT[0] += 1
    print("  %s %s%s" % ("ok " if ok else "FAIL", name, (" - " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)

db = intel._db()
print("== drafting ==")
bad = mirrors = sections = empty = 0
for ship in ("CaliforniaT", "Galaxy", "Sovereign", "Vorcha", "WorldRazer"):
    pf = intel._family(intel._resolve_player_ship(ship) or {})
    for persona in [""] + sorted(director.PERSONAS):
        for diff in ("officer", "captain", "kobayashi"):
            sid, _ = intel.dealer_choice(ship, "Federation", "an honest 1v1 battle",
                                         persona=persona, difficulty=diff, no_carrier=True)
            if not sid:
                empty += 1; continue
            d = db[sid]
            if intel._is_section(d): sections += 1
            if intel._family(d) == pf and persona != "godship": mirrors += 1
            if persona:
                want = intel._PERSONA_RACE.get(persona, persona)
                r = (intel.ship_race(sid) or "").lower().replace("tmp ", "").replace("ent ", "")
                if not (want in r or r in want): bad += 1
check("persona always drafts its own faction", bad == 0, bad)
check("never a mirror of the player's family", mirrors == 0, mirrors)
check("never a separation section", sections == 0, sections)
check("never an empty draft", empty == 0, empty)
sid, _ = intel.dealer_choice("CaliforniaT", "Federation", "1v1, mirror match please",
                             persona="federation", difficulty="officer", no_carrier=True)
check("explicit mirror request honoured", sid == "CaliforniaT", sid)
s = intel._strength
check("Defiant reads > +80% vs California (was +13%)",
      s(db["Defiant"]) / s(db["CaliforniaT"]) > 1.8)
sid, _ = intel.dealer_choice("CaliforniaT", "Federation", "an honest 1v1 battle",
                             persona="federation", difficulty="officer", no_carrier=True)
check("officer draft within +-35% of the player", abs(s(db[sid]) / s(db["CaliforniaT"]) - 1) < 0.35, sid)

print("== stance ==")
check("flash-beam hull at captain -> balanced", doctrine.hull_stance("aggressive", "Challenger") == "balanced")
check("hull never promotes above its profile", doctrine.hull_stance("balanced", "Sovereign") == "balanced")
o = brain.to_bridge_orders([{"verb": "stance", "arg": "aggressive"}], doctrine.PROFILES["captain"]["allowed"], 0, {"ship_class": "Challenger"})
check("LLM 'stance aggressive' capped by the hull", [(x.cmd, x.arg) for x in o] == [(1, 2)])

print("== targeting ==")
mem = {"player_kinds": {"hull", "engines", "sensors", "weapons", "torps", "warp"}}
check("'target shields' -> hull", doctrine.valid_target(mem, "shields") == "hull")
check("'target power' -> hull", doctrine.valid_target(mem, "power") == "hull")
check("'target engines' stays engines", doctrine.valid_target(mem, "engines") == "engines")
check("unknown enemy -> hull only", doctrine.valid_target({}, "engines") == "hull")
check("'shields' gone from the LLM vocabulary", "hull|shields|" not in brain.__dict__.get("SYSTEM", "") and "target: hull|weapons" in open(os.path.join(os.path.dirname(brain.__file__), "brain.py")).read())

print("== power ==")
prof = doctrine.PROFILES["captain"]
m = {"reach": 125, "power": "balanced", "power_tick": -9999}
F = {"Hull": 100, "Batt": 99, "Rng": 100, "ShF": 100, "ShA": 100, "ShL": 100, "ShR": 100, "ShT": 100, "ShB": 100, "AbFires": 0}
oo = []; doctrine.power_reflex(oo, m, prof, dict(F), 0, 10)
check("in band + healthy -> weapons", m["power"] == "weapons" and len(oo) == 1)
oo = []; doctrine.power_reflex(oo, m, prof, dict(F), 0, 11)
check("no resend of a live setting", len(oo) == 0)
oo = []; doctrine.power_reflex(oo, m, prof, dict(F, ShF=20), 0, 15)
check("dwell holds a fresh setting", m["power"] == "weapons")
oo = []; doctrine.power_reflex(oo, m, prof, dict(F, ShF=20), 0, 25)
check("collapsing facing -> shields after dwell", m["power"] == "shields")
m2 = {"reach": 125, "power": "weapons", "power_tick": 0, "ab_fires": 0}
doctrine.power_reflex([], m2, prof, dict(F, AbFires=1), 0, 30)
check("game autobreak resyncs power to shields", m2["power"] == "shields")

print("== range + watchdog ==")
check("fleet slot Rng read", bridge.slot_range({"st": 1, "Rng": 343}) == 343)
check("-1 = no target, not point blank", bridge.slot_range({"st": 1, "Rng": -1}) == bridge.NO_RANGE)
check("343m bands as knife", evaluate._band(343).startswith("knife"))
class Snap:
    def __init__(s, tick, flag, phull):
        s.tick = tick; s.raw = {"YouHull": phull, "YouCloak": 0, "ObjCount": 3, "TorpCount": 0}; s.fleet = {0: flag}; s.foes = {}
    def live_fleet(s): return s.fleet
    def live_foes(s): return {}
mem = {"flag": 0, "reach": 125, "ship_class": "Steamrunner", "power": "weapons", "power_tick": -9999}
fl = {"st": bridge.SLOT_ALIVE, "Hull": 90, "Batt": 99, "Rng": 80, "Sens": 100, "Weap": 100, "TSub": 2, "Cloak": bridge.CLOAK_NO_DEVICE,
      "ShF": 80, "ShA": 80, "ShL": 80, "ShR": 80, "ShT": 80, "ShB": 80, "Dmg": 0, "AbFires": 0}
fired = [t for t in range(1, 20) if any(x.cmd == bridge.CMD_TARGET for x in doctrine.reflexes(Snap(t, dict(fl), 87), mem, prof))]
check("hold-fire watchdog fires once after 15 dry polls", fired == [16], fired)

print("== evaluator + order log ==")
a = evaluate.analyze([dict(tick=i, hull=100, player_hull=100, rng=80, batt=99, shield_min=90, tsub=2, weap=100) for i in range(40)], {"result": "defeat"}, reach=125)
check("held-fire fact emitted", any(f.startswith("held fire") for f in a["facts"]))
check("band lesson is reach-aware", evaluate._band_holds("knife (<350)", 125))
check("order describe()", bridge.describe(bridge.target("hull", 0)) == "target hull @0")
w = bridge.OrderWriter.__new__(bridge.OrderWriter); w.history = []; w.seq = 5; w.mark = 5
check("writer keeps history", hasattr(w, "batches_since"))

print("== gunboat + cloak learner ==")
from duelai import sparring, cloaklearn
import tempfile, random as _random
gp = intel.gunboat_profile("DefiantNX")
check("DefiantNX is a gunboat: close band inside its 100m cannons", bool(gp) and gp["close"] == 80, gp)
check("DefiantNX prefers the gunboat stance", intel.preferred_stance("DefiantNX")[0] == "gunboat")
check("a beam cruiser is no gunboat", intel.gunboat_profile("Sovereign") is None)
o = doctrine.stance_order("gunboat", "DefiantNX", 4)
check("gunboat order carries its close band", (o.cmd, o.arg, o.ship, o.text) == (1, 6, 4, "80"), (o.cmd, o.arg, o.ship, o.text))
check("other stances carry no text", doctrine.stance_order("balanced", "DefiantNX", 4).text == "")
check("an opening broadcast never hands out gunboat",
      not any(x.cmd == 1 and x.arg == 6 for x in doctrine.opening_orders(doctrine.PROFILES["officer"], "DefiantNX", "Defiant")))
check("captain cap still yields gunboat for a Defiant", doctrine.hull_stance("aggressive", "DefiantNX") == "gunboat")
sp = sparring.parse_spec("spar defiantnx vs defiantnx 1v1 x3 cloak=both")
check("spar grammar: cloak=both", sp.get("cloak") == "both", sp)
sc = sparring.build_scenario(sp)
check("a cloak set defaults to @1500 and is keyed apart",
      sc.get("spawn") == 1500 and sc.get("cloak") == "both" and sc["key"].endswith("@1500 cloak=both"), sc.get("key"))
check("spar target decode", bridge.spar_target({"Tgt": 105}) == 5 and bridge.spar_target({"Tgt": 8}) is None
      and bridge.spar_target({"Tgt": 99}) is None)
_real_cl = cloaklearn.DATA_PATH
cloaklearn.DATA_PATH = os.path.join(tempfile.mkdtemp(), "cloak_learning.json")
first = set(cloaklearn.choose("TestHull")[0] for _ in range(40))
check("an untried hull explores every play", first == set(cloaklearn.PLAYS), first)
for p in cloaklearn.PLAYS:
    for _ in range(cloaklearn.MIN_TRIES):
        cloaklearn.record("TestHull", p, "w" if p == "wolfpack" else "l", opponent="X", side="red")
check("the winning play ranks first", cloaklearn.best("TestHull", min_rounds=1)[0] == "wolfpack")
_rng = _random.Random(7)
hist = [cloaklearn.choose("TestHull", _rng)[0] for _ in range(300)]
check("sampling favours the winner and keeps exploring",
      hist.count("wolfpack") > 150 and len(set(hist)) >= 2, {p: hist.count(p) for p in cloaklearn.PLAYS})
for _ in range(3):
    cloaklearn.record("ThinHull", "alpha", "w", opponent="X", side="blue")
check("a duel ignores a thin record (3 rounds)", cloaklearn.duel_play("ThinHull") is None)
check("a duel follows a settled one (%d rounds)" % cloaklearn.DUEL_MIN_ROUNDS, cloaklearn.duel_play("TestHull") == "wolfpack")
cloaklearn.DATA_PATH = _real_cl
cm = {"ship_class": "DefiantNX"}
F2 = dict(fl, Cloak=bridge.CLOAK_READY, Hull=40, Rng=500, Type="DefiantNX", Batt=99)
check("play off never cloaks",
      doctrine.cloak_reflex(Snap(1, dict(F2), 90), dict(cm), doctrine.SPAR_CLOAK_PROFILE, dict(F2), 3, 1, play="off", spar=True) == [])
oo = [(x.cmd, x.arg) for x in doctrine.cloak_reflex(Snap(1, dict(F2), 90), dict(cm), doctrine.SPAR_CLOAK_PROFILE, dict(F2), 3, 1, play="mend", spar=True)]
check("hurt, mend play: defensive, then cloak", oo[:2] == [(1, 3), (4, 1)], oo)
m3 = dict(cm, cloak_phase="stalk", own_cloaked_at=0, restore_stance="gunboat")
F3 = dict(fl, Cloak=bridge.CLOAK_CLOAKED, Hull=95, Rng=100, Closing=2, Type="DefiantNX", Batt=90)
oo = [(x.cmd, x.arg, x.text) for x in doctrine.cloak_reflex(Snap(20, dict(F3), 90), m3, doctrine.SPAR_CLOAK_PROFILE, dict(F3), 3, 20, play="alpha", spar=True)]
check("spar strike: gunboat back with its band BEFORE the decloak, no second rebuild, never the player",
      (4, 0, "") in oo and (1, 6, "80") in oo and oo.index((1, 6, "80")) < oo.index((4, 0, ""))
      and not any(x[0] == 11 for x in oo), oo)
m7 = dict(cm, cloak_phase="stalk", own_cloaked_at=0)
oo = [(x.cmd, x.arg) for x in doctrine.cloak_reflex(Snap(20, dict(F3), 90), m7, doctrine.PROFILES["captain"], dict(F3), 3, 20, play="alpha")]
check("duel strike order unchanged: decloak, cancel flee, stance, focus player",
      [c for c, a in oo][:4] == [4, 7, 1, 11] and (11, 8) in oo, oo)
m8 = dict(cm, cloak_phase="escape", own_cloaked_at=10)
F8 = dict(fl, Cloak=bridge.CLOAK_CLOAKED, Hull=98, Rng=300, Type="DefiantNX", Batt=90)
oo = doctrine.cloak_reflex(Snap(14, dict(F8), 90), m8, doctrine.SPAR_CLOAK_PROFILE, dict(F8), 3, 14, play="mend", spar=True)
check("a mend holds the cloak at least %d ticks (98%% hull, dark 4 ticks)" % doctrine.CLOAK_MEND_MIN_HOLD,
      not any(x.cmd == 4 for x in oo) and m8.get("cloak_phase") == "escape", [(x.cmd, x.arg) for x in oo])
F4 = dict(fl, Cloak=bridge.CLOAK_READY, Hull=100, Rng=-1, Type="DefiantNX", Batt=99)
check("healthy, enemy hidden: no ambush on a ship it cannot see",
      doctrine.cloak_reflex(Snap(200, dict(F4), 90), dict(cm), doctrine.SPAR_CLOAK_PROFILE, dict(F4), 3, 200, play="alpha", spar=True, enemy_hidden=True) == [])
m5 = dict(cm, cloak_phase="stalk", own_cloaked_at=0)
F5 = dict(fl, Cloak=bridge.CLOAK_CLOAKED, Hull=95, Rng=-1, Closing=0, Type="DefiantNX", Batt=90)
oo = [(x.cmd, x.arg) for x in doctrine.cloak_reflex(Snap(31, dict(F5), 90), m5, doctrine.SPAR_CLOAK_PROFILE, dict(F5), 3, 31, play="alpha", spar=True, enemy_hidden=True)]
check("spar: a blind stalk gives up after %d ticks" % doctrine.SPAR_STALK_BLIND, (4, 0) in oo and m5.get("cloak_abort_at") == 31, oo)
F6 = dict(F5, Rng=2000)
oo = doctrine.cloak_reflex(Snap(60, dict(F6), 90), dict(cm, cloak_phase="stalk", own_cloaked_at=0), doctrine.PROFILES["captain"], dict(F6), 3, 60)
check("duel stalks are untouched by the spar timeout", not any(x.cmd == 4 for x in oo), [(x.cmd, x.arg) for x in oo])
oo = doctrine.cloak_reflex(Snap(200, dict(F6), 90), dict(cm, cloak_phase="stalk", own_cloaked_at=0), doctrine.PROFILES["captain"], dict(F6), 3, 200)
check("...but a duel stalk with no band after %d ticks comes out" % doctrine.DUEL_STALK_MAX, any(x.cmd == 4 for x in oo), [(x.cmd, x.arg) for x in oo])

print("== lessons ==")
ls = learning._load().get("lessons2", [])
check("no 'fight from a different band' lesson on file", not any("different band" in l.get("text", "") for l in ls))

print("== main battery / dud bench (2026-09-16) ==")
for cid in ("KD5M", "KD7M"):
    w = intel.weapon_profile(cid)
    check("%s: Mauler kept out of the profile" % cid,
          w["kind"] == "flash-beam" and w["specials"] and w["specials"][0][0] == "Mauler"
          and w["reach"] >= 140 and intel.preferred_stance(cid)[0] != "aggressive",
          (w["kind"], w["reach"], w["specials"], intel.preferred_stance(cid)[0]))
w = intel.weapon_profile("Yamato")
check("Yamato: two assault phasers do not make it sustained-beam", w["kind"] == "flash-beam", w["kind"])
w = intel.weapon_profile("CrystalEntity")
check("Crystalline Entity still reads sustained-beam", w["kind"] == "sustained-beam", w["kind"])
duds = learning.dud_hulls()
check("dud bench holds the known dud hulls", "KD5M" in duds and "KD7M" in duds, duds)
for diff in ("captain", "kobayashi"):
    sid, _ = intel.dealer_choice("Miranda", "Federation", "an honest 1v1 battle",
                                 persona="klingon", era_pick="tmp", difficulty=diff, no_carrier=True)
    check("%s TMP klingon draft vs a Miranda skips the bench" % diff, sid and sid not in duds, sid)

print("== ally mode (2026-09-25) ==")
from duelai import ally as _ally
sp = _ally.parse_spec("ally borg 3 tng wing=akira @2500")
check("ally spec parses race/size/era/wing/spawn",
      sp == {"red": "borg", "size": 3, "era": "tng", "spawn": 2500, "gods": 1, "wing": "akira"}, sp)
check("ally text is not mistaken for spar", _ally.parse_spec("spar borg vs federation") is None)
sc = _ally.build_scenario(_ally.parse_spec("ally dominion 2"), "Voyager J")
check("ally drafts 2 reds + a wingman", not isinstance(sc, str) and len(sc["red"]) == 2 and sc["wing"][0], sc)
def _snap(tick, you, fleet):
    st = {"Tick": tick, "YouAlive": 1}
    for k, v in you.items(): st["You" + k] = v
    for i, f in fleet.items():
        st["S%dSt" % i] = 1
        for k, v in f.items(): st["S%d%s" % (i, k)] = v
    return bridge.Snapshot(st)
_fl = {0: {"Side": 0, "Name": "R1", "OnYou": 0, "Rng": 900}, 1: {"Side": 0, "Name": "R2", "OnYou": 0, "Rng": 700},
       2: {"Side": 1, "Name": "W", "Hull": 100, "ShAvg": 100}}
_m = {}
_ally.wingman_orders(_snap(10, {"ShAvg": 100, "Hull": 100, "TgtS": 1}, _fl), _m, 2)
check("wingman follows the player's target", _m.get("wing_focus") == 1 and _m.get("wing_mode") == "follow", _m)
_fl[0]["OnYou"] = 1
o, say = _ally.wingman_orders(_snap(12, {"ShAvg": 70, "Hull": 100, "TgtS": 1, "HitByS": 0, "HitTick": 12}, _fl), _m, 2)
check("wingman breaks to defend when the player is hammered",
      _m.get("wing_mode") == "defend" and _m.get("wing_focus") == 0 and say and say.startswith("Breaking"), (_m, say))
check("defend focus is sent as fleet slot 100+N",
      any(x.cmd == bridge.CMD_FOCUS and x.arg == bridge.SPAR_TGT_BASE for x in o), [bridge.describe(x) for x in o])
check("wingman never focuses the player", all(x.arg != bridge.FOCUS_PLAYER for x in o if x.cmd == bridge.CMD_FOCUS))

print("== online brain (2026-10-23) ==")
from duelai import online as _on, strategist as _strat
_b = _on.OnlineBrain("sk-test", "claude-sonnet-5-5", "low").body("SYS", [{"role": "user", "content": "x"}], False)
check("sonnet 5.5 online call runs thinking off (between_tools)", _b.get("thinking") == {"type": "between_tools"}, _b.get("thinking"))
check("online system prompt is cached", _b["system"][0].get("cache_control") == {"type": "ephemeral"})
check("orders-only schema has no say", "say" not in _b["output_config"]["format"]["schema"]["properties"])
_b = _on.OnlineBrain("sk-test", "claude-opus-5-5", "low").body("SYS", [], True)
check("opus 5.5 omits thinking (it cannot be disabled)", "thinking" not in _b and _b["output_config"]["effort"] == "low")
check("chat schema carries say, assessment first", list(_b["output_config"]["format"]["schema"]["properties"])[:2] == ["assessment", "say"])
def _fake_ok(key, body, timeout):
    return {"model": body["model"], "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"assessment": "a", "orders": [{"verb": "stance", "arg": "Aggressive"}]}'}],
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 2000, "output_tokens": 50}}
def _fake_down(key, body, timeout):
    raise _on.ApiError("URLError: offline")
_br = brain.Brain(); _br.local_ok = False; _br.banter = 0; _br.system = "SYS"
_br.set_online(_on.OnlineBrain("sk-test", post=_fake_ok))
_r = _br.decide("brief")
check("online decision parsed + lower-cased", _r["orders"] == [{"verb": "stance", "arg": "aggressive"}] and _br.served == "online", _r)
check("online cost is tracked", _br.online.stats["calls"] == 1 and 0 < _br.online.stats["cost"] < 0.01, _br.online.stats)
_br.set_online(_on.OnlineBrain("sk-test", post=_fake_down))
_br._chat = lambda msgs: '{"assessment": "b", "orders": []}'
_br.local_ok = True
_r = _br.decide("brief")
check("online failure falls back to the local model", _br.served == "local" and _br.online_fail == 1, (_br.served, _br.online_fail))
_br.decide("brief"); _br.decide("brief"); _calls = []
_br.online._post = lambda *a: _calls.append(1) or _fake_ok(*a)
_br.decide("brief")
check("3 online failures park it until the next battle", not _calls and _br.served == "local", _br.online_fail)
_br.set_online(None)
check("offline mode never touches the API", _br.online is None and _br.ok)
_st = _strat.Strategist("sk-test")
check("strategist defaults to Opus 5.5 at high effort", _st.model == "claude-opus-5-5" and _st.effort == "high")

print("== brains sparring (2026-10-23) ==")
from duelai import sparbrains as _sb, sparring as _sp
_spec = _sp.parse_spec("spar federation vs klingon 2v2 x4 brains=swap cloak=both")
check("brains=swap parses", _spec["brains"] == "swap", _spec)
_sc = _sp.build_scenario(dict(_spec))
check("brains set keyed apart, cloak learner off, 2000m default",
      _sc["key"].endswith(" brains") and not _sc["cloak"] and _sc["spawn"] == 2000, (_sc["key"], _sc["cloak"], _sc["spawn"]))
def _bsnap(tick, ships):
    st = {"Tick": tick, "AckSeq": 0}
    for i, d in ships.items():
        base = {"St": 1, "Hull": 100, "Cloak": 0, "Stance": 2, "TSub": 0, "Tgt": 99, "Side": 0, "Type": "Galaxy"}
        base.update(d)
        for k, v in base.items():
            st["S%d%s" % (i, k)] = v
    return bridge.Snapshot(st)
_ships = {0: {"Side": 0, "Type": "Galaxy", "Tgt": 102}, 1: {"Side": 0, "Type": "Neghvar", "Cloak": 1, "Tgt": 102},
          2: {"Side": 1, "Type": "Vorcha"}, 3: {"Side": 1, "Type": "Vorcha", "Cloak": 3}}
_v = _sb.SideView(0); _s = _bsnap(10, _ships); _v.update(_s)
check("own ships A1/A2, enemies E1/E2 by slot order", _v.own == {0: "A1", 1: "A2"} and _v.enemy == {2: "E1", 3: "E2"}, (_v.own, _v.enemy))
_o, _ok, _bad = _sb.translate([{"ship": "all", "verb": "focus", "arg": "E1"}], _s, _v)
check("'all' focus expands to every live ship as fleet focus 100+slot",
      sorted((x.cmd, x.arg, x.ship) for x in _o) == [(bridge.CMD_FOCUS, 102, 0), (bridge.CMD_FOCUS, 102, 1)], [bridge.describe(x) for x in _o])
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "focus", "arg": "E1"}], _s, _v)
check("a focus the ship already has is not re-sent (no needless AI rebuild)", not _o, [bridge.describe(x) for x in _o])
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "focus", "arg": "E2"}], _s, _v)
check("a cloaked enemy cannot be focused", not _o and "enemy cloaked" in _bad[0], _bad)
_o, _ok, _bad = _sb.translate([{"ship": "A2", "verb": "cloak", "arg": "on"}, {"ship": "A2", "verb": "stance", "arg": "aggressive"}], _s, _v)
check("cloak + AI rebuild on one ship in one turn: cloak only",
      [x.cmd for x in _o] == [bridge.CMD_CLOAK] and "cloak busy" in _bad[0], ([bridge.describe(x) for x in _o], _bad))
_ships[1]["Cloak"] = 3; _v.update(_bsnap(20, _ships))
_o, _ok, _bad = _sb.translate([{"ship": "A2", "verb": "cloak", "arg": "off"}], _bsnap(22, _ships), _v)
check("decloak refused inside the minimum hold", not _o and "minimum hold" in _bad[0], _bad)
_o, _ok, _bad = _sb.translate([{"ship": "A2", "verb": "cloak", "arg": "off"}], _bsnap(40, _ships), _v)
check("decloak uses the spar sequence: stance, break 0, cloak 0",
      [x.cmd for x in _o] == [bridge.CMD_STANCE, bridge.CMD_BREAK, bridge.CMD_CLOAK] and _o[-1].arg == 0, [bridge.describe(x) for x in _o])
_o, _ok, _bad = _sb.translate([{"ship": "A2", "verb": "target", "arg": "weapons"}], _bsnap(41, _ships), _v)
check("no AI rebuild for a ship that is dark", not _o and "cloak busy" in _bad[0], _bad)
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "stance", "arg": "gunboat"}, {"ship": "A1", "verb": "target", "arg": "shields"}], _bsnap(42, _ships), _v)
check("gunboat on a non-gunboat hull -> balanced (= current, not re-sent); 'shields' refused",
      not _o and any("target shields" in b for b in _bad), ([bridge.describe(x) for x in _o], _bad))
_cm = _sb.SideCommander("claude", 0, "claude-sonnet-5-5", "sk-test", post=lambda *a: (_ for _ in ()).throw(online.ApiError("offline")))
_cm.system = "S"
_ships2 = dict(_ships); _ships2[4] = {"Side": 1, "St": 2, "Type": "Vorcha"}
_v2 = _sb.SideView(0); _v2.update(_bsnap(5, _ships2))
check("wrecks from an earlier round get no id", 4 not in _v2.enemy, _v2.enemy)
check("a failed Claude call is a lost turn, not a hand-off", _cm.decide("brief") is None and _cm.stats["fails"] == 1)

print("== brains learning (2026-10-24) ==")
check("refusals come back in the next brief",
      _sb.feedback_line(["A1 focus e1"], ["A1 target weapons (one AI change per ship per turn)"]).startswith("LAST ORDERS: applied A1 focus e1; refused A1 target weapons"))
check("no feedback line when nothing was ordered", _sb.feedback_line([], []) == "")
_k1 = _sb.memory_key({"red_race": "cardassian", "blue_race": "monster", "size": 1, "gods": 0, "key": "tng-1v1-x"})
_k2 = _sb.memory_key({"red_race": "monster", "blue_race": "cardassian", "size": 1, "gods": 0, "key": "tng-1v1-x"})
check("one memory per matchup whichever side each race drew", _k1 == _k2, (_k1, _k2))
_txt = _sb.system_prompt(["Galaxy"], ["Vorcha"], ["r1: LOST. Lesson: break earlier."])
check("notes land in the system prompt", "YOUR OWN NOTES" in _txt and "break earlier" in _txt)
check("no notes section on a first meeting", "YOUR OWN NOTES" not in _sb.system_prompt(["Galaxy"], ["Vorcha"], []))
_p0 = _sb.DATA_PATH
import tempfile as _tf
_sb.DATA_PATH = os.path.join(_tf.mkdtemp(), "brains.json")
_scn = {"red_race": "a", "blue_race": "b", "size": 1, "gods": 1, "key": "tng-1v1"}
for _i in range(_sb.MEMORY_KEEP + 3):
    _sb.add_memory(_scn, "m", "note %d" % _i)
check("memory capped per brain per matchup", _sb.memory_for(_scn, "m") == ["note %d" % i for i in range(3, _sb.MEMORY_KEEP + 3)], _sb.memory_for(_scn, "m"))
check("memory is per brain", _sb.memory_for(_scn, "other") == [])
_sb.record({"key": "k", "winner_brain": "claude", "claude_model": "c", "local_model": "l"})
check("recording a round keeps the memory", len(_sb.memory_for(_scn, "m")) == _sb.MEMORY_KEEP)
_sb.DATA_PATH = _p0
_cm = _sb.SideCommander("local", 0, "m", chat=lambda model, msgs, url=None: '{"lesson": "Break at 30% fore shield."}')
check("a commander writes its own lesson", _cm.reflect("summary") == "Break at 30% fore shield.")

print("== sparring lessons reach duels (2026-10-24) ==")
_p0 = _sb.DATA_PATH
_sb.DATA_PATH = os.path.join(_tf.mkdtemp(), "brains.json")
_sb.add_hull_lesson({"own": ["Damar"], "enemy": ["First"], "engine": "claude", "model": "c", "result": "lost", "lesson": "vs First: hold 200m.", "time": 1})
_sb.add_hull_lesson({"own": ["Damar"], "enemy": ["Galaxy"], "engine": "claude", "model": "c", "result": "won", "lesson": "vs Galaxy: brawl.", "time": 2})
_sb.add_hull_lesson({"own": ["Damar"], "enemy": ["First"], "engine": "local", "model": "q", "result": "lost", "lesson": "generic local note", "time": 3})
_sb.add_hull_lesson({"own": ["Damar", "First"], "enemy": ["Galaxy", "Galaxy"], "engine": "claude", "model": "c", "result": "won", "lesson": "fleet note", "time": 4})
_hl = _sb.hull_lessons_for("Damar", "First")
check("lessons vs the player's hull rank first", _hl and _hl[0]["lesson"] == "vs First: hold 200m.", [e["lesson"] for e in _hl])
check("1v1 lessons rank above fleet ones", [e["lesson"] for e in _hl][1:] == ["vs Galaxy: brawl.", "fleet note"], [e["lesson"] for e in _hl])
check("only Claude-written lessons go to duels", all(e["engine"] == "claude" for e in _hl))
check("a hull nobody sparred in gets no block", _sb.hull_lessons_text("Galaxy", "Damar") == "")
check("duel block names the hull", "your hull (" in _sb.hull_lessons_text("Damar", "First"))
_sb.add_memory({"red_race": "a", "blue_race": "b", "size": 1, "gods": 1, "key": "k"}, "claude-x",
               "10-24 r1: flew Damar DN vs First DN, LOST in 265 ticks (hull 0% vs 100%). Lesson: Keep range.")
check("old memory notes backfill once", _sb.backfill_hull_lessons() == 1 and _sb.backfill_hull_lessons() == 0)
_sb.DATA_PATH = _p0
_ls = [{"text": "duel", "topic": "opening:x"}, {"text": "c-own", "topic": "cloak:Damar"}, {"text": "c-other", "topic": "cloak:Vorcha"},
       {"text": "s-rel", "topic": "spar:tng-1v1-Damar DN vs First DN (manual)"}, {"text": "s-unrel", "topic": "spar:tng-2v2-borg vs klingon"}]
check("duel lessons: own cloak line + named spar result kept, the rest dropped",
      [l["text"] for l in learning.duel_lessons(_ls, "Damar", "First")] == ["duel", "c-own", "s-rel"],
      [l["text"] for l in learning.duel_lessons(_ls, "Damar", "First")])
check("no hull given = old unfiltered behaviour", len(learning.duel_lessons(_ls)) == 5)

print("== duel lessons by hull (2026-10-24) ==")
_kl = {"text": "global text 125m", "topic": "range:contested_knife", "count": 9, "last": 5,
       "hulls": {"Challenger": {"n": 2, "text": "our guns only reach 125m"}, "Vmelak": {"n": 3, "text": "our guns only reach 145m"}}}
_gen = {"text": "The opening exchange decides these fights.", "topic": "opening:decisive", "count": 7, "last": 1, "hulls": {"Galaxy": {"n": 1, "text": "x"}}}
_dcl = {"text": "Cloak to repair works.", "topic": "cloak:repair_works", "count": 2, "last": 1}
_r = learning.duel_lessons([_kl, _gen, _dcl], "Challenger", "Galaxy")
check("a hull gets its own wording and count", any(l["text"] == "our guns only reach 125m" and l["count"] == 2 for l in _r), [(l["text"], l["count"]) for l in _r])
check("general lessons reach every hull", any(l["topic"] == "opening:decisive" for l in _r))
check("duel cloak lessons survive the sparring filter", any(l["topic"] == "cloak:repair_works" for l in _r))
_r = learning.duel_lessons([_kl], "Galaxy", "Sovereign")
check("a long-reach hull never sees the 125m knife wording", not any("125m" in l["text"] for l in _r), [l["text"] for l in _r])
_r = learning.duel_lessons([_kl], "DefiantNX", "Galaxy")
check("a similar-reach hull borrows it, labelled", _r and "similar reach" in _r[0]["text"], [l["text"] for l in _r])
check("hull-scoped = range topics and quoted metres",
      learning.hull_scoped({"topic": "range:x", "text": ""}) and learning.hull_scoped({"topic": "", "text": "hold 200m"})
      and not learning.hull_scoped({"topic": "", "text": "strike sensors first"}) and not learning.hull_scoped(_gen))
_lp = learning.DATA_PATH
learning.DATA_PATH = os.path.join(_tf.mkdtemp(), "learning.json")
json.dump({"battles": [], "lessons2": []}, open(learning.DATA_PATH, "w"))
learning.add_lesson("Hold the merge on a fresh facing at 120m.", topic="range:test", ship_class="Challenger")
learning.observe_battle({"candidates": [("range:test", "Hold the merge at 145m.")]}, "Vmelak")
_h = learning._load()["lessons2"][0].get("hulls", {})
check("new lessons and confirmations record the hull + its wording",
      _h.get("Challenger", {}).get("n") == 1 and _h.get("Vmelak", {}).get("text") == "Hold the merge at 145m.", _h)
learning.DATA_PATH = _lp

print("== warp break (2026-10-24) ==")
_w = bridge.warp(30, 2)
check("warp order is code 18 to one slot", (_w.cmd, _w.arg, _w.ship) == (18, 30, 2) and bridge.describe(_w).startswith("warp 30"), bridge.describe(_w))
_o = brain.to_bridge_orders([{"verb": "warp", "arg": "200"}], doctrine.PROFILES["captain"]["allowed"], 0)
check("duel brain: warp clamps to 90 s at captain", len(_o) == 1 and _o[0].cmd == 18 and _o[0].arg == 90, [bridge.describe(x) for x in _o])
check("officer cannot order a warp break", not brain.to_bridge_orders([{"verb": "warp", "arg": "30"}], doctrine.PROFILES["officer"]["allowed"], 0))
_ws = {0: {"Side": 0, "Type": "Galaxy", "Tgt": 102}, 1: {"Side": 0, "Type": "Galaxy", "Man": 18, "WB": 2, "Tgt": 102},
       2: {"Side": 1, "Type": "Vorcha"}}
_wv = _sb.SideView(0); _wsn = _bsnap(10, _ws); _wv.update(_wsn)
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "warp", "arg": "45"}, {"ship": "A1", "verb": "focus", "arg": "E1"}], _wsn, _wv)
check("spar: warp counts as the ship's one AI change", [x.cmd for x in _o] == [18] and "one AI change" in _bad[0], ([bridge.describe(x) for x in _o], _bad))
_o, _ok, _bad = _sb.translate([{"ship": "A2", "verb": "stance", "arg": "aggressive"}, {"ship": "A2", "verb": "power", "arg": "shields"}], _wsn, _wv)
check("spar: a ship on a warp break takes power but no AI change", [x.cmd for x in _o] == [bridge.CMD_POWER] and "warp break running" in _bad[0], ([bridge.describe(x) for x in _o], _bad))
check("spar brief marks the ship out mending", "ON WARP BREAK (mending far out)" in _sb.brief(_wsn, _sb.SideView(0), 5))
_ws[3] = {"Side": 0, "Type": "Galaxy", "Hull": 20}
_wsn3 = _bsnap(11, _ws); _wv.update(_wsn3)
_o, _ok, _bad = _sb.translate([{"ship": "A3", "verb": "warp", "arg": "30"}], _wsn3, _wv)
check("spar: no warp below 25% hull", not _o and "hull too low" in _bad[0], _bad)

print("== hull weaknesses (2026-10-24) ==")
from duelai import weakness as _wk
_wp0 = _wk.DATA_PATH
_wk.DATA_PATH = os.path.join(_tf.mkdtemp(), "weakness.json")
for _n in range(4):           # Galaxy: impulse goes early, it dies 30 ticks later
    _t = _wk.Tracker()
    _t.see("x", "Galaxy", 10, {"Hull": 90, "Eng": 100})
    _t.see("x", "Galaxy", 20, {"Hull": 80, "Eng": 10})
    _t.see("x", "Galaxy", 50, {"Hull": 0}, alive=False)
    _t.see("y", "Galaxy", 10, {"Hull": 100, "Weap": 100})          # an unhurt survivor
    _t.see("w", "Galaxy", 10, {"Hull": 0}, alive=False)            # an old wreck: ignored
    _wk.record(_t)
_t = _wk.Tracker()             # late knockouts (hull 20%) prove nothing
_t.see("z", "Vorcha", 10, {"Hull": 20, "Eng": 5}); _t.see("z", "Vorcha", 30, {"Hull": 0}, alive=False)
_t2 = _wk.Tracker(); _t2.see("z", "Vorcha", 10, {"Hull": 15, "Eng": 5}); _t2.see("z", "Vorcha", 30, {"Hull": 0}, alive=False)
_wk.record(_t); _wk.record(_t2)
_tab = _wk.table()
check("wrecks from before the fight are not counted", _tab["Galaxy"]["ships"] == 8, _tab["Galaxy"])
_v = _wk.verdicts("Galaxy")
check("early impulse loss that kills = a dead-duck weakness", _v and "impulse engines down early 4x" in _v[0][1] and "dead duck" in _v[0][1] and "target engines" in _v[0][1], _v)
check("late knockouts (dying anyway) prove nothing", _wk.verdicts("Vorcha") == [] and "Eng" not in _tab["Vorcha"]["subs"], _tab.get("Vorcha"))
check("prompt text names the hull, own-hull text says protect it",
      "Measured weaknesses of the" in _wk.text_for("Galaxy") and "protect it" in _wk.text_for("Galaxy", own=True))
check("no claim without evidence", _wk.text_for("Sovereign") == "")
_wk.DATA_PATH = _wp0

print("== spar power dwell (2026-10-24) ==")
_ps = {0: {"Side": 0, "Type": "Damar", "Tgt": 101}, 1: {"Side": 1, "Type": "Damar"}}
_pv = _sb.SideView(0)
_s1 = _bsnap(100, _ps); _pv.update(_s1)
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "power", "arg": "weapons"}], _s1, _pv)
check("first power setting goes through", [x.cmd for x in _o] == [bridge.CMD_POWER], _bad)
_s2 = _bsnap(108, _ps); _pv.update(_s2)
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "power", "arg": "shields"}], _s2, _pv)
check("flip inside the dwell is refused, with ticks left", not _o and "power held 16 more ticks" in _bad[0], _bad)
check("brief shows power and the hold", "power weapons (held 16 more ticks)" in _sb.brief(_s2, _pv, 8))
_s3 = _bsnap(100 + _sb.POWER_DWELL, _ps); _pv.update(_s3)
_o, _ok, _bad = _sb.translate([{"ship": "A1", "verb": "power", "arg": "shields"}], _s3, _pv)
check("after the dwell the change is allowed", [x.cmd for x in _o] == [bridge.CMD_POWER] and _pv.power_of[0] == "shields", _bad)
check("both brains are told the rule", "HOLDS for at least %d ticks" % _sb.POWER_DWELL in _sb.system_prompt(["Damar"], ["Damar"]))

print("== duel brain skips repeats (2026-10-24) ==")
_cap = doctrine.PROFILES["captain"]["allowed"]
_mem = {"ship_class": "Damar", "power": "weapons", "player_kinds": {"hull", "engines", "sensors", "weapons"}}
_flag = {"Stance": bridge.STANCES["balanced"], "TSub": bridge.TARGETS["hull"], "Focus": bridge.FOCUS_PLAYER}
_o = brain.to_bridge_orders([{"verb": "stance", "arg": "balanced"}, {"verb": "target", "arg": "hull"},
                             {"verb": "power", "arg": "weapons"}, {"verb": "focus", "arg": "player"}], _cap, 0, dict(_mem), _flag)
check("stance/target/power/focus the ship already has are not re-sent", _o == [], [bridge.describe(x) for x in _o])
_o = brain.to_bridge_orders([{"verb": "target", "arg": "engines"}, {"verb": "power", "arg": "shields"}], _cap, 0, dict(_mem), _flag)
check("real changes still go through", [x.cmd for x in _o] == [bridge.CMD_TARGET, bridge.CMD_POWER], [bridge.describe(x) for x in _o])
_o = brain.to_bridge_orders([{"verb": "stance", "arg": "balanced"}], _cap, 0, dict(_mem))
check("without telemetry the old behaviour holds", len(_o) == 1)

print("== duel: reflex respects the brain's power (2026-10-25) ==")
_pm = {"ship_class": "Damar", "power": "balanced", "power_tick": 0, "tick": 500, "reach": 200, "ab_fires": 0}
brain.to_bridge_orders([{"verb": "power", "arg": "shields"}], doctrine.PROFILES["captain"]["allowed"], 0, _pm,
                       {"Stance": 2, "TSub": 1, "Focus": 8})
check("a brain power order is stamped with the real tick", _pm.get("power") == "shields" and _pm.get("power_tick") == 500, _pm)
_o = []
_pf = {"Hull": 100, "Batt": 100, "Rng": 150, "Closing": 0, "AbFires": 0,
       "ShF": 100, "ShA": 100, "ShL": 100, "ShR": 100, "ShT": 100, "ShB": 100}
doctrine.power_reflex(_o, _pm, doctrine.PROFILES["captain"], _pf, 0, 501)
check("the reflex does not undo it on the next tick", not _o and _pm["power"] == "shields", [bridge.describe(x) for x in _o])
doctrine.power_reflex(_o, _pm, doctrine.PROFILES["captain"], _pf, 0, 500 + doctrine.POWER_DWELL)
check("a brain pick outlasts the reflex dwell (logged flip-flop)", not _o, [bridge.describe(x) for x in _o])
doctrine.power_reflex(_o, _pm, doctrine.PROFILES["captain"], _pf, 0, 500 + doctrine.BRAIN_POWER_HOLD)
check("after the brain hold the reflex may move it again", len(_o) == 1, [bridge.describe(x) for x in _o])
_o = []; _pm2 = {"power": "shields", "power_tick": 500, "power_by": "brain"}
check("an urgent handler still cuts in", doctrine.set_power(_o, _pm2, doctrine.PROFILES["captain"], 0, "weapons", 505, True) and _pm2["power_by"] == "reflex")
import inspect as _insp
check("the duel loop records the tick every poll", 'self.mem["tick"] = snap.tick' in _insp.getsource(director.Director._fight))

print("== Fek'lhr fixes: stop running, one owner, cloak that holds (2026-10-29) ==")
_cap = doctrine.PROFILES["captain"]
def _fk(**kw):
    f = {"st": 1, "Type": "Feklhr", "Hull": 100, "Batt": 99, "Rng": 270, "Closing": 0, "Cloak": bridge.CLOAK_READY,
         "ShF": 100, "ShA": 100, "ShL": 100, "ShR": 100, "ShT": 100, "ShB": 100, "Man": 0}
    f.update(kw); return f
_s0 = _snap(190, {"Hull": 100, "Cloak": 1}, {})
_o = doctrine.cloak_reflex(_s0, {"ship_class": "Feklhr"}, _cap, _fk(Hull=88, ShA=7), 0, 190, play="mend")
check("replay t190: 88% hull + one facing at 7%, 270m -> no cloak-and-run", not _o, [bridge.describe(x) for x in _o])
_o = doctrine.cloak_reflex(_s0, {"ship_class": "Feklhr"}, _cap, _fk(Hull=50, ShA=7, Rng=600), 0, 190, play="mend")
check("a real hull wound still cloaks to mend", any(x.cmd == bridge.CMD_CLOAK and x.arg == 1 for x in _o), [bridge.describe(x) for x in _o])
_jm = {"ship_class": "Feklhr"}; _jo = []
for _t, _c in ((250, 1), (252, 2), (255, 4), (258, 1), (261, 2), (264, 4)):
    _jo += doctrine.cloak_reflex(_snap(_t, {"Hull": 100}, {}), _jm, _cap, _fk(Hull=50, Rng=600, Cloak=_c), 0, _t, play="mend")
check("two cloak cycles that never go dark park the cloak", _jm.get("cloak_jam_until", 0) > 264 and _jm.get("cloak_jam_note"), _jm.get("cloak_jam_until"))
check("...and hand the hull its stance + focus back", any(x.cmd == bridge.CMD_STANCE for x in _jo) and any(x.cmd == bridge.CMD_FOCUS for x in _jo), [bridge.describe(x) for x in _jo])
_o = doctrine.cloak_reflex(_snap(270, {"Hull": 100}, {}), _jm, _cap, _fk(Hull=50, Rng=600), 0, 270, play="mend")
check("a parked cloak is not re-ordered", not any(x.cmd == bridge.CMD_CLOAK for x in _o), [bridge.describe(x) for x in _o])
_gm = {"ship_class": "Feklhr", "reach": 174, "player_kinds": {"hull", "sensors", "engines", "weapons"}, "duel_cloak_play": "off"}
_o = doctrine.reflexes(_snap(342, {"Hull": 94, "Cloak": 1}, {0: _fk(Hull=86, ShA=8, Cloak=bridge.CLOAK_CLOAKING, Rng=284)}), _gm, _cap)
check("no guard while the cloak is ramping (replay t342)", not any(x.cmd == bridge.CMD_GUARD for x in _o), [bridge.describe(x) for x in _o])
_o = doctrine.reflexes(_snap(342, {"Hull": 94, "Cloak": 1}, {0: _fk(Hull=86, ShA=8, Rng=284)}), dict(_gm), _cap)
check("a collapsed facing visible gets guard instead", any(x.cmd == bridge.CMD_GUARD for x in _o), [bridge.describe(x) for x in _o])
_wm = dict(_gm); _wm["dry"] = 14; _wm["dry_phull"] = 90
doctrine.reflexes(_snap(140, {"Hull": 90, "Cloak": 1}, {0: _fk(Hull=95, Rng=150, TSub=bridge.TARGETS["sensors"], Weap=100)}), _wm, _cap)
check("hold-fire watchdog bans the dry subsystem for the fight", _wm.get("tsub_banned") == ["sensors"], _wm.get("tsub_banned"))
check("...so the brain's next 'target sensors' becomes hull", doctrine.valid_target(_wm, "sensors") == "hull" and doctrine.valid_target(_wm, "engines") == "engines")
_pc = {"player_cloaked_at": 294, "ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "off", "power": "shields", "power_tick": 0}
_o = doctrine.reflexes(_snap(315, {"Hull": 100, "Cloak": bridge.CLOAK_CLOAKING}, {0: _fk(Rng=252)}), _pc, _cap)
check("player re-cloaking is not read as a decloak (replay t315)", _pc["player_cloaked_at"] == 294 and not any(x.cmd == bridge.CMD_FOCUS for x in _o), [bridge.describe(x) for x in _o])

import types as _ty
def _gate(mem):
    g = _ty.SimpleNamespace(mem=mem)
    for k in ("BRAIN_MAN_GAP", "BRAIN_WARP_GAP", "BRAIN_WARP_HULL", "BRAIN_BREAK_HULL"):
        setattr(g, k, getattr(director.Director, k))
    return g
def _bg(mem, orders, tick, flag):
    return director.Director._brain_gate(_gate(mem), orders, _ty.SimpleNamespace(tick=tick), flag, _cap)
_gm = {}
_o = _bg(_gm, [bridge.brk(10, 0)], 268, _fk(Hull=88, Man=bridge.CMD_BREAK))
check("brain break on top of a running break is dropped", not _o)
_o = _bg(_gm, [bridge.brk(10, 0)], 280, _fk(Hull=88))
check("a brain break with the hull sound becomes guard", [x.cmd for x in _o] == [bridge.CMD_GUARD], [bridge.describe(x) for x in _o])
_o = _bg(_gm, [bridge.brk(10, 0)], 295, _fk(Hull=50))
check("a second manoeuvre inside 30 ticks is dropped", not _o)
_o = _bg(_gm, [bridge.brk(10, 0)], 320, _fk(Hull=50))
check("a hurt hull may break for real once the gap has passed", [x.cmd for x in _o] == [bridge.CMD_BREAK])
_o = _bg({}, [bridge.warp(60, 0)], 178, _fk(Hull=100))
check("replay t178: warp 60 at 100% hull is dropped", not _o)
_wg = {}
_o = _bg(_wg, [bridge.warp(60, 0)], 394, _fk(Hull=17))
check("a hurt hull may warp out", [x.cmd for x in _o] == [bridge.CMD_WARP])
_o = _bg(_wg, [bridge.warp(45, 0)], 406, _fk(Hull=17))
check("...once - the repeat inside 90 ticks is dropped", not _o)
_o = _bg({}, [bridge.target("sensors", 0), bridge.brk(10, 0)], 256, _fk(Hull=88, Cloak=bridge.CLOAK_CLOAKING))
check("nothing that rebuilds the AI while the cloak ramps (replay t256)", not _o, [bridge.describe(x) for x in _o])
_o = _bg({"cloak_jam_until": 400}, [bridge.cloak(1, 0)], 300, _fk(Hull=50))
check("brain cloak-on refused while the cloak is jammed", not _o)
check("the gate counts what it held back", sum(_gm["gate_drops"].values()) == 3, _gm.get("gate_drops"))

class _W:
    seq = 0; sent = []
    def acked(self, snap): return True
    def send(self, orders, source="", tick=0): self.sent.append((source, [bridge.describe(o) for o in orders]))
_fd = _ty.SimpleNamespace(mem={"order_q": [("reflex", bridge.stance("defensive", 0)), ("reflex", bridge.cloak(1, 0)),
                                           ("brain", bridge.brk(10, 0)), ("reflex", bridge.repair(0))]},
                          writer=_W(), ORDER_BATCH=director.Director.ORDER_BATCH, ORDER_STALL_POLLS=99,
                          REBUILD_CMDS=director.Director.REBUILD_CMDS)
director.Director._flush_orders(_fd, _snap(256, {}, {0: _fk(Cloak=bridge.CLOAK_CLOAKING)}))
check("duel: a break queued mid-cloak waits for the ramp to settle",
      _fd.writer.sent and not any("break" in d for d in _fd.writer.sent[-1][1]) and any(o.cmd == bridge.CMD_BREAK for _s, o in _fd.mem["order_q"]),
      (_fd.writer.sent, [bridge.describe(o) for _s, o in _fd.mem["order_q"]]))
_fd.writer.sent = []
director.Director._flush_orders(_fd, _snap(260, {}, {0: _fk(Cloak=bridge.CLOAK_CLOAKED)}))
check("...and goes once the ship is dark", _fd.writer.sent and any("break" in d for d in _fd.writer.sent[-1][1]), _fd.writer.sent)
_bs = brain.SYSTEM_TEMPLATE + brain.SYSTEM_TEMPLATE_SILENT
check("brain doctrine no longer says break when a facing drops", "drops\n  below 25% - EARLY" not in _bs and _bs.count("job for guard") == 2 and "Sensors and engines are small parts" in _bs)
check("strategist told to default to hull + guard a facing", "Default to hull" in _insp.getsource(_strat.Strategist))

print("== the brain's cloak belongs to the playbook (2026-10-29) ==")
def _ca(mem, orders, tick, flag, prof=_cap):
    g = _ty.SimpleNamespace(mem=mem)
    return director.Director._cloak_authority(g, orders, _ty.SimpleNamespace(tick=tick), flag, prof)
_o = _ca({}, [bridge.cloak(1, 0)], 94, _fk(Hull=98, ShF=14, Rng=15))
check("replay t94: brain cloak at 98% hull, one facing at 14% -> dropped", not _o, [bridge.describe(x) for x in _o])
_o = _ca({}, [bridge.cloak(1, 0)], 244, _fk(Hull=82, ShF=3, Rng=102))
check("replay t244: brain cloak at 82% hull -> dropped", not _o, [bridge.describe(x) for x in _o])
_cm = {}
_o = _ca(_cm, [bridge.stance("balanced", 0), bridge.cloak(1, 0)], 300, _fk(Hull=45, Rng=200))
check("a hurt-hull brain cloak goes through as defensive + cloak + repair",
      [x.cmd for x in _o] == [bridge.CMD_STANCE, bridge.CMD_CLOAK, bridge.CMD_REPAIR] and "defensive" in bridge.describe(_o[0]),
      [bridge.describe(x) for x in _o])
check("...and the playbook owns it (escape phase, clock at the order)",
      _cm.get("cloak_phase") == "escape" and _cm.get("own_cloaked_at") == 300, _cm)
_o = _ca({"duel_cloak_play": "off"}, [bridge.cloak(1, 0)], 300, _fk(Hull=45))
check("the cloak learner's 'off' binds the brain too", not _o)
_o = _ca({"cloak_jam_until": 400}, [bridge.cloak(1, 0)], 300, _fk(Hull=45))
check("a jammed cloak gives the brain no phase to strand", not _o)
_am = {"ship_class": "Feklhr"}
_o = doctrine.cloak_reflex(_snap(102, {"Hull": 97}, {}), _am, _cap, _fk(Hull=95, ShF=15, Rng=39, Cloak=bridge.CLOAK_CLOAKED), 0, 102, play="alpha")
check("replay t102: a cloak nobody entered is adopted on a fresh clock",
      _am.get("cloak_phase") == "stalk" and _am.get("own_cloaked_at") == 102 and _am.get("cloak_hold_min") == doctrine.CLOAK_MEND_MIN_HOLD, _am)
_out = []
for _t in range(104, 140, 2):
    _out += [(_t, bridge.describe(x)) for x in doctrine.cloak_reflex(_snap(_t, {"Hull": 97}, {}), _am, _cap, _fk(Hull=95, ShF=30, Rng=40, Closing=1, Cloak=bridge.CLOAK_CLOAKED), 0, _t, play="alpha")]
_first = [t for t, d in _out if d.startswith("cloak @") or d == "cloak 0 @0"]
check("...holds the mend hold, then strikes (was: dark until t166)", _first and 102 + doctrine.CLOAK_MEND_MIN_HOLD <= _first[0] < 130, _out[:6])
_sm = {"ship_class": "Feklhr", "cloak_phase": "stalk", "own_cloaked_at": 175}
doctrine.cloak_reflex(_snap(200, {"Hull": 97}, {}), _sm, _cap, _fk(Hull=97, Rng=196), 0, 200, play="alpha")
check("replay t200: a stalk left over from t175 with her visible is cleared", not _sm.get("cloak_phase"), _sm)
check("...so the brain gate stops holding manoeuvres",
      [x.cmd for x in _bg(_sm, [bridge.guard(10, 0)], 210, _fk(Hull=97))] == [bridge.CMD_GUARD])
_lm = {"ship_class": "Feklhr", "cloak_phase": "stalk", "own_cloaked_at": 175}
doctrine.cloak_reflex(_snap(231, {"Hull": 99}, {}), _lm, _cap, _fk(Hull=99, Rng=64), 0, 231, play="alpha")
_o = doctrine.cloak_reflex(_snap(251, {"Hull": 75}, {}), _lm, _cap, _fk(Hull=75, ShF=5, Rng=101, Closing=1, Cloak=bridge.CLOAK_CLOAKED), 0, 251, play="alpha")
check("replay t251: going dark is not 'struck' off the t175 clock", not any(x.cmd == bridge.CMD_CLOAK for x in _o), [bridge.describe(x) for x in _o])
_rm = {"ship_class": "Feklhr", "cloak_phase": "escape", "own_cloaked_at": 300}
doctrine.cloak_reflex(_snap(305, {"Hull": 50}, {}), _rm, _cap, _fk(Hull=50, Rng=300), 0, 305, play="mend")
check("a cloak order still waiting on the engine is not cleared early", _rm.get("cloak_phase") == "escape", _rm)

print("== a collapsed facing no longer trips the in-game autobreak (2026-10-29) ==")
_abs = {k: v["autobreak"] for k, v in doctrine.PROFILES.items() if v.get("autobreak")}
check("every level arms autobreak with the facing trigger off", _abs and all(v[1] < 0 for v in _abs.values()), _abs)
check("...and keeps the hull + big-hit triggers", all(v[0] > 0 and v[2] > 0 for v in _abs.values()), _abs)
_ab = [o for o in doctrine.opening_orders(_cap, "Feklhr", "Feklhr") if o.cmd == bridge.CMD_AUTOBREAK]
check("captain opening sends '25,-1,18,12'", _ab and _ab[0].text == "25,-1,18,12", [bridge.describe(o) for o in _ab])
# ClaudeCommander.AutoBreakOne: fires on iFace <= abFace; WeakestFacing is 0..100 (101 = no shields)
_face = int(_ab[0].text.split(",")[1])
check("replay t71/112/157: facings at 0% and 8% no longer fire it", not any(f <= _face for f in (0, 8, 101)))

print("== she sees her drive and protects her aft (2026-10-29) ==")
_hs = intel.hull_shape("Feklhr")
check("Fek'lhr reads front-loaded from its hardpoints (fore 2.5x, 2 of 3 tubes forward)",
      _hs and _hs["front_heavy"] and _hs["front_ratio"] == 2.5 and _hs["fwd_tubes"] == 2, _hs)
_fed = [c for c, v in intel._db().items() if v.get("race") == "Federation" and (v.get("shields") or {}).get("front")]
_fh = [c for c in _fed if intel.hull_shape(c)["front_heavy"]]
check("...and most Federation hulls do not", len(_fh) * 10 < len(_fed), (len(_fh), len(_fed)))
check("her ship notes say fight nose-on and guard the impulse engines",
      "FRONT-LOADED HULL" in intel.own_ship_doctrine("Feklhr") and "impulse engines" in intel.own_ship_doctrine("Feklhr"))
check("drive state: one engine out = hit", doctrine.drive_state(_fk(Eng=31, EngOut=1, EngN=2)) == "hit")
check("slowed by the power split alone is NOT a hit", doctrine.drive_state(_fk(Eng=91, EngOut=0, EngN=2, ImpMax=53, ImpFull=98)) == "mended")
check("...and an engine at 75% with low top speed is neither hit nor mended", doctrine.drive_state(_fk(Eng=75, ImpMax=40, ImpFull=79)) == "")
check("...all engines back = mended; old telemetry (no children) at 100 = mended",
      doctrine.drive_state(_fk(Eng=90, EngOut=0, EngN=2, ImpMax=79, ImpFull=79)) == "mended" and doctrine.drive_state(_fk(Eng=100)) == "mended")
_dm = {"ship_class": "Feklhr", "hull_shape": _hs}
_o = doctrine.drive_reflex(_snap(170, {}, {}), _dm, _cap, _fk(Hull=59, Eng=0, EngOut=1, EngN=2, ShA=60), 0, 170)
_d = [bridge.describe(x) for x in _o]
check("replay t170: drive hit -> autobreak stood down + repairs + bow on him",
      any(x.cmd == bridge.CMD_AUTOBREAK and x.arg == 0 for x in _o) and any(x.cmd == bridge.CMD_REPAIR for x in _o)
      and any(x.cmd == bridge.CMD_GUARD for x in _o) and _dm.get("drive_note"), _d)
_o = doctrine.drive_reflex(_snap(171, {}, {}), _dm, _cap, _fk(Hull=59, Eng=0, EngOut=1, EngN=2, ShA=60), 0, 171)
check("...said once, not re-sent every poll", not any(x.cmd == bridge.CMD_AUTOBREAK for x in _o), [bridge.describe(x) for x in _o])
_o = doctrine.drive_reflex(_snap(260, {}, {}), _dm, _cap, _fk(Hull=59, Eng=90, EngOut=0, EngN=2), 0, 260)
check("mended -> autobreak re-armed with the profile's own values",
      any(x.cmd == bridge.CMD_AUTOBREAK and x.arg == 1 and x.text == "25,-1,18,12" for x in _o), [bridge.describe(x) for x in _o])
_o = doctrine.drive_reflex(_snap(300, {}, {}), {"hull_shape": _hs}, _cap, _fk(ShA=40, ShF=90), 0, 300)
check("front-loaded hull, aft at 40% -> guard turns the bow in", [x.cmd for x in _o] == [bridge.CMD_GUARD], [bridge.describe(x) for x in _o])
_o = doctrine.drive_reflex(_snap(300, {}, {}), {"hull_shape": intel.hull_shape(_fed[0])}, _cap, _fk(ShA=40), 0, 300)
check("...a balanced hull with an intact drive does not", not _o, [bridge.describe(x) for x in _o])
_o = doctrine.drive_reflex(_snap(300, {}, {}), {"hull_shape": _hs}, _cap, _fk(ShA=40, Cloak=bridge.CLOAK_CLOAKING), 0, 300)
check("...and never mid-cloak", not _o)
_o = _bg({"drive_hit": 170}, [bridge.brk(10, 0)], 172, _fk(Hull=30))
check("replay t172: brain break with the drive hit becomes guard even at 30% hull", [x.cmd for x in _o] == [bridge.CMD_GUARD], [bridge.describe(x) for x in _o])
_bd = _ty.SimpleNamespace(mem={"hull_shape": _hs, "drive_hit": 170})
_bl = director.Director._own_systems_lines(_bd, _fk(Hull=59, Eng=0, EngOut=1, EngN=2, ShA=30, Weap=100, Torp=100, Warp=100, Sens=100, Pow=100, ImpMax=30, ImpFull=79))
check("the brief now shows her own systems, engines out and top speed",
      "impulse 0% (1 of 2 engines out)" in _bl[0] and "top speed 3.0 of 7.9" in _bl[0], _bl)
check("...and tells her the drive is hit and the aft is open", any("IMPULSE DRIVE IS HIT" in l for l in _bl) and any("AFT shield is at 30%" in l for l in _bl), _bl)
_wt = _wk.Tracker()
_wt.see("flag", "Feklhr", 160, {"Hull": 66, "Eng": 45, "EngOut": 1})
check("weakness log counts a knocked-out child engine as engines down", "Eng" in _wt.ships["flag"]["down"], _wt.ships["flag"])
_ef = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game", "scripts", "Custom", "Autoload", "ClaudeCommander.py")
if os.path.exists(_ef):
    import ast as _ast
    _tree = _ast.parse(open(_ef).read())
    _mod = _ast.Module(body=[n for n in _tree.body if isinstance(n, _ast.FunctionDef) and n.name in ("ChildHealth", "Pct", "Health", "GetSubsystem", "ShipReport", "ImpulseKids", "EngineRepairCount")], type_ignores=[])
    _ns = {"string": _ty.SimpleNamespace(lower=str.lower, strip=str.strip), "ShieldFacings": lambda: [], "CloakCode": lambda p: 1}
    exec(compile(_mod, "engfix", "exec"), _ns)
    class _Sub:
        def __init__(s, c, kids=(), dis=False): s.c, s.kids, s.dis = c, list(kids), dis
        def GetConditionPercentage(s): return s.c
        def GetNumChildSubsystems(s): return len(s.kids)
        def GetChildSubsystem(s, i): return s.kids[i]
        def IsDisabled(s): return s.dis
        def GetMaxSpeed(s): return 3.2
        def GetProperty(s): return _ty.SimpleNamespace(GetMaxSpeed=lambda: 7.91)
    class _Ship:
        def GetHull(s): return _Sub(0.8)
        def GetShields(s): return None
        def GetPhaserSystem(s): return _Sub(1.0)
        def GetTorpedoSystem(s): return _Sub(1.0)
        def GetImpulseEngineSubsystem(s): return _Sub(1.0, [_Sub(0.31, dis=True), _Sub(0.92)])
        def GetWarpEngineSubsystem(s): return _Sub(1.0, [_Sub(1.0), _Sub(1.0)])
        def GetSensorSubsystem(s): return _Sub(1.0)
        def GetPowerSubsystem(s): return None
        def GetVelocityTG(s): return _ty.SimpleNamespace(Length=lambda: 2.04)
    _out = []
    _ns["ShipReport"]("S0", _Ship(), _out)
    _r = dict(_out)
    check("staged ClaudeCommander: parent 100%, Impulse 1 at 31% and out -> Eng 31, 1 of 2 out, speed 3.2/7.9",
          _r.get("S0Eng") == 31 and _r.get("S0EngOut") == 1 and _r.get("S0EngN") == 2 and _r.get("S0ImpMax") == 32 and _r.get("S0ImpFull") == 79, _r)

print("== no running on a dead drive; engine repair experiment (2026-10-29) ==")
_hd = _ty.SimpleNamespace(mem={"drive_hit": 87})
_o = director.Director._drive_hold(_hd, [bridge.stance("defensive", 0), bridge.cloak(1, 0), bridge.brk(12, 0), bridge.repair(0)], _fk(Hull=33, ImpMax=0, ImpFull=98))
check("replay t119: the escape cloak goes, its break 12 is held", [x.cmd for x in _o] == [bridge.CMD_STANCE, bridge.CMD_CLOAK, bridge.CMD_REPAIR], [bridge.describe(x) for x in _o])
_o = director.Director._drive_hold(_hd, [bridge.guard(6, 0), bridge.brk(0, 0)], _fk(ImpMax=0, ImpFull=98))
check("dead drive: guard held too, a break cancel still goes", [x.cmd for x in _o] == [bridge.CMD_BREAK] and _o[0].arg == 0, [bridge.describe(x) for x in _o])
_o = director.Director._drive_hold(_hd, [bridge.guard(6, 0)], _fk(ImpMax=48, ImpFull=98))
check("one engine left: guard still turns the bow in", [x.cmd for x in _o] == [bridge.CMD_GUARD])
check("...held orders are counted", _hd.mem["gate_drops"].get("breaks held (drive hit)") == 1 and _hd.mem["gate_drops"].get("manoeuvres held (drive dead)") == 1, _hd.mem.get("gate_drops"))
_o = director.Director._drive_hold(_ty.SimpleNamespace(mem={}), [bridge.brk(10, 0)], _fk())
check("an intact drive is not touched", [x.cmd for x in _o] == [bridge.CMD_BREAK])
_bl = director.Director._own_systems_lines(_ty.SimpleNamespace(mem={"drive_hit": 87, "hull_shape": _hs}), _fk(Eng=0, EngOut=2, EngN=2, EngRep=1, ImpMax=0, ImpFull=98))
check("dead drive brief: says it cannot move or turn, and shows the bay on the engines",
      any("DRIVE IS DEAD" in l for l in _bl) and "repair bay on 1" in _bl[0], _bl)
_rf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game", "scripts", "Custom", "Autoload", "ClaudeCommander.py")
if os.path.exists(_rf):
    _tree = _ast.parse(open(_rf).read())
    _names = ("ChildHealth", "Pct", "Health", "GetSubsystem", "ShipReport", "ImpulseKids", "EngineRepairCount", "QueueEngineRepairs")
    _ns = {"string": _ty.SimpleNamespace(lower=str.lower, strip=str.strip), "ShieldFacings": lambda: [], "CloakCode": lambda p: 1,
           "DebugPrint": lambda t: None}
    exec(compile(_ast.Module(body=[n for n in _tree.body if isinstance(n, _ast.FunctionDef) and n.name in _names], type_ignores=[]), "repairfix", "exec"), _ns)
    class _Rep(_Sub):
        def __init__(s): _Sub.__init__(s, 1.0); s.list = []
        def IsBeingRepaired(s, sub): return sub in s.list
        def AddToRepairList(s, sub): s.list.append(sub)
    _k1, _k2 = _Sub(0.0, dis=True), _Sub(1.0)
    _imp = _Sub(1.0, [_k1, _k2]); _rp = _Rep()
    class _Ship2(_Ship):
        def GetImpulseEngineSubsystem(s): return _imp
        def GetRepairSubsystem(s): return _rp
    _txt = _ns["QueueEngineRepairs"](_Ship2(), _rp)
    check("staged repair test: a dead engine goes on the list, a whole one does not", _rp.list == [_k1] and "queued 1" in _txt and "being repaired 1 of 2" in _txt, _txt)
    _txt = _ns["QueueEngineRepairs"](_Ship2(), _rp)
    check("...and is not queued twice", _rp.list == [_k1] and "queued 0" in _txt, _txt)
    _out = []; _ns["ShipReport"]("S0", _Ship2(), _out)
    check("...telemetry EngRep shows the bay on it", dict(_out).get("S0EngRep") == 1 and dict(_out).get("S0EngOut") == 1, dict(_out))

print("== cloak and dagger - her cloak is hers (2026-10-29) ==")
_pm = {"ship_class": "Feklhr", "player_cloaked_at": 473, "cloak_phase": "escape", "own_cloaked_at": 480, "reach": 174, "duel_cloak_play": "off"}
_o = doctrine.reflexes(_snap(485, {"Hull": 94, "Cloak": bridge.CLOAK_READY}, {0: _fk(Hull=47, Cloak=bridge.CLOAK_CLOAKED, Rng=281), 1: _fk(Type="Fighter")}), _pm, _cap)
check("replay t485: his decloak sends her no refocus/power while she is dark",
      not any(x.ship == 0 and (x.cmd == bridge.CMD_FOCUS or bridge.describe(x) == "power weapons @0") for x in _o) and not any(x.ship == -1 for x in _o),
      [bridge.describe(x) for x in _o])
check("...the wing is re-committed, and it is counted", any(x.cmd == bridge.CMD_FOCUS and x.ship == 1 for x in _o)
      and _pm["gate_drops"].get("his cloak/decloak reactions held while she was dark") == 1, [bridge.describe(x) for x in _o])
_pc2 = {"ship_class": "Feklhr", "cloak_phase": "escape", "own_cloaked_at": 143, "reach": 174, "duel_cloak_play": "off"}
_o = doctrine.reflexes(_snap(287, {"Hull": 100, "Cloak": bridge.CLOAK_CLOAKED}, {0: _fk(Hull=50, Cloak=bridge.CLOAK_CLOAKED, Rng=259)}), _pc2, _cap)
check("replay t287: his cloak sends her no ceasefire refocus while she is dark", not any(x.cmd in (bridge.CMD_FOCUS, bridge.CMD_BREAK) and x.ship in (0, -1) for x in _o) and _pc2.get("ceasefire_pending"),
      [bridge.describe(x) for x in _o])
_pc2["cloak_phase"] = None
_o = doctrine.reflexes(_snap(300, {"Hull": 100, "Cloak": bridge.CLOAK_CLOAKED}, {0: _fk(Hull=50, Cloak=bridge.CLOAK_READY, Rng=259)}), _pc2, _cap)
check("...the ceasefire lands once she is visible and he is still dark (fairness)", any(x.cmd == bridge.CMD_FOCUS and x.arg == bridge.FOCUS_RELEASE for x in _o) and not _pc2.get("ceasefire_pending"),
      [bridge.describe(x) for x in _o])
_pv = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "off"}
_o = doctrine.reflexes(_snap(287, {"Hull": 100, "Cloak": bridge.CLOAK_CLOAKED}, {0: _fk(Rng=259)}), _pv, _cap)
check("visible, his cloak still gets the immediate ceasefire", any(x.cmd == bridge.CMD_FOCUS and x.ship == -1 for x in _o), [bridge.describe(x) for x in _o])
_ms = {"ship_class": "Feklhr", "cloak_phase": "escape", "own_cloaked_at": 140}
_out = []
for _t in range(141, 200):
    _out += [(_t, bridge.describe(x)) for x in doctrine.cloak_reflex(_snap(_t, {"Hull": 90}, {}), _ms, _cap, _fk(Hull=45, Eng=0, EngOut=2, EngN=2, Rng=300, Cloak=bridge.CLOAK_CLOAKED), 0, _t, play="mend")]
_outs = [t for t, d in _out if d.startswith("cloak @") or d == "cloak 0 @0"]
check("engines stuck at 0% for 40 ticks while dark -> she comes out to fight", _outs and 181 <= _outs[0] <= 183 and "mend stalled" in (_ms.get("drive_note") or ""), (_outs[:2], _ms.get("drive_note")))
_mg = {"ship_class": "Feklhr", "cloak_phase": "escape", "own_cloaked_at": 140}
_out = []
for _t in range(141, 260):
    _out += [bridge.describe(x) for x in doctrine.cloak_reflex(_snap(_t, {"Hull": 90}, {}), _mg, _cap, _fk(Hull=45, Eng=min(85, 42 + (_t - 141) // 7), EngOut=0 if _t > 190 else 1, EngN=2, Rng=300, Cloak=bridge.CLOAK_CLOAKED), 0, _t, play="mend")]
check("...but a mend that is fixing the engines (42 -> 92) hides as long as it likes", not any(d.startswith("cloak @") or d == "cloak 0 @0" for d in _out) and _mg.get("cloak_phase") == "escape", _out[:4])
check("the duel passes its cloak events to the evaluator", "events=self.events" in _insp.getsource(director.Director._finish))

print("== 10-30: the record outranks the brains' guesses ==")
_p0 = _sb.DATA_PATH
_sb.DATA_PATH = os.path.join(_tf.mkdtemp(), "brains.json")
_dec = lambda *o: [{"tick": 1, "assessment": "", "orders": list(o), "rejected": []}]
check("manoeuvre count: break/circle/guard/warp count, break 0 and focus do not",
      _sb.manoeuvre_counts(_dec("A1 focus e1", "A1 break 0", "A1 break 15", "A1 circle 10", "A1 guard 12", "A1 warp 30", "A1 power weapons")) == (7, 4))
_rows = []
for _i in range(6):
    _cs = "red" if _i % 2 == 0 else "blue"
    _rows.append({"time": _i, "key": "k", "void": "", "claude_side": _cs, "winner_brain": "local",
                  "winner_side": "blue" if _cs == "red" else "red",
                  "ids": {"red": ["DefiantNX"], "blue": ["Feklhr"]}, "man": {"claude": 5, "local": 0}})
_sb._save({"rounds": _rows, "hull_lessons": [
    {"own": ["Feklhr"], "enemy": ["DefiantNX"], "engine": "claude", "result": "lost", "time": 9, "lesson": "Keep range and kite at beam reach."},
    {"own": ["Feklhr"], "enemy": ["DefiantNX"], "engine": "local", "result": "won", "time": 1, "lesson": "Focus the hull from the start."},
    {"own": ["Feklhr"], "enemy": ["DefiantNX"], "engine": "local", "result": "lost", "time": 2, "lesson": "Local loss note."}]})
_ev = _sb.evidence_for("Feklhr", "DefiantNX")
check("evidence: 6 rounds, the calmer commander won all 6; claude 0-3 and local 3-0 in the Fek'lhr",
      _ev and _ev["rounds"] == 6 and _ev["calm_won"] == 6 and _ev["flown"]["claude"] == [0, 3] and _ev["flown"]["local"] == [3, 0], _ev)
_ht = _sb.hull_lessons_text("Feklhr", "DefiantNX")
check("duel paragraph opens with the record, a win lesson outranks a newer loss lesson, local losses stay out",
      _ht.startswith("Sparring record") and _ht.index("Focus the hull") < _ht.index("Keep range") and "Local loss" not in _ht, _ht)
check("retire: a matching loss lesson stops being served", _sb.retire_hull_lessons("Feklhr", "DefiantNX", "kite", "test") == 1
      and "Keep range" not in _sb.hull_lessons_text("Feklhr", "DefiantNX"))
check("spar commanders get the record too", "THE RECORD" in _sb.system_prompt(["Feklhr"], ["DefiantNX"], ["old note"]))
check("fewer than EVIDENCE_MIN rounds anywhere -> no record line", _sb._save({"rounds": _rows[:2]}) is None and _sb.evidence_text("Feklhr", "DefiantNX") == "")
_sb.DATA_PATH = _p0
check("the reflection prompt reads the record", "THE RECORD" in _insp.getsource(_sb.SideCommander.reflect))
check("the director banks ids + manoeuvre counts with every brain round",
      "man=" in _insp.getsource(director.Director._spar_brains_finish) and "backfill_round_evidence" in _insp.getsource(director.Director.__init__))

print("== the stalk that could not move, and the wolfpack that never shot ==")
_wm = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "wolfpack"}
_o = doctrine.cloak_reflex(_snap(290, {"Hull": 87}, {}), _wm, _cap, _fk(Hull=68, Eng=56, EngOut=0, EngN=2, Rng=111, Cloak=bridge.CLOAK_CLOAKED, ShA=7), 0, 290)
check("replay t290: a cloak adopted with the drive hit is a mend, not a stalk", _wm.get("cloak_phase") == "escape", _wm.get("cloak_phase"))
_sm = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "wolfpack", "cloak_phase": "stalk", "own_cloaked_at": 290}
_o = doctrine.cloak_reflex(_snap(320, {"Hull": 90}, {}), _sm, _cap, _fk(Hull=74, Eng=90, Rng=313, Closing=1, Cloak=bridge.CLOAK_CLOAKED), 0, 320)
check("replay t468: no wolfpack decloak at 313m with a 174m reach", not any(bridge.describe(x).startswith("cloak 0") or (x.cmd == bridge.CMD_CLOAK and not x.arg) for x in _o) and _sm.get("cloak_phase") == "stalk", [bridge.describe(x) for x in _o])
_o = doctrine.cloak_reflex(_snap(321, {"Hull": 90}, {}), _sm, _cap, _fk(Hull=74, Eng=90, Rng=160, Closing=1, Cloak=bridge.CLOAK_CLOAKED), 0, 321)
check("...but it strikes at 160m", _sm.get("cloak_phase") is None and _sm.get("cloak_struck_at") == 321, [bridge.describe(x) for x in _o])
_rm = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "wolfpack", "cloak_struck_at": 488}
_o = doctrine.cloak_reflex(_snap(500, {"Hull": 87}, {}), _rm, _cap, _fk(Hull=73, Eng=90, Rng=148, Batt=99, Cloak=bridge.CLOAK_READY), 0, 500)
check("replay t500: no wolfpack re-cloak at 148m with him closing (shields down = his alpha)", not any(x.cmd == bridge.CMD_CLOAK and x.arg for x in _o), [bridge.describe(x) for x in _o])
_rm2 = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "wolfpack", "cloak_struck_at": 400}
for _t in range(401, 412):
    doctrine.cloak_reflex(_snap(_t, {"Hull": 87}, {}), _rm2, _cap, _fk(Hull=90, Eng=90, Rng=150, Batt=99, Cloak=bridge.CLOAK_READY), 0, _t)
_o = doctrine.cloak_reflex(_snap(415, {"Hull": 87}, {}), _rm2, _cap, _fk(Hull=90, Eng=90, Rng=300, Batt=99, Cloak=bridge.CLOAK_READY), 0, 415)
check("...a pass that fought in reach and opened to 300m may vanish again", any(x.cmd == bridge.CMD_CLOAK and x.arg for x in _o), [bridge.describe(x) for x in _o])
_dir = director.Director.__new__(director.Director); _dir.mem = {"drive_hit": 1}
_held = _dir._drive_hold([bridge.brk(10, 0)], {"Cloak": bridge.CLOAK_CLOAKED, "ImpMax": 40})
check("dark, a stalk's break to open range is not held by the drive hold", len(_held) == 1, _held)
check("...visible it still is", _dir._drive_hold([bridge.brk(10, 0)], {"Cloak": bridge.CLOAK_READY, "ImpMax": 40}) == [])

print("== no cloak on the approach inside his guns ==")
_am = {"ship_class": "Feklhr", "reach": 174, "duel_cloak_play": "wolfpack"}
_o = doctrine.cloak_reflex(_snap(55, {"Hull": 100}, {}), _am, _cap, _fk(Hull=100, Eng=100, Rng=278, Closing=20, Batt=100, Cloak=bridge.CLOAK_READY), 0, 55)
check("replay t55: healthy at 278m on the approach -> no ambush cloak (his torpedoes reach)", not any(x.cmd == bridge.CMD_CLOAK and x.arg for x in _o), [bridge.describe(x) for x in _o])
_o = doctrine.cloak_reflex(_snap(55, {"Hull": 100}, {}), dict(_am), _cap, _fk(Hull=100, Eng=100, Rng=600, Closing=20, Batt=100, Cloak=bridge.CLOAK_READY), 0, 55)
check("...from 600m (beyond the old 495m line) the ambush still sets up", any(x.cmd == bridge.CMD_CLOAK and x.arg for x in _o), [bridge.describe(x) for x in _o])

print("\n%d checks, %d failed%s" % (COUNT[0], len(FAILS), (": " + ", ".join(FAILS)) if FAILS else ""))
sys.exit(1 if FAILS else 0)
