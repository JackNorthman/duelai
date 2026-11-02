"""The director: owns the battle lifecycle and the chat log.

Phases:
  idle     - no link detected; browser can stage a battle plan
  armed    - link ticking, plan staged, waiting to spawn (or just spawned)
  fighting - flagship alive, orders flowing
  debrief  - battle over (flag dead / player dead / stood down)

The director thread polls the state file ~1.5s.  Reflexes run every poll;
the LLM runs on the profile's cadence via AsyncBrain (never blocks).
"""

import json
import os
import random
import re
import threading
import time

from . import bridge
from . import ally
from . import evaluate
from . import sparring
from . import strategist as strategist_mod, doctrine, intel, learning, lore
from . import online as online_mod, sparbrains, weakness
from . import cloaklearn
from .brain import Brain, AsyncBrain, to_bridge_orders
from .brain import OLLAMA_BASE


POLL_SECS = 1.5
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "logs")

# Comms discipline: minimum seconds between unprompted transmissions
# (replies to the player are always exempt), and how similar a line may
# be to recent ones before it is dropped as a rehash.
SAY_COOLDOWN = 20
SAY_SIMILARITY = 0.6
# qwen3 sometimes echoes the prompt's example turns verbatim - never
# let those reach the channel.
_EXEMPLAR_SAYS = {
    "you will not get a second pass",
    "your flank is open captain",
    "you cost me a warship captain two more answer for her",
}


def _say_norm(text):
    return "".join(c for c in text.lower()
                   if c.isalnum() or c == " ").strip()


def _say_sim(a, b):
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

PERSONAS = {
    "klingon": "Commander Korath, a Klingon warrior captain - honour-bound, "
               "booming, relishes a worthy foe",
    "romulan": "Commander Sela of the Romulan Star Empire - icy, precise, "
               "condescending, three moves ahead",
    "borg": "the voice of a Borg vessel - flat, plural, inevitable; "
            "speaks as We, calls the player Species 5618",
    "instructor": "Captain Reyes, a Starfleet Academy combat instructor - "
                  "encouraging but sharp, grades the player's flying",
    "federation": "Captain Vance of Starfleet - principled, calm under "
                  "fire, regrets the necessity but never the aim",
    "cardassian": "Gul Madred of the Cardassian Union - silky, patient, "
                  "savours the interrogation as much as the kill",
    "dominion": "a Vorta overseer speaking for the Dominion - polite, "
                "smiling menace; refers to their Jem'Hadar crew and the "
                "will of the Founders",
    "breen": "a Breen commander heard through a translator - clipped, "
             "mechanical bursts, never more than a few words, no emotion",
    "tholian": "a Tholian commandant - crystalline, alien, speaks of "
               "geometry, webs and inevitabilities",
    "gorn": "a Gorn captain - slow, heavy, reptilian patience; short "
            "sentences, sudden violence",
    "isc": "an ISC concordium commander - serene, sanctimonious, insists "
           "this pacification is for everyone's good",
    "kzinti": "a Kzinti warrior captain - feline predator, boastful, "
              "scents fear, speaks of the hunt and of heroes' names",
    "hydran": "a Hydran keeper - formal, tri-minded, speaks in plural "
              "deliberations and sudden consensus",
    "lyran": "a Lyran clan-lord - proud feline aristocrat, contemptuous "
             "of lesser bloodlines, duels for clan honour",
    "pirate": "an Orion pirate captain - swaggering, mercenary, will "
              "gladly loot the wreck and sell the escape pods",
    "xindi": "a Xindi-Reptilian commander - paranoid, zealous, certain "
             "this strike preserves their future",
    "kessok": "a Kessok commander - remote, resonant, few words, speaks "
              "as one who answers to older powers",
    "sona": "a Son'a ahdar - vain, acquisitive, faintly bored; considers "
            "this engagement beneath them and intends to win it quickly",
    "ferengi": "a Ferengi DaiMon - venal, quoting Rules of Acquisition, "
               "already calculating the salvage value of the wreck",
    "hirogen": "a Hirogen alpha - ritual hunter; the player is prey, the "
               "battle a hunt, the hull a trophy",
    "omega": "the master of an Omega Fleet hull - Starfleet officers flying "
             "ships that should not exist, powered by the one particle the "
             "Federation is sworn to destroy on sight. Clipped, procedural, "
             "faintly guilty; will not discuss where the power comes from "
             "and changes the subject if pressed",
    "temporal": "an officer of the 29th-century temporal fleet - unhurried "
                "and administrative, treats this century's tactics as a "
                "solved problem and this engagement as paperwork. Refuses "
                "absolutely to discuss the future, including who wins",
    "monster": "NOT a person: a spaceborne lifeform with no language and no "
               "interest in negotiation. It does not hail, boast, or answer. "
               "Say nothing at all - emit orders only, driven by hunger and "
               "threat",
    "godship": "the intelligence of a godship - ancient, vast and bored. "
               "Speaks rarely and in the singular, as something that has "
               "watched this species evolve and is unimpressed",
}

# ships.json race string -> persona key (era prefixes normalised away)
RACE_TO_PERSONA = {
    "federation": "federation", "starfleet": "federation",
    "klingon": "klingon", "romulan": "romulan", "borg": "borg",
    "dominion": "dominion", "cardassian": "cardassian", "breen": "breen",
    "tholian": "tholian", "gorn": "gorn", "isc": "isc", "kzinti": "kzinti",
    "hydran": "hydran", "lyran": "lyran", "pirate": "pirate",
    "xindi": "xindi", "kessok": "kessok", "sona": "sona",
    "ferengi": "ferengi", "delta quadrant": "hirogen",
    # Added 2026-09-05 with the rest of the QB race list. "monster" and
    # "godships" get a voice for completeness, but the monster persona's
    # instruction is to stay silent - a Crystalline Entity should not banter.
    "omega fleet": "omega", "future fleet": "temporal",
    "monster": "monster", "godships": "godship",
}


RACE_ADDRESS = {
    "borg": "Your opponent IS the Borg - a collective, not a person. "
            "Address them as the Collective or 'Borg', expect assimilation "
            "rhetoric, and never use personal names or honorifics.",
    "klingon": "Your opponent flies for the Klingon Empire - address them "
               "as a warrior; expect boasts and answer them in kind.",
    "romulan": "Your opponent flies for the Romulan Star Empire - expect "
               "guile; address them with wary formality.",
    "dominion": "Your opponent serves the Dominion - expect Vorta smarm "
                "or Jem'Hadar bluntness and address them accordingly.",
    "cardassian": "Your opponent flies for the Cardassian Union - expect "
                  "silk over steel.",
    "federation": "Your opponent flies a Federation vessel - address them "
                  "as Captain.",
    "omega": "Your opponent flies an Omega Fleet ship - Starfleet hulls "
             "running on omega particles. Address them as Captain, but the "
             "power source is the unspoken thing between you.",
    "temporal": "Your opponent is from the 29th century. Address them "
                "formally; expect to be told nothing useful about it.",
    "monster": "Your opponent is not a person and will not answer a hail. "
               "Do not address it at all - report it as a contact.",
    "godship": "Your opponent flies a godship. Nothing you say will matter "
               "to it; keep hails short and factual.",
}


def opponent_note(player_free_text, side=""):
    """Tell the persona WHO it is fighting, from the hull's actual race.

    Round 11 lesson: The player flew a Borg Cube and the commander kept
    addressing him by his old nickname - the prompt never said the
    opponent's race, and the learning file's history drowned the present.
    An explicit side declared at setup overrides the hull match."""
    race = ""
    for word in (side or "").lower().split():
        if word in RACE_ADDRESS or word in RACE_TO_PERSONA:
            race = word
            break
    if not race:
        race = (intel.player_ship_race(player_free_text) or "").lower()
    if not race:
        return ""
    base = ("TODAY'S OPPONENT: they fly %s - a %s vessel. Address them to "
            "match. Ignore old names or nicknames from past battles unless "
            "they still fit today's opponent."
            % (player_free_text.strip() or "an unknown ship", race))
    extra = RACE_ADDRESS.get(race)
    return base + ("\n" + extra if extra else "")


PERSONA_FLAG_NAME = {
    "klingon": "IKS Fable", "romulan": "IRW Fable",
    "federation": "USS Fable", "instructor": "USS Fable",
    "omega": "USS Fable", "temporal": "USS Fable",
}

# Dealer's choice normally takes the heaviest hull in the faction pool.  For
# a faction whose heaviest hull is a station or a carrier rather than a
# flagship, name the hull the persona should actually fly instead.
PERSONA_FLAG_CLASS = {
    # ZZOmegaVulture's own Credits line reads "USS Vulture - flagship of
    # Vulture's Omega Fleet", so the fleet names its own flagship; the
    # heaviest-hull default would tie it with the Phoenix at 70k anyway.
    "omega": "USS_Vulture",
}


def flagship_for_persona(persona):
    """Dealer's choice honours an explicit persona: draft the flagship
    from that faction's own heavy pool (round-15 report: borg persona
    still dealt a Neghvar)."""
    forced = PERSONA_FLAG_CLASS.get(persona)
    if forced:
        return forced, PERSONA_FLAG_NAME.get(persona, "")
    race = None
    for r, p in RACE_TO_PERSONA.items():
        if p == persona:
            race = r
            break
    if not race:
        return "", ""
    _escorts, heavies = intel.pools_for_race(race)
    if not heavies:
        return "", ""
    return heavies[0], PERSONA_FLAG_NAME.get(persona, "")


def persona_for_class(ship_class, ship_name=""):
    """Pick a commander persona from the hull's database race."""
    race = (intel.ship_race(ship_class) or "").lower()
    for prefix in ("tmp ", "ent "):
        if race.startswith(prefix):
            race = race[len(prefix):]
    key = RACE_TO_PERSONA.get(race)
    if key:
        return key
    prefix = ship_name.split()[0] if ship_name.split() else ""
    return {"IKS": "klingon", "IRW": "romulan",
            "USS": "federation"}.get(prefix, "klingon")


def _highest_logged_battle():
    try:
        best = 0
        for fn in os.listdir(LOG_DIR):
            m = re.match(r"(?:battle|spar)_\d+_(\d+)\.json$", fn)
            if m:
                best = max(best, int(m.group(1)))
        return best
    except OSError:
        return 0


