# DUELAI

**An AI opponent and wingman director for *Star Trek: Bridge Commander* (2002).**

> **Status: ongoing - work in progress.** DUELAI is actively developed.
> Expect frequent changes, some features are experimental or not yet proven
> in battle, and the doctrine, lessons and tuning keep evolving. See
> [Project status](#project-status).

DUELAI flies AI ships against you in Bridge Commander's Quick Battle, live,
from outside the game. A Python service reads the game's telemetry every
second, decides what its ships should do, and writes orders back. You watch
(and talk to) the opposing commander in an LCARS-style browser console.

- **Duels** at six difficulty levels, from target practice to a fleet
  action, with a deterministic doctrine layer for split-second reflexes and
  a language model for the judgement calls.
- **Two brains:** a local model through [Ollama](https://ollama.com)
  (offline, free) or Claude through the Anthropic API (online), with the
  local model as an automatic fallback.
- **A strategist:** optionally, Claude writes a battle plan when you stage a
  fight and a debrief afterwards.
- **Learning between battles:** lessons about you, about each hull and
  about what actually wins are kept in local JSON files and fed back into
  the next fight.
- **Sparring:** the game fights itself, AI side against AI side, to measure
  matchups, to learn which cloak tactics win per hull, and to pit Claude
  against the local model ("brains sparring").
- **Ally mode:** an AI wingman on your side against an AI-spawned enemy
  force.

This is an unofficial fan project. No game files are included - you need
your own copy of the game. See the [disclaimer](#disclaimer).

---

## How it fits together

```
 Bridge Commander (stbc.exe, embedded Python 1.5.2)
 ┌───────────────────────────────────────────────┐
 │ scripts/Custom/Autoload/ClaudeCommander.py    │  "Duel Claude" button on the
 │   1 Hz tick: telemetry -> claude_state.cfg     │  TACTICAL menu arms the link
 │   polls claude_orders.cfg -> runs orders       │
 └──────────────┬─────────────────▲──────────────┘
      state file│                 │orders file      (both in the game folder)
 ┌──────────────▼─────────────────┴──────────────┐
 │ duelai/bridge.py     parse state, write order batches (Python 3)        │
 │ duelai/director.py   battle lifecycle: stage -> arm -> fight -> debrief │
 │   doctrine.py   reflexes per difficulty (break, power, cloak, autobreak)│
 │   brain.py      the commander: prompts, JSON orders, gates              │
 │   intel.py      hull database -> doctrine and weakness briefs           │
 │   learning.py / evaluate.py / weakness.py / cloaklearn.py               │
 │   sparring.py / sparbrains.py / ally.py                                 │
 └──────┬───────────────────────────┬─────────────────────▲──────────────┘
        │ every decision            │ plan + debrief      │ JSON API
 ┌──────▼──────────────┐   ┌────────▼─────────────┐ ┌─────┴──────────────┐
 │ Ollama (local)      │   │ Claude (strategist,  │ │ server.py :8790    │
 │ or Claude (online,  │   │ optional)            │ │ ui/index.html      │
 │ online.py)          │   └──────────────────────┘ │ LCARS console      │
 └─────────────────────┘                            └────────────────────┘
```

- **Game side.** `ClaudeCommander.py` is an Autoload script. It adds the
  "Duel Claude" button, spawns ships on request (enemy or friendly side, up
  to 24), and runs order codes: stance, subsystem target, power split,
  cloak, break, circle, guard, focus fire, repair, autobreak, fighter
  launch, warp break and more. Python `open()` does not work in this
  engine build, so both files go through the engine's own config API.
- **Bridge.** `bridge.py` is the Python 3 half of that protocol.
- **Director.** `director.py` runs the state machine and the poll loop
  (about 1.5 s), records a per-poll track, and owns every order that
  reaches the game.
- **Doctrine.** `doctrine.py` holds the difficulty profiles and the
  reflexes that cannot wait for a model: break-offs, power shifts, cloak
  play, the hold-fire watchdog, drive and aft protection.
- **Brain.** `brain.py` builds the commander's prompt from the battle state,
  the hull intel and the lessons, and turns its JSON reply into orders. A
  gate between brain and game drops orders that would hurt (re-sending a
  stance the ship already has, manoeuvres mid-cloak, running on a dead
  drive and so on).
- **Brains.** Offline: any Ollama chat model. Online: Claude via the
  Messages API (`online.py`), with a cached system prompt and a JSON schema
  for the orders. Three failed online calls in a row park it until the next
  battle; every failed call falls back to Ollama.

## Requirements

- **Star Trek: Bridge Commander** (retail or digital), on Windows.
- **The Orion Delta 4.1 mod** installed over it. DUELAI was built and tested
  on Orion Delta RC4.1 only. `ClaudeCommander.py` uses its Bedrock ship
  plugins and `ftb` GUI helpers, and the circle-fix patch was made against
  that mod's copy of the AI script.
- **Python 3.8 or newer** on the same PC as the game (developed on 3.12).
  No pip packages are needed.
- **Ollama** (optional but recommended) with a chat model, e.g.
  `ollama pull qwen2.5:7b` (default) or `qwen3:8b` (smarter, slower).
  Without it the opponent fights on doctrine alone and stays silent.
- **An Anthropic API key** (optional) for the online brain, the strategist
  and the Claude side of brains sparring.
- **A hull database** (`data/ships.json` + `data/torpedoes.json`) extracted
  from your own install. See [Hull database](#hull-database).
- **The LCARS Classic theme** from TheLCARS.com for the console's look. See
  [`lcars/README.md`](lcars/README.md).
- For the installer: bash and GNU `patch` (Git Bash on Windows has both).

## Setup

1. **Install the game side** (with the game closed):

   ```bash
   bash game/install.sh "C:/Games/Star Trek Bridge Commander"
   ```

   This copies `ClaudeCommander.py` into `scripts/Custom/Autoload/` and
   applies the patches in `game/patches/`, backing up every file it touches
   as `*.pre-duelai.bak`. `bash game/install.sh "<game folder>" undo`
   reverses it. It refuses to run while `stbc.exe` is running.

2. **Add the LCARS theme** files to `lcars/` (see `lcars/README.md`).

3. **Add your hull database** at `data/ships.json` and
   `data/torpedoes.json`.

4. **Configure.** Copy `data/config.example.json` to `data/config.json`
   (it is gitignored), or use environment variables:

   | variable | meaning | default |
   |---|---|---|
   | `STBC_GAME_ROOT` | folder that holds `stbc.exe` | `game_root` in config, else the current folder |
   | `ANTHROPIC_API_KEY` | Claude API key (wins over `api_key` in config) | none = offline only |
   | `OLLAMA_URL` | Ollama base URL | `http://localhost:11434` |
   | `DUELAI_PORT` | console port | `8790` |

5. **Start it:** double-click `run_duelai.bat`, or `python server.py`, then
   open <http://localhost:8790>. The server listens on 127.0.0.1 only and
   has no authentication; do not expose it to a network.

### Playing a duel

1. In the console, pick a difficulty, a ship (or dealer's choice), a persona
   and your own ship, then **stage battle**.
2. Start the game (the console's launch button starts `stbc.exe -TestMode`),
   open **Quick Battle**, add your ship plus one filler ship (Quick Battle
   will not start with nobody else on the map), and launch.
3. Press **Duel Claude** on the TACTICAL menu. The console sees the link
   arm, spawns its ship and opens the channel.
4. When someone dies you get a debrief: result, hull curves, what was
   learned.

### Configuration (`data/config.json`)

| key | meaning |
|---|---|
| `game_root` | folder that holds `stbc.exe` |
| `model` | Ollama model for the offline brain and the local sparring side |
| `banter` | `false` = orders-only prompt (faster); `true` = in-character chat |
| `brain_mode` | `offline` (Ollama decides) or `online` (Claude decides, Ollama covers failures) |
| `online_model`, `online_effort` | Claude model and effort for live decisions (default `claude-sonnet-5-5`, `low`) |
| `api_key` | Anthropic key, if you do not use `ANTHROPIC_API_KEY` |
| `api_model`, `strategist`, `strategist_effort` | the plan/debrief model (default `claude-opus-5-5`, on, `high`) |
| `god_ships` | extra hull ids barred from `nogods` sparring drafts |
| `auto_race_exclude` | races the automatic sparring picker never drafts unprompted |

The Settings page in the console edits the brain mode, models, effort and
key and writes them to `data/config.json`.

Rough costs measured with a 9 KB battle prompt: Sonnet 5.5 at low effort
about 4 s and $0.006 a decision, Opus 5.5 at low effort about 5 s and
$0.013. The end of each duel prints the calls, latency and cost.

### Difficulty

| level | behaviour |
|---|---|
| holodeck | target practice - spawns and wanders on the stock AI |
| cadet | gentle - passive stance, no focus fire, slow decisions |
| officer | a fair fight - balanced doctrine, some reflexes |
| captain | full doctrine - fast reflexes, subsystem targeting |
| kobayashi | no-win scenario - escorts, counter-picks, maximum cadence |
| armada | fleet action - an opening line of six, relentless waves |

Higher levels fight through more damage before breaking off, not less.

## How lessons work

All learning is local JSON under `data/` (gitignored). Delete a file to wipe
that memory.

- **`learning.json`** - every finished duel: the result, your ship, your
  cloak habits, and structured lessons. After each battle `evaluate.py`
  measures the fight from the per-poll track (range bands, time in reach,
  cloak dividends, when it was decided). The model, or the strategist,
  writes at most one new lesson or confirms an existing one. Lessons carry
  confirmation counts and per-hull wording: a range lesson reaches a duel
  only in the AI hull's own wording, or a similar-reach hull's.
- **`weakness.json`** - per hull, which subsystem knocked out early (while
  hull is still healthy) is usually followed by death. Once the evidence is
  there, the brain is told to go for it on your hull and to protect it on
  its own.
- **`cloak_learning.json`** - which cloak play wins per hull (`off`, `mend`,
  `alpha`, `wolfpack`), from sparring rounds, chosen by Thompson sampling.
  A duel uses a hull's verdict once it has 8 or more banked rounds.
- **`brains.json`** - the brains-sparring record, each commander's own
  lessons per matchup, and lessons indexed by hull. The counted record
  ("the record") opens every lesson block and outranks the models' own
  conclusions. Only Claude-written sparring lessons reach duels.
- **`sparring.json`** - the stock-AI matchup table.
- **`logs/`** - one JSON record per duel, sparring round and ally battle.

## Sparring

Type a spec into the scenario box (or use the Sparring tab):

```
spar borg vs federation 2v2 x3 tng
spar feklhr vs defiantnx 1v1 x10 @1500 cloak=both
spar cardassian vs dominion 1v1 x4 brains=swap
```

- `<red> vs <blue>`: race names or exact hull ids; `NvN` sizes (up to the
  side maximum); `xN` rounds; an era (`tng`, `tmp`, `ent`); `@<metres>`
  engagement distance; `nogods` or `gods`; `red=`, `blue=` or `stance=` to
  fix stances.
- **Stock sparring:** both sides fly the game's own AI and DUELAI referees,
  records and banks the matchup. Auto-pick walks the least-tested pairing.
- **`cloak=red|blue|both`:** cloak-capable hulls on those sides fly
  DUELAI's cloak playbook under one play per round, and the result trains
  the cloak learner.
- **`brains=swap|red|blue`:** Claude commands one side and the local model
  the other, each issuing squadron orders every 8 s on top of the stock AI,
  with the same prompt, brief and verbs. `swap` changes sides every round,
  so run an even number of rounds. A failed call is a lost turn, and a
  round with player interference, a lost link or a commander that failed
  more than a third of its calls is void. After each round both commanders
  write a lesson with their own model.

Sparring rounds that the player's ship touches (weapon hits, god mode) are
logged but never scored.

## Ally mode

`ally <race|hull> [N] [era] [@metres] [nogods] [wing=<race|hull>]`, or the
ALLY tab. DUELAI spawns 1-6 enemies and one wingman on your side. The
wingman is deterministic (no LLM round trip): it locks whatever you have
targeted, breaks off to defend you when your shields or hull drop or you
are being hammered, and repairs and turns defensive when it is hurt
itself.

## Hull database

`intel.py`, the ship picker, the dealer's-choice draft and sparring all read
`data/ships.json`: a JSON object keyed by hull class id, extracted from the
install's own hardpoint and ship scripts. Each entry carries fields such as
`class`, `ship_name`, `label`, `race`, `era`, `hclass`, `spawn` (the name
`loadspacehelper.CreateShip` accepts), `ai`, `hull`, `shields` and
`shield_regen` per facing, `phasers`, `pulses` and `tubes` (per emitter:
damage, arcs, ranges, charge), `ammo`, `cloak`, `subsystems`,
`max_speed`, `accel`, `battery`, `power_output` and `repair_points`.
`data/torpedoes.json` maps torpedo scripts to damage, speed and guidance.

Build them with `tools/extract_ships.py` from
[stbc-directive-fleet-ai](https://github.com/JackNorthman/stbc-directive-fleet-ai)
(`python extract_ships.py --install "<your install>\Star Trek Bridge Commander"`),
then copy `ships.json` and `torpedoes.json` into DUELAI's `data/`. The
generated files are not redistributed because they are derived from game
and mod data. Without `ships.json` the server
still starts, but the picker is empty and drafting does not work.

**Optional lore:** `python build_lore.py <memory-alpha-dump.xml>` builds
`data/lore.db`, an SQLite full-text index of a Memory Alpha XML dump, which
adds short in-universe briefs at staging. Memory Alpha content is CC BY-NC;
keep the index for personal use and credit Memory Alpha.

## Self-check

```
python -B selfcheck.py
```

About 230 offline checks of the doctrine, gates, cloak play, learning,
sparring and the game-side telemetry functions (parsed out of
`game/.../ClaudeCommander.py`). It is a regression suite rather than a
fixture-based test set: it needs your own `data/ships.json` containing the
stock and Orion Delta hulls it names, and it exits with code 2 if the file
is missing.

## Hard-won gotchas

The longer list is in [`docs/DESIGN-NOTES.md`](docs/DESIGN-NOTES.md).

- **Never edit game scripts while the game is running**, and delete the
  `.pyc` beside anything you change. `game/install.sh` does both checks.
- **The game remembers order sequence numbers in memory.** A restarted
  server can re-send a used number and nothing happens; the director
  detects a link that is armed but empty and re-sends.
- **Telemetry keys that do not exist read as -1** without complaint. Check
  key names against `ClaudeCommander.py`'s `ShipReport`.
- **Every stance, target or focus order rebuilds the ship's attack AI**, and
  any rebuild during a cloak ramp aborts the cloak. Cloak transitions mean
  no shields.
- **Spawned ships have no orientation** unless you set one. Before the
  spawn-facing fix one sparring side started with its back turned, which
  skewed every matchup table.
- **Point-defence mods that "defend allies" distort sparring.** The
  sparring flag on spawned ships exists partly so such scripts can skip
  them.
- **Top speed follows the power split**, so it is not a damage signal; read
  the child impulse engines instead.
- **Language models over-manoeuvre.** Commanders that micromanaged lost to
  ones that let the stock AI fight, so DUELAI holds power settings, drops
  repeat orders and gates manoeuvres.
- **Opus 5.x thinking counts against `max_tokens`.** Small caps truncate
  plans.
- **Keep the local model warm.** The first calls after a cold load time out;
  sparring warms it at staging and asks Ollama to keep it loaded.
- **Game-side code is Python 1.5.2:** no list comprehensions, no `+=`, no
  string methods (use the `string` module), and new module-level names
  must go on the relevant `global` lines.

## Project status

DUELAI is an ongoing, actively developed project and a work in progress.
Features, prompts, doctrine and defaults change often, and the lessons it
learns, and how they are weighed, keep evolving. Treat anything below
marked offline-tested as experimental.

| feature | status |
|---|---|
| Duels with the offline brain, doctrine layer, difficulty levels | flown in many battles |
| Learning file, evaluator, strategist plans and debriefs | flown |
| Online brain (Claude for live decisions, Ollama fallback) | flown in duels |
| Stock sparring, matchup table, player-interference quarantine | flown |
| Cloak learner (`cloak=`) | flown; evidence per hull is still thin |
| Brains sparring and per-commander lessons | flown |
| Sparring lessons in duels, per-hull lesson wording, "the record" first | flown; still being tuned |
| Spawn-facing fix (game side) | installed and flown |
| Engine telemetry (child engines) and engine repair queueing | flown; repairing fully destroyed engines is unverified |
| Drive and aft protection, brain gate | flown; still being tuned |
| Hull-weakness log | running; its effect on results is unmeasured |
| Warp break (order 18) | installed, but an AI warp break has not yet been seen to engage in game |
| Borg circle crash patch | installed; offline-tested |
| Ally mode | built and offline-tested only; not yet flown |
| Speech-to-text endpoint (`/api/stt`, needs `faster-whisper`) | server only; the current console does not use it |

## Repository layout

```
server.py             stdlib HTTP server + JSON API (127.0.0.1:8790)
run_duelai.bat        Windows launcher
selfcheck.py          offline regression checks (needs data/ships.json)
build_lore.py         optional Memory Alpha dump -> data/lore.db indexer
duelai/               the director, doctrine, brains, learning, sparring
ui/index.html         the console (vanilla JS, no build step)
lcars/                placeholder for the third-party LCARS theme
game/                 game-side script, patches and installer
data/                 config.example.json; local state lives here (gitignored)
docs/DESIGN-NOTES.md  design decisions and lessons learned
```

## Disclaimer

DUELAI is an unofficial, non-commercial fan project. It is not affiliated
with, endorsed by or sponsored by CBS Studios, Paramount, Activision or
Totally Games, or the authors of the Orion Delta mod. *Star Trek* and
related marks are trademarks of CBS Studios Inc. / Paramount. *Star Trek:
Bridge Commander* is a game by Totally Games, published by Activision.

No game files are included. `game/patches/` contains only the changed lines
of a game script (as a unified diff with a few lines of context), so you
need your own copy of the game and the mod to use it. The LCARS theme
belongs to TheLCARS.com and is not included either.

Licence: MIT (see `LICENSE`), covering the code in this repository only.
