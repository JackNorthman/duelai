# Design notes and lessons learned

Distilled from the development log. These are the decisions that shaped
DUELAI and the mistakes that taught them. Battle-by-battle records are not
included.

## Shape of the thing

- **Reuse one file bridge for everything.** The game side
  (`ClaudeCommander.py`) knows nothing about what sits on the other end of
  `claude_state.cfg` and `claude_orders.cfg`. Duels, sparring, brains
  sparring and Ally mode are all just different directors writing the same
  order batches.
- **Hybrid brain, not a smart model.** A deterministic doctrine layer
  (plain Python rules in `doctrine.py`) handles everything that has to
  happen within a tick: breaking off, power shifts, cloak timing, autobreak
  arming, holding fire. The language model makes slower judgement calls
  and, optionally, talks. Given "aft shield 22%, enemy behind you", an 8B
  model chose *aggressive* - that kind of decision belongs to the rules.
- **Difficulty gates both layers.** Cadence, the order vocabulary, which
  reflexes run, escorts and fighter wings. It is not just a prompt change.
- **Private orders, public chat.** Every brain reply is a validated JSON
  order block plus an optional line of chat, so the opponent can talk
  without giving its intentions away.
- **Stdlib only.** No pip dependencies for the core; the Claude Messages
  API and Ollama are both plain JSON over HTTP.
- **Opt-in cloud.** The Claude strategist (plans and debriefs) and the
  online brain are optional. Every Claude failure degrades to the local
  stack, and the local stack degrades to doctrine alone.

## Learning

- **Lessons are records, not prose.** An early lesson file held twelve
  rephrasings of one idea. Lessons now carry a topic key, confirmation
  counts, a retirement clock and per-hull wording, and the model must
  answer CONFIRMED or produce something genuinely new.
- **A deterministic evaluator reads the fight first.** Range bands, time in
  reach, cloak dividends and when the fight was decided are measured from a
  per-poll track. The model writes lessons with those numbers in front of
  it.
- **The measured record outranks the model's own conclusions.** In a
  brains-vs-brains set the Claude commander wrote "keep range, circle"
  lessons while the record showed that brawling won and manoeuvring lost.
  The counted record now opens every lesson block, win lessons outrank loss
  lessons, and contradicted lessons can be retired.
- **Less micromanagement wins.** LLM commanders that issued one or two
  orders a round and let the stock AI fight beat commanders that flipped
  power and manoeuvres every turn. Hence the power dwell, the repeat-order
  filter, and the brain gate.
- **Hull-specific advice must stay hull-specific.** A range lesson learned
  in one hull ("our guns only reach 125 m") was being given to every ship.
  Range lessons now reach a duel only in the AI hull's own wording, or a
  similar-reach hull's, labelled as such.
- **Measure before you believe a matchup table.** Sparring results were
  biased for a while by spawn geometry (one side spawned facing away) and by
  a point-defence mod defending one side. Change the referee and the old
  numbers stop being comparable.

## Fairness

- When the player cloaks, an engine AI that is already engaged keeps
  tracking them. DUELAI broadcasts a ceasefire to force the AI to rebuild
  its targeting (a rebuilt AI genuinely cannot see a cloaked ship), and the
  model's brief is redacted to last-known data.
- Sparring rounds the player interferes with (weapon hits from the player's
  ship, god mode) are logged but never scored.

## Engine gotchas worth knowing

- Python `open()` is unusable in this game build. The bridge uses the
  engine's own config API; `LoadConfigFile` merges and the mapping has no
  delete, so telemetry uses fixed slot keys and every block is gated on its
  status key.
- The game ignores any order batch whose sequence number it has already
  consumed, and that counter lives in memory for the whole game session. A
  restarted server can re-issue a used number and nothing happens while
  every signal looks healthy. The director now detects "armed, ticking,
  nothing spawned" and re-sends on a fresh number.
- A telemetry key that does not exist reads as -1 silently. One wrong key
  name left the brain told "range -1 m" for weeks.
- Every stance, target or focus order rebuilds the attack AI, and any AI
  rebuild during a cloak ramp aborts the cloak. Cloak transitions also mean
  no shields, so a cloak started inside the enemy's torpedo reach is
  usually fatal.
- Spawned ships get no orientation unless you set one.
- On some hulls the parent impulse-engine subsystem is not the one that
  takes damage; read the child engines. Top speed follows the power split,
  so it is not a damage signal. `RepairSubsystem.AddToRepairList` does queue
  damaged child engines.
- The stock circle manoeuvre can raise "math domain error" on hulls whose
  weapon directions are not unit length (Borg spheres); see the patch in
  `game/patches/`.
- New module-level names in a Python 1.5.2 script need adding to the
  `global` line of any function that resets them, or the reset silently
  creates locals.
- Delete the `.pyc` beside any script you edit, or the old bytecode may
  still load. Never edit game scripts while the game is running.
- Pausing or alt-tabbing freezes telemetry; treat short link losses as a
  pause, not the end of a round.
- Opus 5.x thinking counts against `max_tokens`, so small output caps
  truncate plans; the strategist uses generous caps.
- Two writers saving the same JSON file at once once spliced it into
  something unparseable, and the next battle ran with no lessons at all.
  All data saves are now temp file + `os.replace`.