class Director:
    def __init__(self, game_root=bridge.DEFAULT_GAME_ROOT,
                 model=None, banter=1):
        self.game_root = game_root
        self.phase = "idle"
        self.plan = None            # staged from the browser
        self.chat = []              # [{"i","t","who","text"}]
        self._chat_i = 0
        self.writer = bridge.OrderWriter(game_root)
        self.probe = bridge.LinkProbe(game_root)
        self.brain = Brain(model=model) if model else Brain()
        self.banter = 1 if banter else 0
        self.strategist = None      # set by configure_strategist()
        self.claude_plan = ""       # latest battle plan from the admiral
        self.api_cfg = {}           # key/model/effort for any Claude brain
        self.spar_brains = None     # brains sparring: {"red": .., "blue": ..}
        self._reflecting = 0        # post-round lessons still being written
        self.wk = weakness.Tracker()  # subsystem knockouts this fight
        try:                        # notes written before hull tagging
            sparbrains.backfill_hull_lessons()
        except Exception:
            pass
        try:                        # brain rounds banked before the record
            sparbrains.backfill_round_evidence(LOG_DIR)
        except Exception:
            pass
        try:                        # duel lessons: per-hull wording, once
            learning.backfill_lesson_hulls(LOG_DIR)
        except Exception:
            pass
        self.abrain = AsyncBrain(self.brain)
        self.mem = {}               # reflex memory
        self.hull_curve = []        # [(tick, flag_hull, player_hull)]
        self.track = []             # per-poll telemetry for post-battle eval
        self.events = []            # [(tick, kind)] cloaks/repairs/waves
        self.spar_track = []        # sparring: per-poll side aggregates
        self.debrief = None
        # Battle numbering survives server restarts via the learning file.
        # Seeded from the recorded count alone, a restart handed the next
        # fight the number of any unrecorded (stood-down) fight before it -
        # two battle_60 logs on 2026-09-09.  The log directory remembers.
        self.battle_no = max(learning.stats().get("battles", 0),
                             _highest_logged_battle())
        self._pending_user_line = None
        self._last_brain = 0.0
        self._last_snap = None
        self._prev = {}             # change detection for the brief
        self._lock = threading.RLock()
        self._stop = False
        os.makedirs(LOG_DIR, exist_ok=True)

    def configure_strategist(self, api_key, model, enabled, effort=None):
        """(Re)build the Claude strategist from settings. Live-safe."""
        if enabled and api_key:
            self.strategist = strategist_mod.Strategist(
                api_key, model, effort or strategist_mod.DEFAULT_EFFORT)
        else:
            self.strategist = None

    def configure_online(self, mode, api_key, model, effort):
        """Brain mode: "online" = Claude flies the tactical layer (local
        model as per-decision fallback), anything else = Ollama only.
        Live-safe: takes effect from the next decision; the key/model
        check runs on a thread so a settings save never blocks."""
        # brains sparring uses the same key/model/effort whatever the mode
        self.api_cfg = {"key": api_key or "", "model": model or
                        online_mod.DEFAULT_ONLINE_MODEL,
                        "effort": effort or online_mod.DEFAULT_ONLINE_EFFORT}
        if mode == "online" and api_key:
            ob = online_mod.OnlineBrain(api_key, model, effort)
            self.brain.set_online(ob)
            def check():
                ob.ping()
                self.brain._recompute_ok()
                if ob.ok:
                    self.say("system", "brain: ONLINE - %s at %s effort "
                             "(local model is the fallback)."
                             % (ob.model, ob.effort))
                else:
                    self.say("system", "brain: online check failed (%s) - "
                             "fighting on the local model."
                             % ob.last_error)
            threading.Thread(target=check, daemon=True).start()
        else:
            self.brain.set_online(None)

    def brain_mode(self):
        return "online" if self.brain.online is not None else "offline"

    def _player_class(self, plan=None):
        """Class id of the player's ship, or "" if it can't be resolved."""
        text = (plan or self.plan or {}).get("player_ship", "")
        if not text:
            return ""
        db = intel._db()
        if text in db:
            return text
        p = intel._resolve_player_ship(text)
        if not p:
            return ""
        for cid, d in db.items():
            if d is p:
                return cid
        for cid, d in db.items():
            if d.get("label") == p.get("label"):
                return cid
        return ""

    def _strategist_ctx_common(self, ship_class=None, player_class=None):
        s = learning.stats()
        ls = learning.duel_lessons(s.get("lessons2", []), ship_class,
                                   player_class)
        lessons = "\n".join("- %s (x%d)" % (l["text"], l.get("count", 1))
                             for l in ls[:8]) or "(none yet)"
        recent = "; ".join("%s in a %s (%s)"
                           % ("won" if b.get("won") else "lost",
                              b.get("ai_ship", "?"), b.get("difficulty", "?"))
                           for b in learning._load()["battles"][-4:]) or "none"
        table = sparring.table()
        spar = "\n".join("- %s: red %d / blue %d / draw %d, avg %d ticks"
                          % (k, v.get("red", 0), v.get("blue", 0),
                             v.get("draw", 0), v.get("avg_ticks", 0))
                          for k, v in sorted(table.items())[:8]) or "(none yet)"
        return lessons, recent, spar

    def _plan_async(self, ship_class, ship_name, difficulty,
                    player_ship, matchup):
        st = self.strategist
        if not st:
            return
        pclass = self._player_class({"player_ship": player_ship})

        def run():
            lessons, recent, spar = self._strategist_ctx_common(ship_class,
                                                                pclass)
            d = intel._db().get(ship_class, {})
            wp = intel.weapon_profile(ship_class) or {}
            text = st.plan({
                "own_ship": ship_class,
                "own_label": ship_name or d.get("label", ship_class),
                "player_ship": player_ship or "unknown",
                "difficulty": difficulty,
                "matchup": matchup or "no assessment",
                "guns": "\n".join(x for x in (
                    intel.weapon_profile_text(ship_class).lstrip("- "),
                    intel.hull_shape_text(ship_class)) if x) or "unknown",
                "reach": wp.get("reach", 0) or 350,
                "lessons": lessons, "sparring": spar, "recent": recent,
                "hull_lessons": sparbrains.hull_lessons_text(
                    ship_class, pclass),
                "weakness": "\n".join(x for x in (
                    weakness.text_for(pclass),
                    weakness.text_for(ship_class, own=True)) if x),
            })
            if text:
                self.claude_plan = text
                self.say("claude", "battle plan:\n" + text)
                # Opus 5.5 at high effort can land after the link is armed;
                # the brain was configured without it - hand it over now
                if self.phase in ("armed", "fighting") and \
                        "ADMIRAL'S BATTLE PLAN" not in self.brain.system:
                    self.brain.system += ("\n\nYOUR ADMIRAL'S BATTLE PLAN "
                                          "(follow unless the battle "
                                          "clearly contradicts it):\n" + text)
            elif st.last_error:
                self.say("system", "strategist unavailable (%s) - fighting "
                         "on local doctrine." % st.last_error)
        threading.Thread(target=run, daemon=True).start()

    def _debrief_async(self, result):
        st = self.strategist
        if not st or not self.debrief:
            return
        analysis = self.debrief.get("analysis", {})
        curve = self.hull_curve
        sc = self.debrief.get("ship_class", "")   # fixed now, not at reply
        step = max(1, len(curve) // 20)
        def run():
            lessons, recent, _ = self._strategist_ctx_common(
                (self.plan or {}).get("ship_class"), self._player_class())
            out = st.debrief({
                "result": result,
                "facts": "; ".join(analysis.get("facts", [])) or "none",
                "metrics": json.dumps(analysis.get("metrics", {}))[:400],
                "curve": [(c[0], c[1], c[2]) for c in curve[::step]][:24],
                "lessons": lessons, "recent": recent,
            })
            if out["analysis"]:
                self.say("claude", "debrief: " + out["analysis"])
            if out["lesson"]:
                res = learning.add_lesson(out["lesson"], topic="claude",
                                          ship_class=sc)
                if res == "new":
                    self.say("claude", "lesson banked: " + out["lesson"])
            if not out["analysis"] and st.last_error:
                self.say("system", "strategist debrief failed (%s)."
                         % st.last_error)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- chat api

    def say(self, who, text):
        with self._lock:
            self._chat_i += 1
            self.chat.append({"i": self._chat_i, "t": time.time(),
                              "who": who, "text": text})
            self.chat = self.chat[-400:]

    def chat_since(self, i):
        with self._lock:
            return [m for m in self.chat if m["i"] > i]

    def user_says(self, text):
        text = text.strip()[:300]
        if not text:
            return
        self.say("you", text)
        with self._lock:
            self._pending_user_line = text

    # ------------------------------------------------------------- plan api

    def stage_plan(self, difficulty, ship_class="", ship_name="",
                   persona="", era="", player_ship="", player_name="",
                   player_side="", player_allies="", scenario="", spawn=0):
        aspec = ally.parse_spec(scenario)
        if aspec is not None:
            if era and era != "auto" and not aspec.get("era"):
                aspec["era"] = era
            return self.stage_ally(aspec, player_ship)
        spec = sparring.parse_spec(scenario)
        if spec is not None:
            if era and era != "auto" and not spec.get("era"):
                spec["era"] = era
            return self.stage_spar(spec)
        if difficulty not in doctrine.PROFILES:
            difficulty = "officer"
        prof = doctrine.PROFILES[difficulty]
        if era in ("", "auto"):
            era = ""
        if not ship_class:
            # the era capsule, then the written scenario, outrank the
            # static defaults when drafting dealer's choice
            ship_class, dc_race = intel.dealer_choice(
                player_ship, player_side, scenario, persona=persona,
                era_pick=era, difficulty=difficulty,
                no_carrier=doctrine.duel_pure({
                    "scenario": scenario, "player_allies": player_allies}))
            if ship_class and not ship_name:
                p = persona or persona_for_class(ship_class)
                ship_name = PERSONA_FLAG_NAME.get(p, "")
        if not ship_class and persona:
            ship_class, faction_name = flagship_for_persona(persona)
            ship_name = ship_name or faction_name
        if not ship_class:
            ship_class, default_name = doctrine.DEFAULT_SHIPS[difficulty]
            ship_name = ship_name or default_name
        if not ship_name:
            # No naming convention for this faction (Borg etc.): the class
            # name doubles as the display name, which _find_flag matches.
            ship_name = ship_class
        if not persona:
            persona = persona_for_class(ship_class, ship_name)
        with self._lock:
            self.plan = {
                "difficulty": difficulty,
                "ship_class": ship_class,
                "ship_name": ship_name,
                "persona": persona,
                "era": era,
                "player_ship": player_ship,
                "player_name": player_name[:60],
                "player_side": player_side[:40],
                "player_allies": player_allies[:300],
                "scenario": scenario[:500],
                "spawn": max(150, min(20000, int(spawn))) if spawn else 0,
                "staged_at": time.time(),
            }
            self.debrief = None
            # Staging a new battle implies leaving the last one's debrief -
            # round 6 stalled because only the "run again" button did this.
            if self.phase == "debrief":
                self.phase = "idle"
                self.probe = bridge.LinkProbe(self.game_root)
        self.say("system", "battle plan staged: %s (%s) at %s difficulty. "
                 "arm the link in-game when ready."
                 % (ship_name, ship_class, prof["label"]))
        note = intel.matchup_note(ship_class, player_ship,
                                  difficulty=difficulty,
                                  escorts=prof.get("escorts", 0))
        if note:
            self.say("system", note)
        elif player_ship and not intel._resolve_player_ship(player_ship):
            self.say("system", "could not identify your ship %r in the "
                     "database - no matchup assessment possible. Check the "
                     "spelling, or use the class name (e.g. 'Voyager J')."
                     % player_ship[:40])
        self.claude_plan = ""
        self._plan_async(ship_class, ship_name, difficulty,
                         player_ship, note)
        lore_hits = lore.matches(self.plan)
        if lore_hits:
            self.say("system", "lore archive: pulled \"%s\" for the "
                     "commander's briefing." % "\", \"".join(lore_hits))
        return self.plan

    def stand_down(self):
        with self._lock:
            if self.phase in ("armed", "fighting"):
                self.writer.send([bridge.end()])
                if (self.plan or {}).get("mode") == "ally":
                    self._finish_ally("aborted", "stood down")
                else:
                    self._finish("stood down")
            self.plan = None
            self.spar_brains = None
            if self.phase == "idle":
                self.say("system", "plan cleared.")

    # ------------------------------------------------------------ main loop

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop = True

    def _loop(self):
        self.brain.ping()
        if not self.brain.ok:
            self.say("system", "warning: ollama is not answering on "
                     "%s - the opponent will fight on doctrine "
                     "alone and stay silent." % OLLAMA_BASE)
        elif self.brain.local_ok is False:
            self.say("system", "warning: ollama is not answering - the "
                     "online brain has no local fallback this session.")
        while not self._stop:
            try:
                self._tick()
            except Exception as e:
                self.say("system", "director error: %r" % e)
                time.sleep(3)
            time.sleep(POLL_SECS)

    def _tick(self):
        snap = bridge.snapshot(self.game_root)
        self._last_snap = snap
        link_up = self.probe.alive(snap)

        spar = bool(self.plan and self.plan.get("mode") == "spar")
        allied = bool(self.plan and self.plan.get("mode") == "ally")
        if self.phase == "idle":
            if link_up and self.plan:
                if allied:
                    # same leftovers problem as sparring: clear the board first
                    self.writer.resync()
                    self.writer.send([bridge.reap_fleet()])
                    self.phase = "ally_cleanup"
                    self.mem = {"ally": True, "cleanup_polls": 0}
                elif spar:
                    # Clear ANY leftovers first - survivors from a previous
                    # spar or duel persist in the mission (END never
                    # despawns; the player had to god-mode two of them away).
                    self.writer.resync()
                    self.writer.send([bridge.reap_fleet()])
                    self.phase = "spar_cleanup"
                    self.mem = {"spar": True, "cleanup_polls": 0}
                else:
                    self._arm(snap)
            return
        if spar:
            self._tick_spar(snap, link_up)
            return
        if allied:
            self._tick_ally(snap, link_up)
            return

        if self.phase == "armed":
            if not link_up:
                return
            slot = self._find_flag(snap)
            if slot is not None:
                self.mem["flag"] = slot
                self.phase = "fighting"
                self.say("system", "contact - %s is on the board."
                         % self.plan["ship_name"])
                return
            # Silent-drop guard (the NX duel that never started): the
            # game ignores any batch whose Seq it already consumed - its
            # counter lives in RAM while the on-disk ack can lag after a
            # quit - so an armed, ticking link with an empty fleet means
            # the spawn batch vanished. Re-send it with a fresh Seq.
            polls = self.mem.get("armed_polls", 0) + 1
            self.mem["armed_polls"] = polls
            if polls in (8, 24, 60) and not snap.live_fleet() and \
                    self.mem.get("opening_orders"):
                self.writer.seq = max(self.writer.seq, snap.ack_seq)
                seq = self.writer.send(self.mem["opening_orders"])
                self.say("system", "spawn unconfirmed - re-sending the "
                         "battle plan (batch %d)." % seq)
            return

        if self.phase == "fighting":
            # A torn read (file caught mid-rewrite) comes back empty - skip
            # the poll rather than mistake it for a dead game (round 9 was
            # ended 12 ticks in by exactly that).
            if snap.tick == 0 and not snap.fleet:
                return
            # Player death STOPS the game-side link (stand-down), and the
            # merge-only cfg leaves YouAlive stale at 1 - so the death
            # evidence (hull 0 / mission status 4) must be checked before
            # the stall logic, or a victory reads as a pause forever.
            if snap.raw.get("YouHull", 100) == 0 or \
                    snap.raw.get("Status", 0) == 4:
                self._finish("victory - the player's ship was destroyed")
                return
            # Tick going backwards = the game process was restarted.
            if snap.tick < self.mem.get("last_tick", 0) - 5:
                self._finish("game restarted mid-battle")
                return
            self.mem["last_tick"] = snap.tick
            if not link_up:
                stall = self.mem.get("stall_polls", 0) + 1
                self.mem["stall_polls"] = stall
                if stall == 8:
                    self.say("system", "telemetry stalled - game paused or "
                             "loading. standing by.")
                if stall > 200:     # ~5 minutes of true silence
                    self._finish("link lost (game closed or crashed)")
                return
            self.mem["stall_polls"] = 0
            self._fight(snap)
            return

    # ------------------------------------------------------------ sparring

    def stage_spar(self, spec):
        """Stage a self-play round set: RED vs BLUE, stock AI both sides."""
        scen = sparring.build_scenario(spec or {})
        if isinstance(scen, str):
            self.say("system", "sparring: " + scen)
            return None
        if scen.get("brains"):
            if not self.api_cfg.get("key"):
                self.say("system", "brains sparring needs a Claude api key "
                         "on the settings page.")
                return None
            if not sparbrains.local_ok():
                self.say("system", "brains sparring needs the local model "
                         "too, and ollama is not answering on "
                         "%s." % OLLAMA_BASE)
                return None
            model = self.brain.model

            def warm():
                secs = sparbrains.warm_local(model)
                self.say("system", ("local model %s loaded and warm (%.0fs)."
                                    % (model, secs)) if secs >= 0 else
                         "local model %s did not answer the warm-up - its "
                         "first calls may time out." % model)
            threading.Thread(target=warm, daemon=True).start()
        with self._lock:
            self.plan = {
                "mode": "spar",
                "difficulty": "holodeck",      # no doctrine interference
                "ship_class": scen["red"][0][0],
                "ship_name": scen["red"][0][1],
                "persona": "",
                "era": spec.get("era", ""),
                "scenario": scen["key"],
                "spar": scen,
                "spar_rounds_left": spec.get("rounds", 1),
                "staged_at": time.time(),
            }
            self.debrief = None
            if self.phase == "debrief":
                self.phase = "idle"
                self.probe = bridge.LinkProbe(self.game_root)
        self.say("system",
                 "sparring staged: %s - %d round%s. arm the link in-game "
                 "and observe; both sides fight under the game's own AI."
                 % (scen["key"], self.plan["spar_rounds_left"],
                    "" if self.plan["spar_rounds_left"] == 1 else "s"))
        if scen.get("brains"):
            self.say("system",
                     "brains sparring: Claude (%s, %s effort) vs the local "
                     "model (%s) - %s. Each side's commander decides every "
                     "%ds on top of the stock AI; hulls stay the same every "
                     "round.%s"
                     % (self.api_cfg["model"], self.api_cfg["effort"],
                        self.brain.model,
                        "Claude swaps sides every round" if
                        scen["brains"] == "swap" else
                        "Claude commands %s" % scen["brains"],
                        int(sparbrains.CADENCE),
                        " An even number of rounds gives each brain each "
                        "draft once." if scen["brains"] == "swap" and
                        self.plan["spar_rounds_left"] % 2 else ""))
        if scen.get("cloak"):
            self.say("system",
                     "cloak learning on for %s: every cloak-capable hull there "
                     "fights one cloak play per round (off / mend / alpha / "
                     "wolfpack), and the result is banked against its class."
                     % ("both sides" if scen["cloak"] == "both"
                        else scen["cloak"]))
        return self.plan

    def _arm_spar(self, snap):
        self.writer.resync()
        self.mem = {"spar": True}
        scen = self.plan["spar"]
        orders = []
        # Must precede the spawns - order 17 only affects the NEXT spawn.
        # ALWAYS sent: the game keeps the last value, so a set with no "@"
        # inherited whatever the previous set used (09-10: the Defiant set was
        # keyed as the 5000 default and fought at a pirate set's @400).
        orders.append(bridge.spawn_range(scen.get("spawn")
                                         or sparring.DEFAULT_SPAWN))
        for cid, name in scen["red"]:
            orders.append(bridge.spawn(intel.spawn_name(cid), name, side=0))
        for cid, name in scen["blue"]:
            orders.append(bridge.spawn(intel.spawn_name(cid), name, side=1))
        self.writer.send(orders, source="spar")
        self.writer.mark = self.writer.seq - 1
        self.mem["opening_orders"] = orders
        self.wk = weakness.Tracker()
        self._spar_brains_arm(scen)
        self.mem["armed_polls"] = 0
        self.mem["spar_hits0"] = snap.raw.get("PlayerHits", 0)
        self.battle_no += 1
        self.hull_curve = []
        self.track = []
        self.events = []
        self.spar_track = []
        self._prev = {}
        self.debrief = None
        self.phase = "armed"
        self._last_brain = time.time()
        self.say("system", "link armed - fielding %d red vs %d blue%s."
                 % (len(scen["red"]), len(scen["blue"]),
                    ", %dm apart" % scen["spawn"] if scen.get("spawn") else ""))

    def _spar_sides(self, snap):
        red = [s for s in snap.live_fleet().values()
               if s.get("Side", 0) == 0]
        blue = [s for s in snap.live_fleet().values()
                if s.get("Side", 0) == 1]
        return red, blue

    # Every order a poll produces goes through ONE queue and leaves as ONE
    # batch, never over the top of a batch the game has not consumed.  The game
    # runs only the newest Seq it finds (ProcessOrders: `if iSeq <= g_iOrders:
    # return`), so two sends in one poll silently lost the first.  Sparring
    # sent focus then stance and cost all three RED Defiants of 2026-09-10
    # their spar focus; a duel poll sent its reflex orders and then the LLM's,
    # so every poll where a decision landed dropped that poll's reflexes.
    ORDER_BATCH = 16            # ProcessOrders caps a batch at 16 orders
    ORDER_STALL_POLLS = 6       # unacked this long: stop waiting for it

    def _queue_orders(self, orders, source="spar"):
        if orders:
            self.mem.setdefault("order_q", []).extend(
                (source, o) for o in orders)

    REBUILD_CMDS = (bridge.CMD_STANCE, bridge.CMD_TARGET, bridge.CMD_FOCUS,
                    bridge.CMD_BREAK, bridge.CMD_CIRCLE, bridge.CMD_GUARD,
                    bridge.CMD_WARP)

    def _flush_orders(self, snap):
        q = self.mem.get("order_q")
        if not q:
            return
        if not self.writer.acked(snap):
            waited = self.mem.get("order_q_wait", 0) + 1
            self.mem["order_q_wait"] = waited
            if waited < self.ORDER_STALL_POLLS:
                return
            # Never acked (a failed write, a missed read): send on top of it
            # rather than stall every order queued behind it.
            self.writer.seq = max(self.writer.seq, snap.ack_seq)
        self.mem["order_q_wait"] = 0
        # An AI rebuild (stance/target/focus, and any manoeuvre) for a ship
        # that is mid-cloak or mid-decloak waits for the transition to
        # settle, and a spar focus that the same queue's stance order would
        # repeat is dropped: one rebuild, not two.  The one crash of the
        # 09-10 cloak sets came four seconds after decloak + stance + focus
        # landed on one ship in a tick.  This was sparring-only ("duel cloak
        # timing is proven") until one logged duel: nine duel cloaks went CLOAKING ->
        # DECLOAKING, and in every one a break, target or guard had landed
        # during the ramp.  The game's own warp break refuses mid-cloak for
        # the same reason ("cloak changing - reissue in a moment").
        stanced = set(o.ship for src, o in q
                      if src.startswith("spar") and o.cmd == bridge.CMD_STANCE)
        keep, held = [], []
        for src, o in q:
            if o.ship >= 0 and o.cmd in self.REBUILD_CMDS:
                slot = snap.fleet.get(o.ship, {})
                if slot.get("Cloak") in (bridge.CLOAK_CLOAKING,
                                         bridge.CLOAK_DECLOAKING):
                    held.append((src, o))
                    continue
            if src.startswith("spar") and o.ship >= 0 and o.cmd in (
                    bridge.CMD_STANCE, bridge.CMD_TARGET, bridge.CMD_FOCUS):
                slot = snap.fleet.get(o.ship, {})
                if (o.cmd == bridge.CMD_FOCUS and o.arg == 9
                        and o.ship in stanced and slot.get("Focus") == 9):
                    continue
            keep.append((src, o))
        batch = keep[:self.ORDER_BATCH]
        self.mem["order_q"] = keep[self.ORDER_BATCH:] + held
        if not batch:
            return
        sources = []
        for src, _o in batch:
            if src not in sources:
                sources.append(src)
        self.writer.send([o for _s, o in batch], source="+".join(sources),
                         tick=snap.tick)

    def _spar_focus_new(self, snap):
        """Spar focus for every not-yet-focused ship, BOTH sides.  Returns
        the orders.

        Runs every poll from the moment the link arms - round 1 failed
        with red on default targeting because blue never spawned and the
        old code only focused red at the both-sides-up transition.  Blue
        used to be skipped (it never targets the player anyway), but spar
        focus is also what points a ship's Rng/Closing/Brg and its
        break/circle/guard at the opposing fleet, and a cloak play needs
        exactly that - so both sides get it."""
        focused = self.mem.setdefault("spar_focused", [])
        orders = []
        for i, s in snap.live_fleet().items():
            if i not in focused:
                orders.append(bridge.focus("spar", i))
                focused.append(i)
        return orders

    def _spar_stance_new(self, snap):
        """Give every newly-spawned spar ship its stance, once.

        RegisterFleetShip hardcodes "balanced" on everything it slots, which is
        the 0.7 flag band - SmartPhasers off.  That is why no spar we have ever
        run could answer a beam-dwell question: the flag was dark in all of
        them.  ApplyStance rebuilds the AI at runtime, so re-issuing per slot
        after the spawn is enough and the game script needs no change.

        A stance forced on the side ("red=aggressive") wins; otherwise the hull
        chooses from its own weapon profile via intel.preferred_stance."""
        scen = self.plan.get("spar", {})
        done = self.mem.setdefault("spar_stanced", [])
        orders, notes = [], []
        for i, s in snap.live_fleet().items():
            if i in done:
                continue
            side_key = "red" if s.get("Side", 0) == 0 else "blue"
            forced = scen.get("stance_" + side_key, "")
            cid = s.get("Type", "")
            if forced and forced != "auto":
                stance, why = forced, "forced"
            else:
                stance, why = intel.preferred_stance(cid)
            orders.append(doctrine.stance_order(stance, cid, i))
            self.mem.setdefault("spar_stance_of", {})[i] = stance
            done.append(i)
            notes.append("%s %s -> %s (%s)"
                         % (side_key.upper(), cid or "?", stance, why))
        if notes:
            self.say("system", "stance: " + "; ".join(notes))
        return orders

    def _spar_cloak(self, snap):
        """cloak=red|blue|both.  Returns the orders.

        1. Honesty, always: the engine's fire scripts keep tracking a target
           that cloaks mid-engagement (visibility is stubbed), so when any
           ship goes dark or comes back, every ship across from it gets its
           spar focus re-issued - an AI rebuild, and a fresh target selection
           cannot see a cloaked ship.  Ships that are themselves cloaking or
           mid-manoeuvre are left alone: a rebuild would drop their own cloak
           (CloakShip uncloaks when its AI loses focus).
        2. Each cloak-capable hull on a learning side runs doctrine's
           cloak_reflex under the play cloaklearn picked for it this round.
        """
        scen = self.plan.get("spar", {})
        sides = {"red": (0,), "blue": (1,), "both": (0, 1)}.get(
            scen.get("cloak", ""), ())
        live = snap.live_fleet()
        tick = snap.tick
        arms = self.mem.setdefault("cloak_arm", {})
        orders, refocus = [], set()

        prev = self.mem.setdefault("cloak_state", {})
        for j, s in live.items():
            st = s.get("Cloak", bridge.CLOAK_NO_DEVICE)
            was = prev.get(j, st)
            prev[j] = st
            dark = st == bridge.CLOAK_CLOAKED
            if dark == (was == bridge.CLOAK_CLOAKED):
                continue
            if dark and j in arms:
                arms[j]["cloaks"] += 1
            self.say("system", "%s %s." % (
                s.get("Name", "S%d" % j),
                ("goes dark (%s)" % arms[j]["play"]) if dark and j in arms
                else "goes dark" if dark else "decloaks"))
            for i, o in live.items():
                if (o.get("Side", 0) != s.get("Side", 0)
                        and o.get("Cloak", 0) not in (bridge.CLOAK_CLOAKING,
                                                      bridge.CLOAK_CLOAKED,
                                                      bridge.CLOAK_DECLOAKING)
                        and not o.get("Man", 0)):
                    refocus.add(i)
        orders.extend(bridge.focus("spar", i) for i in sorted(refocus))
        if self.spar_brains:
            for cm in self.spar_brains.values():
                for i in refocus:
                    if i in cm.view.own:
                        cm.view.focus_of[i] = "nearest"
            sides = ()          # the commanders own the cloak

        if not sides:
            return orders
        cmems = self.mem.setdefault("cloak_mem", {})
        stance_of = self.mem.get("spar_stance_of", {})
        for i, s in live.items():
            if (s.get("Side", 0) not in sides or
                    s.get("Cloak", bridge.CLOAK_NO_DEVICE) ==
                    bridge.CLOAK_NO_DEVICE):
                continue
            cid = s.get("Type", "")
            if i not in arms:
                play, why = cloaklearn.choose(cid)
                arms[i] = {"play": play, "hull": cid,
                           "name": s.get("Name", "S%d" % i),
                           "side": "red" if s.get("Side", 0) == 0 else "blue",
                           "cloaks": 0}
                cmems[i] = {"ship_class": cid, "power": "balanced",
                            "power_tick": -9999}
                self.say("system", "cloak learner: %s (%s) plays %s - %s."
                         % (arms[i]["name"], cid, play, why))
            cm = cmems[i]
            cm["restore_stance"] = stance_of.get(i, "")
            view = dict(s)
            j = bridge.spar_target(s)
            hidden = (j is not None and
                      live.get(j, {}).get("Cloak") == bridge.CLOAK_CLOAKED)
            if hidden:
                view["Rng"], view["Closing"] = -1, 0
            out = doctrine.cloak_reflex(
                snap, cm, doctrine.SPAR_CLOAK_PROFILE, view, i, tick,
                play=arms[i]["play"], spar=True, enemy_hidden=hidden)
            for o in out:
                if o.cmd == bridge.CMD_CLOAK and o.arg == 1:
                    # DoCloak moves power to shields + repair by itself
                    cm["power"], cm["power_tick"] = "shields", tick
            if (not out and s.get("Cloak") == bridge.CLOAK_READY
                    and not cm.get("cloak_phase")
                    and cm.get("power") != "balanced"
                    and tick - cm.get("power_tick", -9999) > 20):
                # Out of the play: back to the grid the other side fights on.
                doctrine.set_power(out, cm, doctrine.SPAR_CLOAK_PROFILE, i,
                                   "balanced", tick, True)
            orders.extend(out)
        return orders

    SPAR_LINK_GRACE = 80        # polls (~2 min): a paused game is not a
                                # dead link - round 42 drew as "link lost"
                                # because the player alt-tabbed for 7 seconds.

    def _spar_link_ok(self, link_up):
        """True while the link is up or within the pause grace window."""
        if link_up:
            self.mem["link_down_polls"] = 0
            return True
        n = self.mem.get("link_down_polls", 0) + 1
        self.mem["link_down_polls"] = n
        if n == 4:
            self.say("system", "telemetry stalled (game paused?) - holding "
                     "the round open.")
        return n < self.SPAR_LINK_GRACE

    def _tick_spar(self, snap, link_up):
        if self.phase == "spar_cleanup":
            # Between rounds: the end order reaps the old fleets; spawning
            # before that finishes stacked duplicate fleets (round 43 ran
            # 4v4).  Wait for an empty board, then arm the next round.
            n = self.mem.get("cleanup_polls", 0) + 1
            self.mem["cleanup_polls"] = n
            # ...and for the reap itself to be consumed: arming on an already
            # empty board overwrote the reap batch before the game read it.
            if (not snap.live_fleet() and self.writer.acked(snap)) or n >= 10:
                # brains set: let both commanders finish writing their
                # lesson so the next round starts with it (cap ~30 s)
                if self._reflecting and n < 22:
                    return
                self._arm_spar(snap)
            return
        if self.phase == "armed":
            if not self._spar_link_ok(link_up):
                self._finish_spar("draw", "link lost while arming")
                return
            if not link_up:
                return
            self._queue_orders(self._spar_focus_new(snap))
            self._queue_orders(self._spar_stance_new(snap))
            red, blue = self._spar_sides(snap)
            if red and blue:
                self.phase = "fighting"
                self.say("system", "both fleets on the board - both sides "
                         "are ordered to ignore your ship. observing.")
                self._flush_orders(snap)
                return
            polls = self.mem.get("armed_polls", 0) + 1
            self.mem["armed_polls"] = polls
            scen = self.plan.get("spar", {})
            if polls in (8, 14):
                # Re-spawn ONLY the shortfall per side - a wholesale batch
                # resend would duplicate the ships that made it.
                short = []
                for side_key, side_num, live in (("red", 0, red),
                                                 ("blue", 1, blue)):
                    want = scen.get(side_key, [])
                    for cid, name in want[len(live):]:
                        short.append(bridge.spawn(intel.spawn_name(cid),
                                                  name, side=side_num))
                if short:
                    self.say("system", "%d ship(s) missing - re-spawning "
                             "the shortfall." % len(short))
                    self._queue_orders(short)
            if polls >= 22 and (not red or not blue):
                self._finish_spar("draw", "spawn failure - a side never "
                                  "materialised (check the log for "
                                  "'spawn FAILED' classes)")
                return
            self._flush_orders(snap)
            return
        if self.phase != "fighting":
            return
        if not self._spar_link_ok(link_up):
            self._finish_spar("draw", "link lost")
            return
        if not link_up:
            return          # paused: hold position, sample nothing
        self._queue_orders(self._spar_focus_new(snap))
        self._queue_orders(self._spar_stance_new(snap))
        self._queue_orders(self._spar_cloak(snap))
        self._spar_brains_tick(snap)
        # Taint detection: any weapon hit the player lands on a fleet ship
        # marks the round - it still runs and logs, but never reaches the
        # matchup table or the lesson store.
        hits = snap.raw.get("PlayerHits", 0) - self.mem.get("spar_hits0", 0)
        if hits > 0 and not self.mem.get("spar_tainted"):
            self.mem["spar_tainted"] = 1
            self.say("system", "divine intervention detected (%d player "
                     "hit%s) - this round is quarantined from the "
                     "sparring record." % (hits, "" if hits == 1 else "s"))
        red, blue = self._spar_sides(snap)
        for i, s in snap.fleet.items():
            self.wk.see(("s", i, s.get("Name", "")), s.get("Type", ""),
                        snap.tick, s, alive=s["st"] == bridge.SLOT_ALIVE)
        # The Q loophole: god-mode REPAIRS fire no weapons.  Crew repair is
        # slow; a side's average hull JUMPING between polls (~1.5s) is not
        # achievable naturally.  (Must run after red/blue are bound and
        # before this poll's sample is appended - v1 ran before the
        # assignment and threw UnboundLocalError every tick.)
        if self.spar_track and not self.mem.get("spar_tainted"):
            prevs = self.spar_track[-1]
            for side, ships in (("red", red), ("blue", blue)):
                if not ships:
                    continue
                cur = sum(s.get("Hull", 100) for s in ships) // len(ships)
                jump = cur - prevs.get(side + "_hull", cur)
                if jump >= 8 and prevs.get(side + "_alive", 0) == len(ships):
                    self.mem["spar_tainted"] = 1
                    self.say("system", "implausible repair on %s (+%d%% "
                             "hull in one tick) - Q is on deck. Round "
                             "quarantined." % (side, jump))
                    break
        def side_avg(side):
            return (sum(s.get("Hull", 0) for s in side) // len(side))                 if side else 0
        def side_sh(side, key):
            # A ship in any cloak state has its shields DOWN, not gone: they
            # read 0 and recharge in the dark.  Averaging them in turned every
            # cloak into a "shield collapse" in the facts and the narration.
            lit = [s for s in side if s.get("Cloak", 0) not in
                   (bridge.CLOAK_CLOAKING, bridge.CLOAK_CLOAKED,
                    bridge.CLOAK_DECLOAKING)]
            if lit:
                return sum(s.get("ShAvg", 100) for s in lit) // len(lit)
            if side and self.spar_track:
                return self.spar_track[-1].get(key, 100)
            return 100 if side else 0
        self.spar_track.append({
            "tick": snap.tick,
            "red_hull": side_avg(red), "blue_hull": side_avg(blue),
            "red_sh": side_sh(red, "red_sh"), "blue_sh": side_sh(blue, "blue_sh"),
            "red_alive": len(red), "blue_alive": len(blue),
            # ships fully cloaked per side: how long each side spent dark
            "red_dark": sum(1 for s in red
                            if s.get("Cloak") == bridge.CLOAK_CLOAKED),
            "blue_dark": sum(1 for s in blue
                             if s.get("Cloak") == bridge.CLOAK_CLOAKED),
        })
        if len(self.spar_track) > 4000:
            self.spar_track = self.spar_track[-4000:]
        self._spar_narrate(snap, red, blue)
        started = self.spar_track[0]["tick"] if self.spar_track else 0
        if not red and not blue:
            self._finish_spar("draw", "mutual destruction")
        elif not red:
            self._finish_spar("blue", "red fleet destroyed")
        elif not blue:
            self._finish_spar("red", "blue fleet destroyed")
        elif snap.tick - started > sparring.ROUND_TICK_LIMIT:
            winner = ("red" if side_avg(red) > side_avg(blue) else
                      "blue" if side_avg(blue) > side_avg(red) else "draw")
            self._finish_spar(winner, "time limit - %s ahead on hull"
                              % winner)
        else:
            self._flush_orders(snap)

    def _spar_narrate(self, snap, red, blue):
        """Sparring-only data feed: the AI showing its working.  One-shot
        threshold events + kills by name + a periodic status line.  Never
        runs in player-vs-AI battles (this method is spar-tick only)."""
        m = self.mem
        # kills, by name
        names = {"red": [s.get("Name", "?") for s in red],
                 "blue": [s.get("Name", "?") for s in blue]}
        prev = m.get("spar_names")
        if prev:
            for side in ("red", "blue"):
                for gone in [n for n in prev[side]
                             if n not in names[side]]:
                    self.say("system", "kill: %s destroyed - %d red / %d "
                             "blue still up." % (gone, len(red), len(blue)))
        m["spar_names"] = names
        # one-shot thresholds per side per round
        for side, ships in (("red", red), ("blue", blue)):
            if not ships:
                continue
            hull = sum(s.get("Hull", 100) for s in ships) // len(ships)
            # from the track row just written: it leaves cloaked ships out
            sh = self.spar_track[-1].get(side + "_sh", 100) \
                if self.spar_track else 100
            if hull < 100 and not m.get("fb_" + side):
                m["fb_" + side] = 1
                self.say("system", "first blood on %s - hull breached "
                         "(shields %d%%)." % (side, sh))
            if sh <= 25 and not m.get("shd_" + side):
                m["shd_" + side] = 1
                self.say("system", "%s shields buckling (%d%%) - hull "
                         "%d%%." % (side, sh, hull))
            if hull <= 50 and not m.get("half_" + side):
                m["half_" + side] = 1
                self.say("system", "%s past half strength - hull %d%%."
                         % (side, hull))
        # periodic status (~every 40 game ticks)
        last = m.get("spar_status_tick", 0)
        if snap.tick - last >= 40:
            m["spar_status_tick"] = snap.tick
            def one(side, ships):
                if not ships:
                    return side + " wiped"
                hull = sum(s.get("Hull", 100) for s in ships) // len(ships)
                sh = self.spar_track[-1].get(side + "_sh", 100) \
                    if self.spar_track else 100
                return "%s %d%% hull / %d%% sh / %d up" % (side, hull, sh,
                                                           len(ships))
            self.say("system", "t+%d: %s | %s"
                     % (snap.tick - (self.spar_track[0]["tick"]
                                     if self.spar_track else 0),
                        one("red", red), one("blue", blue)))

    def _spar_cloak_bank(self, winner, key, ticks, tainted):
        """Bank this round's cloak plays with the learner - never a tainted
        round.  Returns per-ship records for the round log."""
        arms = self.mem.get("cloak_arm") or {}
        if not arms:
            return []
        scen = self.plan.get("spar", {})
        cmems = self.mem.get("cloak_mem", {})
        hulls = self.plan.setdefault("spar_cloak_hulls", [])
        out, lines = [], []
        for i in sorted(arms):
            a = arms[i]
            side = a["side"]
            outcome = ("d" if winner not in ("red", "blue") else
                       "w" if winner == side else "l")
            other = scen.get("blue" if side == "red" else "red", [])
            strikes = cmems.get(i, {}).get("strikes", 0)
            out.append({"slot": i, "name": a["name"], "hull": a["hull"],
                        "side": side, "play": a["play"], "outcome": outcome,
                        "cloaks": a["cloaks"], "strikes": strikes})
            lines.append("%s %s %s (%d cloak%s, %d strike%s)" % (
                a["name"], a["play"],
                {"w": "won", "l": "lost", "d": "drew"}[outcome],
                a["cloaks"], "" if a["cloaks"] == 1 else "s",
                strikes, "" if strikes == 1 else "s"))
            if not tainted:
                cloaklearn.record(a["hull"], a["play"], outcome,
                                  opponent=other[0][0] if other else "",
                                  key=key, side=side, ticks=ticks,
                                  cloaks=a["cloaks"], strikes=strikes)
                if a["hull"] not in hulls:
                    hulls.append(a["hull"])
        self.say("system", "cloak plays this round: %s%s." % (
            "; ".join(lines),
            " - NOT banked (tainted round)" if tainted else ""))
        return out

    def _finish_spar(self, winner, why):
        scen = self.plan.get("spar", {})
        key = scen.get("key", "spar")
        track = getattr(self, "spar_track", [])
        ticks = (track[-1]["tick"] - track[0]["tick"]) if len(track) > 1 else 0
        facts = sparring.analyze_round(track, winner, key)
        note = "%s (%s)" % (winner, why)
        tainted = bool(self.mem.get("spar_tainted"))
        self.mem["spar_tainted"] = 0
        if tainted:
            rec = {}
            self.say("system", "round quarantined (player intervention) - "
                     "result not banked.")
        else:
            rec = sparring.record_round(key, winner if winner in
                                        ("red", "blue") else "draw",
                                        ticks, note)
        self.say("system", "round over: %s. %s"
                 % (why, ("facts: " + "; ".join(facts)) if facts else ""))
        cloak_rounds = self._spar_cloak_bank(winner, key, ticks, tainted)
        if not tainted and "spawn failure" not in why:
            self._bank_weakness()
        brains_row = self._spar_brains_finish(winner, why, key, ticks,
                                              tainted, track)
        # per-set bookkeeping for the final debrief + lesson
        self.plan["spar_ticks_total"] = \
            self.plan.get("spar_ticks_total", 0) + ticks
        self.plan["spar_last_track"] = track[-1200:]
        self.plan.setdefault("spar_round_notes", []).append(
            "%s beat by %s in %d ticks (%s)%s"
            % (" + ".join(sparring.labels_for(
                   scen, "red" if winner == "blue" else "blue")) or "-",
               " + ".join(sparring.labels_for(scen, winner))
               if winner in ("red", "blue") else "nobody",
               ticks, "; ".join(facts) or "no facts",
               " [TAINTED]" if tainted else "")
            + ((" [Claude commanded %s, %s won]"
                % (brains_row["claude_side"], brains_row["winner_brain"]))
               if brains_row else ""))
        last = track[-1] if track else {}
        def dmg(side):
            alive = last.get(side + "_alive", 0)
            return 100 if not alive else 100 - last.get(side + "_hull", 100)
        self.plan.setdefault("spar_rounds", []).append({
            "n": len(self.plan.get("spar_rounds", [])) + 1,
            "red": sparring.labels_for(scen, "red"),
            "blue": sparring.labels_for(scen, "blue"),
            "winner": winner, "ticks": ticks, "tainted": tainted,
            "red_dmg_taken": dmg("red"), "blue_dmg_taken": dmg("blue"),
            "facts": facts,
        })
        # log the round (with the drafts that fought it)
        try:
            path = os.path.join(LOG_DIR, "spar_%d_%d.json"
                                % (int(time.time()), self.battle_no))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"key": key, "winner": winner, "why": why,
                           "tainted": tainted,
                           "red": [c for c, _ in scen.get("red", [])],
                           "blue": [c for c, _ in scen.get("blue", [])],
                           "ticks": ticks, "facts": facts,
                           "cloak": cloak_rounds,
                           "brains": brains_row,
                           "track": track[-2400:]}, f, indent=1)
        except OSError:
            pass
        left = self.plan.get("spar_rounds_left", 1) - 1
        self.plan["spar_rounds_left"] = left
        if left > 0:
            # the LOSING side redrafts from its race pool (both on a draw):
            # the set asks "can this race field anything that wins?"
            # A brains set keeps its hulls: the question there is the brain.
            era = self.plan.get("era", "")
            if scen.get("brains"):
                pass
            elif winner in ("red", "blue"):
                loser = "blue" if winner == "red" else "red"
                labels = sparring.redraft_side(scen, loser, era)
                if labels:
                    self.say("system", "%s redrafts: %s - trying to answer "
                             "%s." % (loser, " + ".join(labels),
                                      " + ".join(sparring.labels_for(
                                          scen, winner))))
            else:
                for side in ("red", "blue"):
                    labels = sparring.redraft_side(scen, side, era)
                    if labels:
                        self.say("system", "%s redrafts: %s."
                                 % (side, " + ".join(labels)))
            self.say("system", "%d round%s left - clearing the board, "
                     "then respawning." % (left, "" if left == 1 else "s"))
            self.writer.send([bridge.reap_fleet()])
            self.phase = "spar_cleanup"
            self.mem["cleanup_polls"] = 0
        else:
            self.writer.send([bridge.reap_fleet(), bridge.end()])
            self.phase = "debrief"
            tab = sparring.table().get(key, {})
            r, b, dr = (tab.get("red", 0), tab.get("blue", 0),
                        tab.get("draw", 0))
            win_side = ("red" if r > b else "blue" if b > r else "draw")
            win_race = scen.get("%s_race" % win_side, "nobody") \
                if win_side != "draw" else "nobody"
            total = self.plan.get("spar_ticks_total", 0)
            # a full debrief the report page can actually render
            curve = [(s["tick"], s.get("red_hull", 0), s.get("blue_hull", 0))
                     for s in self.plan.get("spar_last_track", [])]
            self.debrief = {
                "result": "sparring complete: %s - red %d / blue %d / "
                          "draw %d" % (key, r, b, dr),
                "ship": "RED %s vs BLUE %s" % (scen.get("red_race", "?"),
                                               scen.get("blue_race", "?")),
                "ship_class": key,
                "difficulty": "sparring",
                "duration_ticks": total,
                "battle_no": self.battle_no,
                "curve": curve,
                "sparring": tab,
                "rounds": self.plan.get("spar_rounds", []),
                "finished_at": time.time(),
            }
            self.say("system", "sparring set complete. table: red %d / "
                     "blue %d / draws %d." % (r, b, dr))
            # ONE fresh lesson per matchup, with the ships named -
            # replaces any earlier text so scores never go stale.
            if win_side != "draw" and (r + b) > 0:
                lesson = ("Sparring %s: %s won %d-%d (avg %d ticks). "
                          "Winning draft: %s. Failed answers: %s."
                          % (key, win_race, max(r, b), min(r, b),
                             tab.get("avg_ticks", 0),
                             " + ".join(sparring.labels_for(scen, win_side)),
                             "; ".join(self.plan.get("spar_round_notes",
                                                     [])[:4]) or "none"))
                learning.replace_lesson("spar:" + key, lesson[:400])
                self.debrief["lesson"] = lesson[:400]
            # ONE current cloak lesson per hull the set taught something about
            learned = []
            for hull in self.plan.get("spar_cloak_hulls", []):
                label = intel._db().get(hull, {}).get("label", hull)
                learning.replace_lesson("cloak:" + hull,
                                        cloaklearn.lesson(hull, label)[:400])
                line = "%s - %s" % (label, cloaklearn.summary(hull))
                learned.append(line)
                self.say("system", "cloak learner, all rounds on record: "
                         + line)
            if learned:
                self.debrief["cloak"] = learned
            if scen.get("brains"):
                self._spar_brains_set_summary()
            self._spar_debrief_async(key, scen, tab)

    def _bank_weakness(self):
        """Fold this fight's knockouts into data/weakness.json and say any
        weakness that became provable with it."""
        try:
            classes = set(r["cls"] for r in self.wk.ships.values())
            before = dict((c, set(l for _r, l in weakness.verdicts(c)))
                          for c in classes)
            weakness.record(self.wk)
            for c in sorted(classes):
                for _r, line in weakness.verdicts(c):
                    if line not in before.get(c, set()):
                        self.say("system", "weakness logged - %s: %s"
                                 % (intel._db().get(c, {}).get("label", c),
                                    line))
        except Exception as e:
            self.say("system", "weakness log failed (%s)" % e)
        self.wk = weakness.Tracker()

    def _spar_debrief_async(self, key, scen, tab):
        """Set-end strategist pass: turn the round notes into insight."""
        st = self.strategist
        if not st:
            return
        notes = self.plan.get("spar_round_notes", [])
        def run():
            out = st.debrief({
                "result": "sparring set %s: red %d / blue %d / draw %d"
                          % (key, tab.get("red", 0), tab.get("blue", 0),
                             tab.get("draw", 0)),
                "facts": " | ".join(notes) or "none",
                "metrics": json.dumps({"avg_ticks": tab.get("avg_ticks", 0),
                                       "red_race": scen.get("red_race"),
                                       "blue_race": scen.get("blue_race")}),
                "curve": [],
                "lessons": "(sparring set - write the tactical takeaway)",
                "recent": "",
            })
            if out["analysis"]:
                self.say("claude", "sparring analysis: " + out["analysis"])
            if out["lesson"]:
                learning.replace_lesson("spar:" + key, out["lesson"])
                self.say("claude", "lesson refined: " + out["lesson"])
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------- brains sparring

    def _spar_brains_arm(self, scen):
        """Build this round's two commanders (fresh memory every round)."""
        mode = scen.get("brains", "")
        if not mode:
            self.spar_brains = None
            return
        done = len(self.plan.get("spar_rounds", []))
        claude_side = (mode if mode in ("red", "blue") else
                       ("red" if done % 2 == 0 else "blue"))
        cfg, cms = self.api_cfg, {}
        for side_key, num in (("red", 0), ("blue", 1)):
            if side_key == claude_side:
                cm = sparbrains.SideCommander(
                    "claude", num, cfg.get("model"), cfg.get("key"),
                    cfg.get("effort"))
            else:
                cm = sparbrains.SideCommander("local", num, self.brain.model)
            other = "blue" if side_key == "red" else "red"
            notes = sparbrains.memory_for(scen, cm.model)
            cm.system = sparbrains.system_prompt(
                [c for c, _ in scen.get(side_key, [])],
                [c for c, _ in scen.get(other, [])], notes)
            cm.notes_n = len(notes)
            cms[side_key] = cm
        self.spar_brains = cms
        self.mem["brains_claude_side"] = claude_side
        lo_side = "blue" if claude_side == "red" else "red"
        self.say("system", "round %d: Claude (%s) commands %s, the local "
                 "model (%s) commands %s. Notes carried in: Claude %d, "
                 "local %d." % (
                     done + 1, cfg.get("model"), claude_side.upper(),
                     self.brain.model, lo_side.upper(),
                     cms[claude_side].notes_n, cms[lo_side].notes_n))

    def _spar_brains_tick(self, snap):
        """Collect finished decisions, queue their orders, start new ones."""
        if not self.spar_brains:
            return
        t0 = self.spar_track[0]["tick"] if self.spar_track else snap.tick
        for side_key, cm in self.spar_brains.items():
            cm.view.update(snap)
            res = cm.poll()
            if res:
                orders, ok, bad = sparbrains.translate(res["orders"], snap,
                                                       cm.view)
                cm.stats["orders"] += len(ok)
                cm.stats["rejected"] += len(bad)
                cm.decisions.append({"tick": snap.tick,
                                     "assessment": res["assessment"],
                                     "orders": ok, "rejected": bad})
                del cm.decisions[:-80]
                self._queue_orders(orders, source="spar-" + cm.name)
                cm.feedback = sparbrains.feedback_line(ok, bad)
                if ok or bad:
                    self.say("claude" if cm.engine == "claude" else "ai",
                             "[%s %s] %s%s%s" % (
                                 cm.name.upper(), side_key.upper(),
                                 res["assessment"],
                                 (" -> " + ", ".join(ok)) if ok else "",
                                 (" (refused: " + "; ".join(bad) + ")")
                                 if bad else ""))
            elif cm.last_error and not cm.busy and \
                    cm.stats["fails"] in (1, 3, 10):
                self.say("system", "%s commander call failed (%s) - that "
                         "side gets no orders this turn." % (
                             cm.name, cm.last_error))
                cm.last_error = ""
            if cm.due():
                cm.request(sparbrains.brief(snap, cm.view,
                                            snap.tick - t0, cm.feedback))

    def _spar_brains_finish(self, winner, why, key, ticks, tainted, track):
        """Score the round for the brains table. Returns the log row."""
        cms = self.spar_brains
        if not cms:
            return None
        self.spar_brains = None             # late replies are discarded
        claude_side = self.mem.get("brains_claude_side", "red")
        cl = cms[claude_side]
        lo = cms["blue" if claude_side == "red" else "red"]
        void = ""
        if tainted:
            void = "player intervention"
        elif "link lost" in why or "spawn failure" in why:
            void = why
        else:
            for cm in (cl, lo):
                n = cm.stats["calls"] + cm.stats["fails"]
                if not cm.stats["calls"] or (
                        n >= 3 and cm.fail_ratio() > sparbrains.VOID_FAIL_RATIO):
                    void = "%s commander failed %d of %d calls" % (
                        cm.name, cm.stats["fails"], n)
                    break
        last = track[-1] if track else {}
        winner_brain = ("draw" if winner not in ("red", "blue") else
                        "claude" if winner == claude_side else "local")
        row = {"time": int(time.time()), "key": key,
               "round": len(self.plan.get("spar_rounds", [])) + 1,
               "claude_side": claude_side, "claude_model": cl.model,
               "claude_effort": cl.effort, "local_model": lo.model,
               "winner_side": winner, "winner_brain": winner_brain,
               "why": why, "ticks": ticks, "void": void,
               "red_hull": last.get("red_hull"),
               "blue_hull": last.get("blue_hull"),
               "claude": dict(cl.stats), "local": dict(lo.stats),
               "decisions": {"claude": cl.decisions[-40:],
                             "local": lo.decisions[-40:]}}
        spar = self.plan.get("spar", {})
        counts = {k: sparbrains.manoeuvre_counts(cm.decisions)
                  for k, cm in (("claude", cl), ("local", lo))}
        sparbrains.record(dict(
            row, decisions=None,
            ids={s: [c for c, _ in spar.get(s, [])] for s in ("red", "blue")},
            man={k: v[1] for k, v in counts.items()},
            orders={k: v[0] for k, v in counts.items()}))
        brief_row = {k: row[k] for k in ("round", "claude_side",
                                         "winner_brain", "void", "ticks")}
        brief_row["claude_cost"] = cl.stats["cost"]
        self.plan.setdefault("spar_brain_rounds", []).append(brief_row)
        self._spar_brains_reflect(cms, winner, why, ticks, last, row["round"])
        self.say("system", "brains: %s%s. %s | %s" % (
            {"claude": "CLAUDE wins the round",
             "local": "the LOCAL model wins the round",
             "draw": "a draw"}[winner_brain],
            (" - VOID, not scored (%s)" % void) if void else "",
            cl.summary(), lo.summary()))
        return row

    def _spar_brains_reflect(self, cms, winner, why, ticks, last, rnd):
        """Each commander writes one lesson from its own round, with its own
        model, into the matchup memory both brains read at the next arm.
        Runs on threads; a lesson that lands after the next arm is read
        from the round after that."""
        if ticks < 10 or "spawn failure" in why or "link lost" in why:
            return
        scen = dict(self.plan.get("spar", {}))
        for side_key, cm in cms.items():
            other = "blue" if side_key == "red" else "red"
            result = ("drew" if winner not in ("red", "blue") else
                      "won" if winner == side_key else "lost")
            own = sparring.labels_for(scen, side_key)
            enemy = sparring.labels_for(scen, other)
            own_c = [c for c, _ in scen.get(side_key, [])]
            en_c = [c for c, _ in scen.get(other, [])]
            summary = cm.round_summary(
                side_key, own, enemy, result, why, ticks,
                last.get(side_key + "_hull", "?"),
                last.get(other + "_hull", "?"),
                enemy_counts=sparbrains.manoeuvre_counts(
                    cms[other].decisions),
                record=sparbrains.evidence_text(
                    own_c[0] if own_c else "",
                    en_c[0] if len(set(en_c)) == 1 else None))
            fact = ("%s: flew %s vs %s, %s in %d ticks (hull %s%% vs %s%%)"
                    % (time.strftime("%m-%d") + " r%d" % rnd,
                       " + ".join(own), " + ".join(enemy), result.upper(),
                       ticks, last.get(side_key + "_hull", "?"),
                       last.get(other + "_hull", "?")))

            with self._lock:
                self._reflecting += 1

            own_ids = [c for c, _ in scen.get(side_key, [])]
            enemy_ids = [c for c, _ in scen.get(other, [])]

            def run(cm=cm, summary=summary, fact=fact, result=result,
                    own_ids=own_ids, enemy_ids=enemy_ids):
                try:
                    lesson = cm.reflect(summary)
                    sparbrains.add_memory(scen, cm.model, fact + (
                        ". Lesson: " + lesson if lesson else ""))
                    if lesson:
                        # indexed by hull so duels can use it
                        sparbrains.add_hull_lesson({
                            "own": own_ids, "enemy": enemy_ids,
                            "model": cm.model, "engine": cm.engine,
                            "result": result, "lesson": lesson,
                            "key": scen.get("key", ""),
                            "time": int(time.time())})
                    self.say("claude" if cm.engine == "claude" else "ai",
                             "[%s lesson] %s" % (cm.name.upper(), lesson)
                             if lesson else
                             "[%s] no lesson this round (%s)."
                             % (cm.name.upper(), cm.last_error or "empty"))
                finally:
                    with self._lock:
                        self._reflecting -= 1
            threading.Thread(target=run, daemon=True).start()

    def _spar_brains_set_summary(self):
        rows = self.plan.get("spar_brain_rounds", [])
        scored = [r for r in rows if not r["void"]]
        c = sum(1 for r in scored if r["winner_brain"] == "claude")
        l = sum(1 for r in scored if r["winner_brain"] == "local")
        d = len(scored) - c - l
        cost = sum(r.get("claude_cost", 0) for r in rows)
        pair = "%s vs %s" % (self.api_cfg.get("model"), self.brain.model)
        overall = sparbrains.table().get("by_pair", {}).get(pair, {})
        line = ("brains set: Claude %d / local %d / draws %d%s, Claude spend "
                "~$%.2f. All rounds on record for %s: Claude %d / local %d "
                "/ draws %d." % (
                    c, l, d, (" (%d void)" % (len(rows) - len(scored)))
                    if len(rows) > len(scored) else "", cost, pair,
                    overall.get("claude", 0), overall.get("local", 0),
                    overall.get("draw", 0)))
        self.say("system", line)
        if self.debrief is not None:
            self.debrief["brains"] = {"claude": c, "local": l, "draw": d,
                                      "rounds": rows, "summary": line}

    # ------------------------------------------------------------ ally mode

    def stage_ally(self, spec, player_ship=""):
        """Stage Ally mode: Claude flies one wingman on the player's side vs a red force."""
        scen = ally.build_scenario(spec, player_ship)
        if isinstance(scen, str):
            self.say("system", "ally mode: " + scen)
            return None
        db = intel._db()
        with self._lock:
            self.plan = {
                "mode": "ally",
                "difficulty": "officer",
                "ship_class": scen["wing"][0],
                "ship_name": scen["wing"][1],
                "persona": "",
                "era": spec.get("era", ""),
                "player_ship": player_ship,
                "scenario": scen["key"],
                "ally": scen,
                "staged_at": time.time(),
            }
            self.debrief = None
            if self.phase == "debrief":
                self.phase = "idle"
                self.probe = bridge.LinkProbe(self.game_root)
        reds = ", ".join(db.get(c, {}).get("label", c) for c, _n in scen["red"])
        self.say("system", "ally mode staged: %d red (%s) vs you + my wingman %s (%s - %s). "
                 "arm the link in-game when ready."
                 % (len(scen["red"]), reds, scen["wing"][1],
                    db.get(scen["wing"][0], {}).get("label", scen["wing"][0]),
                    scen["wing_why"]))
        return self.plan

    def _arm_ally(self, snap):
        self.writer.resync()
        self.mem = {"ally": True}
        scen = self.plan["ally"]
        orders = [bridge.spawn_range(scen.get("spawn") or ally.DEFAULT_SPAWN)]
        for cid, name in scen["red"]:
            orders.append(bridge.spawn(intel.spawn_name(cid), name, side=0))
        orders.append(bridge.spawn(intel.spawn_name(scen["wing"][0]), scen["wing"][1], side=1))
        self.writer.send(orders, source="ally")
        self.writer.mark = self.writer.seq - 1
        self.mem["opening_orders"] = orders
        self.mem["armed_polls"] = 0
        self.battle_no += 1
        self.hull_curve = []
        self.track = []
        self.events = []
        self._prev = {}
        self.debrief = None
        self.phase = "armed"
        self.say("system", "link armed - %d red ahead, your wingman forming up on you."
                 % len(scen["red"]))

    def _ally_sides(self, snap):
        fleet = snap.live_fleet()
        red = {i: s for i, s in fleet.items() if s.get("Side", 0) == 0}
        wing = None
        want = (self.plan or {}).get("ally", {}).get("wing", ("", ""))[1]
        for i, s in fleet.items():
            if s.get("Side", 0) == 1 and (s.get("Name") == want or wing is None):
                wing = i
        return red, wing

    def _tick_ally(self, snap, link_up):
        if self.phase == "ally_cleanup":
            n = self.mem.get("cleanup_polls", 0) + 1
            self.mem["cleanup_polls"] = n
            if (not snap.live_fleet() and self.writer.acked(snap)) or n >= 10:
                self._arm_ally(snap)
            return
        if self.phase == "debrief":
            return
        red, wing = self._ally_sides(snap)
        if self.phase == "armed":
            if not link_up:
                return
            if red and wing is not None:
                self.mem["wing"] = wing
                self.mem["reds_seen"] = len(red)
                self.mem["red_names"] = dict((i, s.get("Name", "")) for i, s in red.items())
                self.mem["t0"] = snap.tick
                self.phase = "fighting"
                self.say("system", "contact - %d red on the board, %s formed up. "
                         "I'll hit whatever you target and cover you if you get hammered."
                         % (len(red), snap.fleet[wing].get("Name", "wingman")))
                self._queue_orders([bridge.taunt("Wingman in position. Lead the way.", ship=wing)], "ally")
                self._flush_orders(snap)
                return
            polls = self.mem.get("armed_polls", 0) + 1
            self.mem["armed_polls"] = polls
            if polls in (8, 24, 60) and not snap.live_fleet() and self.mem.get("opening_orders"):
                self.writer.seq = max(self.writer.seq, snap.ack_seq)
                seq = self.writer.send(self.mem["opening_orders"], source="ally")
                self.say("system", "spawn unconfirmed - re-sending (batch %d)." % seq)
            if polls >= 90:
                self._finish_ally("aborted", "the ships never reached the board")
            return
        # fighting
        if snap.tick == 0 and not snap.fleet:
            return
        if snap.raw.get("YouHull", 100) == 0 or snap.raw.get("Status", 0) == 4:
            self._finish_ally("defeat", "your ship was destroyed")
            return
        if snap.tick < self.mem.get("last_tick", 0) - 5:
            self._finish_ally("aborted", "game restarted mid-battle")
            return
        self.mem["last_tick"] = snap.tick
        if not link_up:
            stall = self.mem.get("stall_polls", 0) + 1
            self.mem["stall_polls"] = stall
            if stall == 8:
                self.say("system", "telemetry stalled - game paused or loading. standing by.")
            if stall > 200:
                self._finish_ally("aborted", "link lost (game closed or crashed)")
            return
        self.mem["stall_polls"] = 0
        # kills + wingman loss
        names = self.mem.setdefault("red_names", {})
        for i, s in red.items():
            names.setdefault(i, s.get("Name", ""))
        for i in list(names):
            if i not in red and i not in self.mem.setdefault("red_dead", {}):
                self.mem["red_dead"][i] = snap.tick
                self.say("system", "red down: %s (%d left)." % (names[i], len(red)))
        wslot = self.mem.get("wing")
        wing_alive = wslot is not None and snap.fleet.get(wslot, {}).get("st") == bridge.SLOT_ALIVE
        if not wing_alive and not self.mem.get("wing_lost"):
            self.mem["wing_lost"] = snap.tick
            self.say("system", "my wingman is gone - you're on your own. sorry, commander.")
        self.track.append({"t": snap.tick, "you_hull": snap.raw.get("YouHull", -1),
                           "you_sh": snap.raw.get("YouShAvg", -1),
                           "wing_hull": snap.fleet.get(wslot, {}).get("Hull", 0) if wing_alive else 0,
                           "reds": len(red), "mode": self.mem.get("wing_mode", ""),
                           "focus": self.mem.get("wing_focus")})
        if not red:
            quiet = self.mem.get("no_red_polls", 0) + 1
            self.mem["no_red_polls"] = quiet
            if quiet >= 2:
                self._finish_ally("victory", "all red ships destroyed")
            return
        self.mem["no_red_polls"] = 0
        if wing_alive:
            orders, say = ally.wingman_orders(snap, self.mem, wslot)
            if say:
                orders.append(bridge.taunt(say, ship=wslot))
                self.say("claude", say)
            self._queue_orders(orders, "ally")
        self._flush_orders(snap)

    def _finish_ally(self, result, why):
        t0 = self.mem.get("t0", 0)
        dur = max(0, self.mem.get("last_tick", t0) - t0)
        kills = len(self.mem.get("red_dead", {}))
        scen = (self.plan or {}).get("ally", {})
        self.debrief = {
            "mode": "ally",
            "result": "%s - %s" % (result, why),
            "battle_no": self.battle_no,
            "scenario": scen.get("key", ""),
            "wingman": scen.get("wing", ("", ""))[1],
            "wing_survived": not self.mem.get("wing_lost"),
            "reds": len(scen.get("red", [])),
            "kills": kills,
            "duration_ticks": dur,
            "finished_at": time.time(),
        }
        self.phase = "debrief"
        self.say("system", "ally battle over: %s - %s. %d/%d red destroyed in %d ticks, "
                 "wingman %s." % (result, why, kills, len(scen.get("red", [])), dur,
                                  "survived" if self.debrief["wing_survived"] else "lost"))
        if result == "victory" and self.debrief["wing_survived"]:
            ws = self.mem.get("wing")
            if ws is not None:
                self.writer.send([bridge.taunt("Good hunting, commander.", ship=ws)], source="ally")
        try:
            path = os.path.join(LOG_DIR, "ally_%d_%d.json" % (int(time.time()), self.battle_no))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"debrief": self.debrief, "plan": self.plan, "chat": self.chat[-200:],
                           "track": self.track[-2400:],
                           "orders": self.writer.batches_since(getattr(self.writer, "mark", 0))[-600:]},
                          f, indent=1, default=str)
        except OSError:
            pass

    def _arm(self, snap):
        """Link just came up with a plan staged: spawn and open."""
        self.writer.resync()
        prof = doctrine.PROFILES[self.plan["difficulty"]]
        self.mem = {}
        threat_text = " ".join((self.plan.get("player_ship", ""),
                                self.plan.get("player_allies", "")))
        escorts = doctrine.escorts_for(prof, threat_text,
                                       self.plan["ship_class"])
        if doctrine.duel_pure(self.plan):
            self.mem["duel_pure"] = True
            escorts = []
        self.mem["heavy_opponent"] = doctrine.heavy_opponent(threat_text)
        pools = intel.faction_pools(self.plan["ship_class"])
        self.mem["pool_escort"], self.mem["pool_heavy"] = pools
        self.mem["is_station"] = intel.is_station(self.plan["ship_class"])
        # Total-ship budget: the engine tolerates ~14-16 active AI ships.
        # Her cap shrinks to leave room for the player's declared fleet.
        import re as _re
        declared = sum(int(m) for m in
                       _re.findall(r"\b(\d{1,2})\b",
                                   self.plan.get("player_allies", ""))) + 1
        prof_cap = prof.get("fleet_cap")
        if prof_cap:
            self.mem["fleet_cap_override"] = max(4, min(prof_cap,
                                                        14 - declared))
        orders = doctrine.opening_orders(
            prof, self.plan["ship_class"], self.plan["ship_name"], escorts)
        auto_range = 0
        if not self.plan.get("spawn"):
            # No forced distance: pick one from the hull's own guns.  The game
            # spawns at 5000 regardless, which is why a Crystalline Entity
            # (beams reach 60) can fight five rounds and never fire a shot.
            wp = intel.weapon_profile(self.plan["ship_class"])
            if wp and wp.get("want_range"):
                auto_range = max(150, min(5000, wp["want_range"] + 250))
                self.plan["spawn"] = auto_range
        if self.plan.get("spawn"):
            # must precede the spawns - order 17 only affects the NEXT spawn.
            # Without this a duel always opens at the 5000 default, where a
            # slow or short-ranged hull (DS9Wings: speed 8.4, beams 250) can
            # never reach the player and fights the whole battle on torpedoes.
            orders.insert(0, bridge.spawn_range(self.plan["spawn"]))
        self.writer.send(orders, source="opening")
        self.writer.mark = self.writer.seq - 1     # this battle's log starts here
        if auto_range:
            wp = intel.weapon_profile(self.plan["ship_class"]) or {}
            self.say("system", "engagement set to %dm from the hull's own "
                     "profile (%s, effective reach %d)."
                     % (auto_range, wp.get("kind", "?"), wp.get("reach", 0)))
        self.mem["opening_orders"] = orders
        self.mem["armed_polls"] = 0
        # Seed the power reflex: this hull's own gun reach, and whatever
        # setting the opening batch just installed - without the second it
        # would re-send that same setting on its very first tick.
        wp0 = intel.weapon_profile(self.plan["ship_class"]) or {}
        self.mem["reach"] = wp0.get("reach", 0)
        self.mem["ship_class"] = self.plan["ship_class"]
        # Which subsystem KINDS the enemy hull has (valid_target reads this).
        # Presence only - the database's targetable flag is the group node's,
        # and Orion marks every group 0 while the parts inside it are 1.
        pship = intel._resolve_player_ship(self.plan.get("player_ship", ""))
        if pship:
            kinds = {"impulse": "engines", "sensors": "sensors",
                     "warp": "warp", "torpedo_system": "torps",
                     "cloak": "cloak", "hull": "hull"}
            have = {kinds[x["kind"]] for x in pship.get("subsystems", [])
                    if x.get("kind") in kinds}
            if pship.get("phasers") or pship.get("pulses"):
                have.add("weapons")
            self.mem["player_kinds"] = have
        else:
            self.mem["player_kinds"] = None
        self.mem["power"] = ""
        self.mem["power_tick"] = -9999
        prof0 = doctrine.PROFILES.get(self.plan.get("difficulty", "officer"))
        for step in (prof0 or {}).get("opening", []):
            bits = step.split()
            if bits[0] == "power" and len(bits) > 1:
                self.mem["power"] = bits[1]
        self.battle_no += 1
        self.hull_curve = []
        self.track = []
        self.events = []
        self._prev = {}
        self.debrief = None
        self.brain.configure(
            PERSONAS.get(self.plan["persona"], PERSONAS["klingon"]),
            self.plan["ship_name"], self.plan["ship_class"],
            self.plan.get("player_ship", ""),
            prof["label"], prof["allowed"], prof["telegraph"],
            banter=self.banter,
            extra_context="\n\n".join(x for x in (
                ("YOUR ADMIRAL'S BATTLE PLAN (follow unless the battle "
                 "clearly contradicts it):\n" + self.claude_plan)
                if self.claude_plan else "",
                opponent_note(self.plan.get("player_ship", ""),
                              self.plan.get("player_side", "")),
                self._scenario_block(),
                lore.brief(self.plan),
                intel.battle_intel(self.plan["ship_class"],
                                   self.plan.get("player_ship", "")),
                sparbrains.hull_lessons_text(self.plan["ship_class"],
                                             self._player_class()),
                weakness.text_for(self._player_class()),
                weakness.text_for(self.plan["ship_class"], own=True),
                learning.brief_context(self.plan["ship_class"],
                                       self._player_class())) if x))
        self.phase = "armed"
        self._last_brain = time.time()
        self.wk = weakness.Tracker()
        self.mem["pclass"] = self._player_class()
        self.mem["hull_shape"] = intel.hull_shape(self.plan["ship_class"])
        self.brain.online_fail = 0          # un-park the online brain
        if self.brain.online is not None:
            self.brain.online.reset_stats()
        self.say("system", "link armed - spawning %s."
                 % self.plan["ship_name"])

    def _scenario_block(self):
        """The player's own description of the battle they want."""
        p = self.plan or {}
        lines = []
        if p.get("player_name"):
            side = (" fighting for the %s" % p["player_side"]
                    if p.get("player_side") else "")
            lines.append("- Their ship is named %s%s."
                         % (p["player_name"], side))
        elif p.get("player_side"):
            lines.append("- They fight for the %s." % p["player_side"])
        if p.get("player_allies"):
            lines.append("- They will also field allied ships: %s. Expect "
                         "to fight more than one vessel."
                         % p["player_allies"])
        if p.get("scenario"):
            lines.append("- The player describes the battle they want: "
                         "\"%s\". Honour the spirit of this scenario in how "
                         "you fight and speak." % p["scenario"])
        if not lines:
            return ""
        return "THE SCENARIO:\n" + "\n".join(lines)

    def _find_flag(self, snap):
        """Locate the spawned flagship in the fleet telemetry.

        The game picks the slot itself (first empty, else first dead is
        recycled), so we identify our ship after the fact - by name first
        (spawn may have deduped it with a numeric suffix), then by class.
        """
        want_name = self.plan["ship_name"].lower()
        want_class = intel.spawn_name(self.plan["ship_class"]).lower()
        by_class = None
        for i, s in snap.live_fleet().items():
            name = str(s.get("Name", "")).lower()
            stype = str(s.get("Type", "")).lower()
            if name.startswith(want_name):
                return i
            if stype == want_class and by_class is None:
                by_class = i
        return by_class

    GUNBOAT_OPENING_TICKS = 40

    def _gunboat_opening(self, snap, prof, flag, flag_slot):
        """Put a short-reach flagship on the gunboat stance, by slot.

        The opening batch broadcasts its stance before the flagship has a
        slot, and a broadcast would hand the escorts the gunboat fork too, so
        doctrine.opening_orders sends balanced instead.  This gives the real
        pick to the flagship alone, re-sent for the first
        GUNBOAT_OPENING_TICKS until telemetry shows it installed (a batch can
        be overwritten before the game reads it)."""
        m = self.mem
        cls = self.plan.get("ship_class", "")
        if "gunboat" not in m:
            want = ""
            if "stance" in prof.get("allowed", []):
                ops = [st.split()[1] for st in prof.get("opening", [])
                       if st.startswith("stance ")]
                if ops and doctrine.hull_stance(ops[0], cls) == "gunboat":
                    want = "gunboat"
            m["gunboat"] = want
            m["gunboat_until"] = snap.tick + self.GUNBOAT_OPENING_TICKS
        if (not m["gunboat"] or snap.tick > m.get("gunboat_until", 0)
                or m.get("cloak_phase")
                or flag.get("Stance") == bridge.STANCES["gunboat"]
                or flag.get("Cloak", 0) in (bridge.CLOAK_CLOAKING,
                                            bridge.CLOAK_CLOAKED)
                or snap.tick - m.get("gunboat_tick", -99) < 4):
            return []
        m["gunboat_tick"] = snap.tick
        return [doctrine.stance_order("gunboat", cls, flag_slot)]

    def _fight(self, snap):
        prof = doctrine.PROFILES[self.plan["difficulty"]]
        flag_slot = self.mem.get("flag", 0)
        flag = snap.fleet.get(flag_slot)
        # The current tick, for anything that stamps a time into mem.  The
        # brain's power orders record mem["power_tick"] from this; it was
        # never set, so they were stamped tick 0, the reflex's 10-tick dwell
        # read as long expired, and it overrode every brain power choice on
        # the next poll (10-25 duel: shields -> balanced at t61, t73, t85).
        self.mem["tick"] = snap.tick

        # ---- endings
        if snap.player_alive == 0:
            self._finish("victory - the player's ship was destroyed")
            return
        if flag is None or flag["st"] == bridge.SLOT_DEAD:
            others = snap.live_fleet()
            if not others:
                self._finish("defeat - %s destroyed"
                             % self.plan["ship_name"])
                return
            # promote an escort to flagship
            new_flag = sorted(others.keys())[0]
            self.mem["flag"] = new_flag
            self.say("system", "%s is gone - %s takes the flag."
                     % (self.plan["ship_name"],
                        others[new_flag].get("Name", "escort")))
            return

        # ---- learning observations
        if snap.raw.get("YouCloak", 0) == bridge.CLOAK_CLOAKED:
            self.mem["obs_player_cloaked"] = True
        ph = snap.raw.get("YouHull", -1)
        if 0 <= ph < self.mem.get("obs_player_min_hull", 100):
            self.mem["obs_player_min_hull"] = ph
        nf = len(snap.live_foes())
        if nf > self.mem.get("obs_fighters_seen", 0):
            self.mem["obs_fighters_seen"] = nf

        # ---- record curve
        self.hull_curve.append((snap.tick, flag.get("Hull", -1),
                                snap.raw.get("YouHull", -1)))
        if len(self.hull_curve) > 4000:
            self.hull_curve = self.hull_curve[-4000:]

        # ---- record the movement/state track for post-battle evaluation
        p = snap.raw
        self.wk.see("player", self.mem.get("pclass", ""), snap.tick,
                    {"Hull": p.get("YouHull", 100), "Eng": p.get("YouEng"),
                     "Weap": p.get("YouWeap"), "Sens": p.get("YouSens"),
                     "Warp": p.get("YouWarp"), "Pow": p.get("YouPow"),
                     "Torp": p.get("YouTorp"), "Rep": p.get("YouRep"),
                     "EngOut": p.get("YouEngOut", 0),
                     "WarpOut": p.get("YouWarpOut", 0)},
                    alive=p.get("YouHull", 100) > 0)
        self.wk.see("flag", (self.plan or {}).get("ship_class", ""),
                    snap.tick, flag)
        self.track.append({
            "tick": snap.tick,
            "hull": flag.get("Hull", 100),
            "player_hull": snap.raw.get("YouHull", 100),
            "rng": bridge.slot_range(flag),
            "cloak": flag.get("Cloak", 0),
            "pcloak": snap.raw.get("YouCloak", 0),
            "batt": flag.get("Batt", 100),
            # What the game says the flagship IS doing, not what we sent.
            # Four Challenger fights at captain read "in range, not firing"
            # from hull/battery alone and the cause was guessed three times;
            # weapons health, stance, target and subsystem end the guessing.
            "weap": flag.get("Weap", -1), "torp": flag.get("Torp", -1),
            "pow": flag.get("Pow", -1), "sens": flag.get("Sens", -1),
            "eng": flag.get("Eng", -1), "stance": flag.get("Stance", -1),
            "eng_out": flag.get("EngOut", 0), "imp": flag.get("ImpMax", -1),
            "spd": flag.get("Spd10", -1), "eng_rep": flag.get("EngRep", -1),
            "tgt": flag.get("Tgt", -1), "tsub": flag.get("TSub", -1),
            "ab": flag.get("AbFires", 0), "man": flag.get("Man", 0),
            "torps": snap.raw.get("TorpCount", 0),   # salvo rhythm
            "shield_min": min([flag.get(k, 100) for k in
                               ("ShF", "ShA", "ShL", "ShR", "ShT", "ShB")]),
            # every facing, F/A/L/R/T/B: which one he is working on (a logged duel)
            "sh6": [flag.get(k, -1) for k in
                    ("ShF", "ShA", "ShL", "ShR", "ShT", "ShB")],
        })
        if len(self.track) > 4000:
            self.track = self.track[-4000:]
        if len(self.track) >= 2:
            prev, cur = self.track[-2], self.track[-1]
            CLOAKED = bridge.CLOAK_CLOAKED
            if cur["cloak"] == CLOAKED and prev["cloak"] != CLOAKED:
                self.events.append((snap.tick, "cloak_on"))
            if cur["cloak"] != CLOAKED and prev["cloak"] == CLOAKED:
                self.events.append((snap.tick, "decloak"))
            if cur["pcloak"] == CLOAKED and prev["pcloak"] != CLOAKED:
                self.events.append((snap.tick, "player_cloak"))
            if cur["pcloak"] != CLOAKED and prev["pcloak"] == CLOAKED:
                self.events.append((snap.tick, "player_decloak"))
            if cur["player_hull"] - prev["player_hull"] >= 6:
                self.events.append((snap.tick, "player_repair_spike"))

        # ---- reflex layer (every poll)
        orders = self._gunboat_opening(snap, prof, flag, flag_slot)
        orders += doctrine.reflexes(snap, self.mem, prof)
        orders = self._drive_hold(orders, flag)
        if self.mem.get("duel_cloak_play") and \
                not self.mem.get("duel_cloak_said"):
            self.mem["duel_cloak_said"] = True
            self.say("system", "cloak learner: sparring settled on '%s' for "
                     "this hull - the cloak play follows it."
                     % self.mem["duel_cloak_play"])
        self._queue_orders(orders, source="reflex")
        for key in ("holdfire_note", "cloak_jam_note", "drive_note"):
            note = self.mem.pop(key, None)
            if note:
                self.say("system", note)

        # ---- collect a finished LLM decision
        res = self.abrain.poll()
        if res:
            if res["say"]:
                # a reply to the player always goes through; unprompted
                # transmissions respect the cooldown, never echo the
                # prompt exemplars, and never rehash a recent line
                norm = _say_norm(res["say"])
                history = self.mem.setdefault("say_history", [])
                expect = self.mem.pop("expect_reply", False)
                fresh = (norm not in _EXEMPLAR_SAYS and
                         all(_say_sim(norm, h) < SAY_SIMILARITY
                             for h in history[-3:]))
                cool = (time.time() - self.mem.get("last_say_time", 0)
                        >= SAY_COOLDOWN)
                if expect or (fresh and cool):
                    self.say("ai", res["say"])
                    self.mem["last_say_time"] = time.time()
                    history.append(norm)
                    del history[:-5]
            llm_orders = res["orders"]
            if any(o["verb"] == "reinforce" for o in llm_orders) and \
                    "reinforce" in prof["allowed"] and \
                    not self.mem.get("duel_pure"):
                wave = doctrine.reinforcement_wave(
                    snap, self.mem, prof, flag, snap.tick, 1)
                self._queue_orders(wave, source="wave")
                llm_orders = [o for o in llm_orders
                              if o["verb"] != "reinforce"]
            border = to_bridge_orders(llm_orders, prof["allowed"],
                                      flag_slot, self.mem, flag)
            border = self._cloak_authority(border, snap, flag, prof)
            border = self._brain_gate(border, snap, flag, prof)
            border = self._drive_hold(border, flag)
            self._queue_orders(border, source="brain")

        # ---- start a new LLM decision on cadence (or on player chat)
        cadence = prof["brain_cadence"]
        user_line = None
        with self._lock:
            if self._pending_user_line:
                user_line = self._pending_user_line
                self._pending_user_line = None
        due = cadence and (time.time() - self._last_brain) >= cadence
        if (due or user_line) and self.brain.ok and not self.abrain.busy:
            brief = self._brief(snap, flag, prof)
            if self.abrain.request(brief, user_line):
                self._last_brain = time.time()
                self.mem["expect_reply"] = bool(user_line)
        elif user_line and not self.brain.ok:
            pass  # ollama down: user chat goes unanswered, already warned

        self._flush_orders(snap)

    def _brief(self, snap, flag, prof):
        """Compact battle state for the LLM, with change annotations.

        Fairness: while the player is cloaked the model gets NO live data
        on them - only what was known at the moment they vanished."""
        p = snap.raw
        hidden = p.get("YouCloak", 0) == bridge.CLOAK_CLOAKED
        # RngYou is the real slot key ("Rng"/"Closing" never existed - the
        # model has been reading -1 range in every brief until 08-31).
        rng_now = flag.get("RngYou", -1)
        closing = 0
        if rng_now >= 0:
            prev_r = self.mem.get("brief_prev_rng")
            if prev_r is not None:
                closing = prev_r - rng_now
            self.mem["brief_prev_rng"] = rng_now
        if not hidden:
            self.mem["lk"] = {"hull": p.get("YouHull", -1),
                              "rng": rng_now}
        lines = []
        lines.append("Tick %d." % snap.tick)
        if flag.get("Man") == bridge.CMD_WARP:
            lines.append("You are on a WARP BREAK (%s) - it returns you to "
                         "the fight by itself; no orders needed."
                         % ("mending far out" if flag.get("WB") == 2
                            else "warping out"))
        elif flag.get("Man") in (bridge.CMD_BREAK, bridge.CMD_CIRCLE,
                                 bridge.CMD_GUARD):
            lines.append("You are mid-%s - it runs its full time by itself; "
                         "do not re-order it."
                         % bridge._CMD_NAMES.get(flag.get("Man"), "manoeuvre"))
        banned = self.mem.get("tsub_banned")
        if banned:
            lines.append("Target %s is OFF the list this fight - it landed "
                         "nothing in reach. Your guns are on hull."
                         % "/".join(banned))
        if snap.tick < self.mem.get("cloak_jam_until", -1):
            lines.append("Your cloak would not go dark - cloak orders are "
                         "refused for now. Fight visible.")
        ai_intel = intel.contact_ai_intel(snap.foes)
        if ai_intel:
            lines.append(ai_intel)
        own = ("Your ship: hull %d%%, shields F%d/A%d/L%d/R%d/T%d/B%d, "
               "battery %d%%, cloak %s"
               % (flag.get("Hull", -1),
                  flag.get("ShF", -1), flag.get("ShA", -1),
                  flag.get("ShL", -1), flag.get("ShR", -1),
                  flag.get("ShT", -1), flag.get("ShB", -1),
                  flag.get("Batt", -1), _cloak_word(flag.get("Cloak", 0))))
        if hidden:
            own += ". Range to enemy: UNKNOWN (they are cloaked)."
        else:
            own += (", range to enemy %dm, closing %dm per tick."
                    % (rng_now, closing))
        lines.append(own)
        lines.extend(self._own_systems_lines(flag))
        if hidden:
            lk = self.mem.get("lk", {})
            lines.append(
                "Enemy (player): CLOAKED - no sensor data. Last known: "
                "hull %d%%, range %dm. You cannot target or track them."
                % (lk.get("hull", -1), lk.get("rng", -1)))
        else:
            lines.append(
                "Enemy (player): hull %d%%, shields F%d/A%d/P%d/S%d/D%d/V%d, "
                "cloak %s."
                % (p.get("YouHull", -1),
                   p.get("YouShF", -1), p.get("YouShA", -1),
                   p.get("YouShL", -1), p.get("YouShR", -1),
                   p.get("YouShT", -1), p.get("YouShB", -1),
                   _cloak_word(p.get("YouCloak", 0))))
            faces = {"fore": p.get("YouShF", 100), "aft": p.get("YouShA", 100),
                     "port": p.get("YouShL", 100),
                     "starboard": p.get("YouShR", 100),
                     "dorsal": p.get("YouShT", 100),
                     "ventral": p.get("YouShB", 100)}
            weak = min(faces, key=faces.get)
            if faces[weak] < 60:
                lines.append("Their weakest shield facing: %s at %d%%."
                             % (weak, faces[weak]))
            lines.append(
                "Enemy subsystems: weapons %d%%, torpedoes %d%%, engines "
                "%d%%%s, sensors %d%%, power %d%%."
                % (p.get("YouWeap", -1), p.get("YouTorp", -1),
                   p.get("YouEng", -1),
                   (" (%d of %d impulse engines out)"
                    % (p.get("YouEngOut", 0), p.get("YouEngN", 0))
                    if p.get("YouEngOut") else ""),
                   p.get("YouSens", -1), p.get("YouPow", -1)))
        if 0 <= flag.get("Sens", 100) < 15:
            lines.append("YOUR SENSORS ARE DESTROYED - you are blind. Your "
                         "ship is disengaging until repairs restore them. "
                         "No attack orders will work; speak accordingly.")
        if p.get("YouCloak", 0) == bridge.CLOAK_CLOAKED:
            lines.append("They are cloaked: your weapons CANNOT target them "
                         "until they decloak. Do not cloak yourself - stay "
                         "visible, keep power up, strike first when they "
                         "reappear.")
        n_foes = len(snap.live_foes())
        if n_foes:
            lines.append("Enemy escorts/fighters on the board: %d." % n_foes)
        if self.mem.get("duel_pure"):
            lines.append("This is SINGLE COMBAT by agreement: no fighters, "
                         "no escorts, no reinforcements. Win it ship to "
                         "ship.")
        cap = prof.get("fleet_cap")
        if cap and not self.mem.get("duel_pure"):
            lines.append("Your battle group: %d of %d ships deployed. "
                         "You may order reinforce when the fight demands "
                         "it." % (len(snap.live_fleet()), cap))
        if self.mem.get("is_station"):
            lines.append("You are a STATION: break/circle/guard/cloak do "
                         "nothing for you. Fight with your arcs and your "
                         "battle group.")
        # change detection
        changes = []
        prev = self._prev
        cur = {"hull": flag.get("Hull", -1),
               "phull": p.get("YouHull", -1),
               "pcloak": p.get("YouCloak", 0),
               "foes": n_foes,
               "allies": len(snap.live_fleet())}
        if prev:
            if cur["hull"] < prev["hull"] - 3:
                changes.append("you took %d%% hull damage"
                               % (prev["hull"] - cur["hull"]))
            if cur["phull"] < prev["phull"] - 3 and not hidden:
                changes.append("you dealt %d%% hull damage to the enemy"
                               % (prev["phull"] - cur["phull"]))
            if cur["phull"] > prev["phull"] + 2 and not hidden:
                changes.append("the enemy is REPAIRING - their hull "
                               "climbed %d%% since your last turn"
                               % (cur["phull"] - prev["phull"]))
            for mark in (75, 50, 25):
                if not hidden and cur["phull"] < mark <= prev["phull"]:
                    changes.append("the enemy's hull has fallen below %d%%"
                                   % mark)
                    break
            if cur["pcloak"] == bridge.CLOAK_CLOAKED and \
                    prev["pcloak"] != bridge.CLOAK_CLOAKED:
                changes.append("the enemy just cloaked")
            if cur["pcloak"] != bridge.CLOAK_CLOAKED and \
                    prev["pcloak"] == bridge.CLOAK_CLOAKED:
                changes.append("the enemy just decloaked")
            if cur["foes"] < prev["foes"]:
                changes.append("%d enemy fighter(s) destroyed"
                               % (prev["foes"] - cur["foes"]))
            if cur["allies"] < prev.get("allies", cur["allies"]):
                changes.append("your battle group lost %d ship(s)"
                               % (prev["allies"] - cur["allies"]))
            elif cur["allies"] > prev.get("allies", cur["allies"]):
                changes.append("%d reinforcement(s) have arrived and "
                               "joined your line"
                               % (cur["allies"] - prev["allies"]))
        self._prev = cur
        # the say channel follows the facts: comms when something
        # happened, a rare earned taunt otherwise, silence by default
        if changes:
            lines.append("Since your last turn: " + "; ".join(changes) + ".")
            lines.append("If you transmit, make it a short comms line "
                         "grounded in one of those facts.")
        elif self.banter and random.random() < prof["taunt_chance"]:
            lines.append("A pointed taunt would land now - one line, tied "
                         "to the current state of the battle.")
        else:
            lines.append("Nothing notable changed - transmit nothing "
                         "unless you have a factual comms line.")
        return "\n".join(lines)

    def _drive_hold(self, orders, flag):
        """No running on a crippled drive, from any source.

        One logged duel: the brain's breaks were already turned to guard, but the
        reflex - the cloak escape, the overshoot, the player-cloak
        reaction - sent six more breaks after her drive was hit, four of
        them with her top speed at 0.  A break on a dead drive goes nowhere
        and rebuilds her AI.  With the drive dead (top speed 0) she cannot
        turn either, so guard and circle are held too.  break 0 (cancel)
        always goes through."""
        if not orders or not flag or not self.mem.get("drive_hit"):
            return orders
        dead = flag.get("ImpMax", -1) == 0
        # One logged duel: the hold kept her aft off his guns while visible, but dark
        # there is no aft to show - and the stalk's break to open the range
        # was held 105 times while she sat cloaked 14-38m from him.
        dark = flag.get("Cloak") == bridge.CLOAK_CLOAKED
        drops = self.mem.setdefault("gate_drops", {})
        out = []
        for o in orders:
            why = None
            if o.cmd == bridge.CMD_BREAK and o.arg and not (dark and not dead):
                why = "breaks held (drive hit)"
            elif dead and o.cmd in (bridge.CMD_GUARD, bridge.CMD_CIRCLE):
                why = "manoeuvres held (drive dead)"
            if why:
                drops[why] = drops.get(why, 0) + 1
                continue
            out.append(o)
        return out

    def _own_systems_lines(self, flag):
        """Her own damage, for the brief.  Until the drive work the brief carried her
        hull and shields only - she could not see her weapons, her engines
        or her speed, so she could not act on any of them (player feedback:
        "the model needs to be able to see the damage being taken")."""
        eng = flag.get("Eng", -1)
        n, out = flag.get("EngN", 0) or 0, flag.get("EngOut", 0) or 0
        eng_txt = "%d%%" % eng
        if n:
            eng_txt += " (%d of %d engines out)" % (out, n) if out else \
                " (%d engines)" % n
        if (flag.get("EngRep", -1) or 0) > 0:
            eng_txt += ", repair bay on %d" % flag["EngRep"]
        line = ("Your systems: weapons %d%%, torpedoes %d%%, impulse %s, "
                "warp %d%%, sensors %d%%, power %d%%"
                % (flag.get("Weap", -1), flag.get("Torp", -1), eng_txt,
                   flag.get("Warp", -1), flag.get("Sens", -1),
                   flag.get("Pow", -1)))
        imp, full = flag.get("ImpMax", -1), flag.get("ImpFull", -1)
        if imp >= 0 and full > 0:
            line += ", top speed %.1f of %.1f" % (imp / 10.0, full / 10.0)
        lines = [line + "."]
        shape = self.mem.get("hull_shape") or {}
        if self.mem.get("drive_hit") and imp == 0:
            lines.append(
                "YOUR IMPULSE DRIVE IS DEAD (top speed 0). You cannot move "
                "or turn: no manoeuvre orders, they are held. Fire what "
                "bears, power to weapons or shields, repairs are running.")
        elif self.mem.get("drive_hit"):
            lines.append(
                "YOUR IMPULSE DRIVE IS HIT. You cannot outrun or out-turn "
                "him now: no breaks - they show him your aft and go nowhere. "
                "Keep your bow on him (guard turns your strongest facing "
                "in), keep firing, repairs are running.")
        aft = flag.get("ShA", 100)
        if shape.get("front_heavy") and 0 <= aft < doctrine.AFT_GUARD_PCT:
            lines.append(
                "Your AFT shield is at %d%% on a front-loaded hull - turn "
                "your bow back onto him." % aft)
        return lines

    def _cloak_authority(self, orders, snap, flag, prof=None):
        """Cloak TIMING and ENTRY belong to the rules layer, not the model.

        - Drop decloaks inside the minimum hold (the round-4/5 cloak-pop).
        - Drop redundant cloak-ons while already cloaked.
        - Drop cloak-ons that fit neither play: ESCAPE (hull below
          doctrine.CLOAK_HURT_HULL) nor AMBUSH (healthy at 1200m+).  A
          healthy mid-brawl cloak is a free kill and the model kept
          requesting it.
        - A cloak-on that passes is handed to the playbook (phase, clock,
          defensive stance) exactly as if the reflex had entered it.

        One logged duel: this let a shield-only wound through (98% hull, one
        facing at 14%) after the reflex had stopped doing that, and the
        brain's cloak set no phase and no clock.  The playbook never owned
        it: she sat dark on top of him for 60 ticks doing nothing, and her
        last cloak was popped one tick after it went dark on a clock left
        over from tick 175."""
        cloaked_at = self.mem.get("own_cloaked_at", 0)
        my_cloak = flag.get("Cloak", 0) if flag else 0
        in_cycle = my_cloak in (bridge.CLOAK_CLOAKING, bridge.CLOAK_CLOAKED)
        held = snap.tick - cloaked_at if cloaked_at else 999
        hull = flag.get("Hull", 100) if flag else 100
        # It is the other way round: fleet slots carry Rng/Closing and only
        # foe slots carry RngYou/RngNear.  Reading RngYou off the flagship is
        # what kept ambush_worthy from ever becoming true.
        rng = bridge.slot_range(flag)
        prev = self.mem.get("cloak_prev_rng_auth", rng)
        self.mem["cloak_prev_rng_auth"] = rng
        closing = bridge.slot_closing(flag, prev - rng)
        # While the playbook has the ship (a cloak phase is set, or the cloak
        # is up), the model's STANCE orders are dropped too: every stance
        # but defensive/ambush carries UseCloaking 0, and in a logged duel the model's
        # "stance balanced" decloaked her nine times mid-mend.  Its decloaks
        # are dropped for the same reason - the playbook chooses the strike.
        phase = bool(self.mem.get("cloak_phase"))
        allowed = prof["allowed"] if prof else ()
        out = []
        entered = None
        for o in orders:
            if o.cmd == bridge.CMD_STANCE and (phase or in_cycle or entered):
                continue
            if o.cmd == bridge.CMD_CLOAK:
                if o.arg == 0 and (phase or (in_cycle and held < 12)):
                    continue
                if o.arg == 1:
                    if in_cycle or phase or entered:
                        continue
                    if self.mem.get("duel_cloak_play") == "off":
                        continue
                    if snap.tick < self.mem.get("cloak_jam_until", -1):
                        drops = self.mem.setdefault("gate_drops", {})
                        k = "cloak-ons while the cloak was jammed"
                        drops[k] = drops.get(k, 0) + 1
                        continue
                    # shields alone are guard's job, as in the reflex
                    escape_worthy = hull < doctrine.CLOAK_HURT_HULL
                    ambush_worthy = rng > 1200 and closing >= 0
                    if not (escape_worthy or ambush_worthy):
                        continue
                    entered = "escape" if escape_worthy else "stalk"
                    m = self.mem
                    m["cloak_phase"] = entered
                    m["own_cloaked_at"] = snap.tick
                    m["cloak_ordered_tick"] = snap.tick
                    m["cloak_hull_at"] = hull
                    if entered == "escape":
                        # balanced/aggressive carry UseCloaking 0: without
                        # defensive the game's AI decloaks her to shoot (a logged duel)
                        out = [x for x in out if x.cmd != bridge.CMD_STANCE]
                        if "stance" in allowed:
                            out.append(bridge.stance("defensive", o.ship))
                        out.append(o)
                        if "repair" in allowed:
                            out.append(bridge.repair(o.ship))
                        continue
            out.append(o)
        if entered:
            drops = self.mem.setdefault("gate_drops", {})
            k = "brain cloaks handed to the playbook"
            drops[k] = drops.get(k, 0) + 1
        return out

    BRAIN_MAN_GAP = 30          # ticks between brain-ordered manoeuvres
    BRAIN_WARP_GAP = 90         # ...and between brain-ordered warp breaks
    BRAIN_WARP_HULL = 60        # a warp break needs a hull wound below this
    BRAIN_BREAK_HULL = 60       # at/above this a brain "break" becomes guard

    def _brain_gate(self, orders, snap, flag, prof):
        """Manoeuvres and warp belong to whoever is flying, once.

        One logged duel: the brain ordered "break 10" 25 times, every 6 ticks,
        mostly while the last break was still running (Man 7) - each one a
        fresh SetAI, so the ship never finished a manoeuvre, never settled
        its guns, and aborted every cloak it tried.  It opened at 100% hull
        with "warp 60" and closed with three more warps in twelve ticks.
        The battle's own banked lesson said "do not leave the knife range,
        break to rotate rather than to withdraw".  So:
          - nothing that rebuilds the AI while the cloak playbook has the
            ship (a phase is set, or the cloak is ramping/up);
          - no new break/circle/guard while one is running, nor within
            BRAIN_MAN_GAP ticks of the brain's last;
          - a brain break with the hull still sound becomes a guard - turn
            a fresh facing in, do not show them the aft;
          - warp only with a hull wound, never on top of a running one,
            and not again for BRAIN_WARP_GAP ticks."""
        if not orders or flag is None:
            return orders
        tick = snap.tick
        man = flag.get("Man", 0)
        hull = flag.get("Hull", 100)
        cloak = flag.get("Cloak", 0)
        playbook = bool(self.mem.get("cloak_phase")) or cloak in (
            bridge.CLOAK_CLOAKING, bridge.CLOAK_CLOAKED,
            bridge.CLOAK_DECLOAKING)
        drops = self.mem.setdefault("gate_drops", {})

        def drop(why):
            drops[why] = drops.get(why, 0) + 1

        rebuild = (bridge.CMD_BREAK, bridge.CMD_CIRCLE, bridge.CMD_GUARD,
                   bridge.CMD_TARGET, bridge.CMD_FOCUS, bridge.CMD_WARP)
        out = []
        for o in orders:
            if o.cmd == bridge.CMD_CLOAK and o.arg == 1 and \
                    tick < self.mem.get("cloak_jam_until", -1):
                drop("cloak-ons while the cloak was jammed")
                continue
            if o.cmd in rebuild and playbook:
                drop("orders while the cloak playbook had the ship")
                continue
            if o.cmd in (bridge.CMD_BREAK, bridge.CMD_CIRCLE,
                         bridge.CMD_GUARD):
                if man:
                    drop("manoeuvres on top of a running one")
                    continue
                if tick - self.mem.get("brain_man_tick", -9999) < \
                        self.BRAIN_MAN_GAP:
                    drop("manoeuvres inside %d ticks of the last"
                         % self.BRAIN_MAN_GAP)
                    continue
                drive = bool(self.mem.get("drive_hit"))
                if o.cmd == bridge.CMD_BREAK and \
                        (hull >= self.BRAIN_BREAK_HULL or drive) and \
                        "guard" in prof["allowed"] and \
                        not self.mem.get("is_station"):
                    o = bridge.guard(min(int(o.arg or 10), 12), o.ship)
                    drop("breaks turned to guard (drive hit)" if drive and
                         hull < self.BRAIN_BREAK_HULL
                         else "breaks turned to guard (hull sound)")
                self.mem["brain_man_tick"] = tick
            elif o.cmd == bridge.CMD_WARP:
                if hull >= self.BRAIN_WARP_HULL:
                    drop("warp breaks at %d%%+ hull" % self.BRAIN_WARP_HULL)
                    continue
                if man == bridge.CMD_WARP or \
                        tick - self.mem.get("brain_warp_tick", -9999) < \
                        self.BRAIN_WARP_GAP:
                    drop("repeat warp orders")
                    continue
                self.mem["brain_warp_tick"] = tick
            out.append(o)
        return out

    # ------------------------------------------------------------- debrief

    def _finish(self, result):
        prof = doctrine.PROFILES[self.plan["difficulty"]] if self.plan \
            else doctrine.PROFILES["officer"]
        curve = self.hull_curve
        duration = 0
        if len(curve) >= 2:
            duration = curve[-1][0] - curve[0][0]
        try:
            # events: without them every duel scored "cloaks 0" and the
            # cloak lessons below never fired (a logged duel cloaked twice, read 0)
            analysis = evaluate.analyze(
                self.track, {"result": result}, events=self.events,
                reach=(intel.weapon_profile(self.plan.get("ship_class", ""))
                       or {}).get("reach", 0))
            learned = learning.observe_battle(
                analysis, (self.plan or {}).get("ship_class", ""))
        except Exception:
            analysis, learned = {"metrics": {}, "facts": [],
                                 "candidates": []}, []
        self.debrief = {
            "result": result,
            "analysis": analysis,
            "lessons_outcome": learned,
            "battle_no": self.battle_no,
            "difficulty": prof["label"],
            "ship": self.plan["ship_name"] if self.plan else "",
            "ship_class": self.plan["ship_class"] if self.plan else "",
            "duration_ticks": duration,
            "curve": curve[-600:],
            "finished_at": time.time(),
        }
        ob = self.brain.online
        if ob is not None and (ob.stats["calls"] or ob.stats["fails"]):
            self.debrief["brain"] = dict(ob.stats, model=ob.model,
                                         effort=ob.effort)
        self.phase = "debrief"
        self.say("system", "battle over: %s (%d ticks)." % (result, duration))
        if not result.startswith("stood down"):
            last_t = curve[-1][0] if curve else 0
            if result.startswith("victory"):
                self.wk.mark_dead("player", last_t)
            elif result.startswith("defeat"):
                self.wk.mark_dead("flag", last_t)
            self._bank_weakness()
        if ob is not None and ob.summary():
            self.say("system", ob.summary())
        gate = self.mem.get("gate_drops") or {}
        if gate:
            self.debrief["gate_drops"] = dict(gate)
            self.say("system", "order gate: " + ", ".join(
                "%d %s" % (n, why) for why, n in sorted(gate.items())) + ".")
        self._save_log()
        learning.record_battle(self.debrief, {
            "player_ship": (self.plan or {}).get("player_ship", ""),
            "player_cloaked": bool(self.mem.get("obs_player_cloaked")),
            "player_min_hull": self.mem.get("obs_player_min_hull", 100),
            "fighters_seen": self.mem.get("obs_fighters_seen", 0),
        })
        self._debrief_async(result)
        # one last in-character line (banter mode), then the lesson
        if self.brain.ok:
            if self.banter:
                won = result.startswith("victory")
                self.abrain.request(
                    "The battle is over. Result for you: %s. Give one short "
                    "closing line in character - %s. No orders."
                    % ("VICTORY" if won else "DEFEAT",
                       "gracious in triumph" if won else
                       "defiant or respectful in defeat"))
            threading.Thread(target=self._collect_last_words,
                             daemon=True).start()

    def _collect_last_words(self):
        for _ in range(40 if self.banter else 0):
            time.sleep(0.5)
            res = self.abrain.poll()
            if res:
                if res["say"]:
                    self.say("ai", res["say"])
                break
        # distill one lesson for the persistent opponent model
        try:
            known = [l["text"] for l in
                     learning.stats().get("lessons2", [])][:8]
            facts = []
            if self.debrief:
                facts = self.debrief.get("analysis", {}).get("facts", [])
            prompt = ("Battle over. Measured facts from the battle log: %s. "
                      "Lessons you ALREADY hold: %s. If this battle only "
                      "confirms one of those, reply with the single word "
                      "CONFIRMED in \"say\". Otherwise state ONE genuinely "
                      "NEW concrete tactical lesson (max 22 words, no "
                      "rephrasing of the list) in \"say\". No orders."
                      % ("; ".join(facts) or "none recorded",
                         " | ".join(known) or "none yet"))
            res = self.brain.decide(prompt, want_say=True)
            say = (res.get("say") or "").strip()
            if say and "confirmed" not in say.lower()[:24]:
                learning.add_lesson(
                    say, ship_class=(self.debrief or {}).get("ship_class", ""))
        except Exception:
            pass

    def rearm(self):
        """From debrief back to idle, keeping the last plan staged."""
        with self._lock:
            if self.phase == "debrief":
                self.phase = "idle"
                self.probe = bridge.LinkProbe(self.game_root)
                self.say("system", "standing by for the next battle.")

    def _save_log(self):
        try:
            path = os.path.join(LOG_DIR, "battle_%d_%d.json"
                                % (int(time.time()), self.battle_no))
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"debrief": self.debrief, "chat": self.chat,
                           "track": self.track[-2400:],
                           "events": self.events[-400:],
                           # (seq, wall-time, tick, source, [orders]) -
                           # what was actually sent, and by which layer
                           "orders": self.writer.batches_since(
                               getattr(self.writer, "mark", 0))[-600:]},
                          f, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------ state api

    def public_state(self):
        """What the browser may see.  Deliberately excludes AI tactical
        detail beyond what the player's own HUD would show."""
        with self._lock:
            plan = dict(self.plan) if self.plan else None
        snap = self._last_snap
        out = {
            "phase": self.phase,
            "plan": plan,
            "link_up": self.probe.alive(snap) if snap else False,
            "ollama_ok": self.brain.ok,
            "brain_mode": self.brain_mode(),
            "brain_src": self.brain.served,
            "brain_online_ok": (self.brain.online.ok
                                if self.brain.online is not None else None),
            "battle_no": self.battle_no,
            "debrief": self.debrief,
        }
        if snap and self.phase in ("fighting", "debrief"):
            out["player"] = {
                "hull": snap.raw.get("YouHull", -1),
                "shields": snap.raw.get("YouShAvg", -1),
                "alive": snap.player_alive,
            }
            if (plan or {}).get("mode") == "ally":
                ws = snap.fleet.get(self.mem.get("wing", -1))
                if ws:
                    out["ai_ship"] = {"name": ws.get("Name", ""),
                                      "alive": ws["st"] == bridge.SLOT_ALIVE,
                                      "hull": ws.get("Hull", -1),
                                      "mode": self.mem.get("wing_mode", "")}
                out["reds_left"] = len([s for s in snap.live_fleet().values()
                                        if s.get("Side", 0) == 0])
                return out
            flag = snap.fleet.get(self.mem.get("flag", 0))
            if flag:
                out["ai_ship"] = {
                    "name": flag.get("Name", ""),
                    "alive": flag["st"] == bridge.SLOT_ALIVE,
                }
        return out


def _cloak_word(code):
    return {0: "no device", 1: "ready", 2: "cloaking",
            3: "CLOAKED", 4: "decloaking"}.get(code, "?")
