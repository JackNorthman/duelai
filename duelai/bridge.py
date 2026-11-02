"""Bridge to the in-game ClaudeCommander file protocol.

The game side (scripts/Custom/Autoload/ClaudeCommander.py, Python 1.5.2)
writes telemetry to claude_state.cfg every tick and consumes order batches
from claude_orders.cfg.  This module is the Python 3 desktop half:
parse the state file, write order batches.

Protocol facts (proven in-engine across many sessions):
- Both files live in the game root; the engine resolves bare names there.
- State keys are pipe-separated ints ("Key|123") or "Key=string" pairs.
- The engine's own option strings (e.g. the captain name) embed
  NUL bytes; read with errors="replace" and ignore non-ClaudeState sections.
- Orders: [ClaudeOrders] Seq|n, Num|n, then Cmd<j>/Arg<j>/Ship<j>/Text<j>
  per order.  Ship -1 broadcasts.  The game consumes a batch only when Seq
  exceeds its last-seen value (AckSeq in the state file).
- LoadConfigFile MERGES and the config mapping has no delete, so stale
  higher-index keys from old batches are harmless (Num gates consumption).
"""

import os
import re
import time

# ---------------------------------------------------------------- constants

# The folder that holds stbc.exe (the game writes claude_state.cfg there).
# Set STBC_GAME_ROOT, or "game_root" in data/config.json (server.py passes
# that in); the fallback is the current directory.
DEFAULT_GAME_ROOT = os.environ.get("STBC_GAME_ROOT", "") or "."

STATE_FILE = "claude_state.cfg"
ORDERS_FILE = "claude_orders.cfg"

MAX_FLEET = 24
MAX_FOES = 8

# Order codes (RunOrder/RunOrderOne in ClaudeCommander.py)
CMD_STANCE = 1
CMD_TARGET = 2
CMD_POWER = 3
CMD_CLOAK = 4
CMD_TAUNT = 5
CMD_END = 6
CMD_BREAK = 7
CMD_CIRCLE = 8
CMD_GUARD = 9
CMD_SPAWN = 10
CMD_FOCUS = 11
CMD_FREE = 12
CMD_AUTOBREAK = 13
CMD_REPAIR = 14
CMD_LAUNCH = 15
CMD_WARP = 18       # warp break (ClaudeCommander 2026-10-24)

STANCES = {"aggressive": 1, "balanced": 2, "defensive": 3, "ambush": 4,
           "cripple": 5,
           # short-reach NonFedAttack hulls: ClaudeCommander builds
           # GunboatAttack, close band from the order text (see stance())
           "gunboat": 6}
TARGETS = {"none": 0, "hull": 1, "shields": 2, "weapons": 3, "torps": 4,
           "engines": 5, "warp": 6, "sensors": 7, "power": 8, "cloak": 9}
POWERS = {"weapons": 1, "shields": 2, "engines": 3, "balanced": 4}

FOCUS_PLAYER = 8      # Arg for FOCUS meaning "the player"
FOCUS_RELEASE = 99
SPAR_TGT_BASE = 100   # a spar-focused ship's Tgt: 100 + the opposing FLEET slot

SLOT_EMPTY, SLOT_ALIVE, SLOT_DEAD = 0, 1, 2

# Cloak telemetry codes - MUST match ClaudeCommander.CloakCode():
#   0 no device, 1 ready (device idle), 2 cloaking, 3 cloaked, 4 decloaking.
# (A "ready" 1 is NOT cloaked - misreading this cost the maiden battle.)
CLOAK_NO_DEVICE, CLOAK_READY, CLOAK_CLOAKING, CLOAK_CLOAKED, CLOAK_DECLOAKING = (
    0, 1, 2, 3, 4)

_INT_RE = re.compile(r"^([A-Za-z0-9]+)\|(-?\d+)$")
_STR_RE = re.compile(r"^([A-Za-z0-9]+)=(.*)$")


# ---------------------------------------------------------------- state read

def read_state(game_root=DEFAULT_GAME_ROOT):
    """Parse [ClaudeState] out of claude_state.cfg into {key: int|str}."""
    path = os.path.join(game_root, STATE_FILE)
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    out = {}
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_section = (line == "[ClaudeState]")
            continue
        if not in_section or not line:
            continue
        m = _INT_RE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
            continue
        m = _STR_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def state_mtime(game_root=DEFAULT_GAME_ROOT):
    try:
        return os.path.getmtime(os.path.join(game_root, STATE_FILE))
    except OSError:
        return 0.0


# Per-slot telemetry keys.  Ints unless listed in _SLOT_STRINGS.
_SLOT_STRINGS = ("Name", "Type")

# "no target" / "out of contact" - never a real distance.
NO_RANGE = 99999


def _slot(state, prefix):
    """Collect one S<n>/F<n> block into a dict; None if slot is empty."""
    st = state.get(prefix + "St", SLOT_EMPTY)
    if st == SLOT_EMPTY:
        return None
    d = {"st": st}
    plen = len(prefix)
    for key, val in state.items():
        if key.startswith(prefix) and key[plen:plen + 1].isupper():
            d[key[plen:]] = val
    return d


def slot_range(slot):
    """Range from a FLEET slot to whatever it is currently shooting at.

    ClaudeCommander writes `S<n>Rng` + `S<n>Closing` on fleet slots and
    `F<n>RngYou` only on foe slots.  A flagship is always a fleet slot, so
    every `RngYou` lookup against it silently read its default: the battle
    track has recorded 99999 for every tick since an early duel, which bucketed a
    343m knife fight as "long (2000+)" and wrote the lesson to match.
    -1 is the game's explicit "no target" and must not read as point-blank.
    """
    rng = (slot or {}).get("Rng", -1)
    return NO_RANGE if rng is None or rng < 0 else rng


def slot_closing(slot, fallback=0):
    """Metres closed since last tick - positive means closing in.  The game
    computes this itself and zeroes it across a target switch, which a
    delta taken on this side cannot know about."""
    if slot is None or "Closing" not in slot:
        return fallback
    return slot.get("Closing", 0)


def spar_target(slot):
    """The fleet slot a spar-focused ship is measuring against, or None.

    Under spar focus ClaudeCommander resolves the nearest ship of the OPPOSING
    FLEET and reports it as Tgt = SPAR_TGT_BASE + slot, and Rng, Closing and
    Brg are then about that ship.  Any other Tgt (a foe slot, 8 = the player,
    99 = nothing) is not a fleet ship."""
    t = (slot or {}).get("Tgt", FOCUS_RELEASE)
    if isinstance(t, int) and SPAR_TGT_BASE <= t < SPAR_TGT_BASE + MAX_FLEET:
        return t - SPAR_TGT_BASE
    return None


class Snapshot:
    """A parsed view of one telemetry tick."""

    def __init__(self, state):
        self.raw = state
        self.tick = state.get("Tick", 0)
        self.ack_seq = state.get("AckSeq", 0)
        self.status = state.get("Status", 0)
        self.last_result = state.get("LastResult", "")
        self.obj_count = state.get("ObjCount", 0)
        self.torp_count = state.get("TorpCount", 0)
        # Player ("You*" keys)
        self.player = {}
        for key, val in state.items():
            if key.startswith("You"):
                self.player[key[3:]] = val
        self.player_alive = state.get("YouAlive", 0)
        # Fleet + foes
        self.fleet = {}
        for i in range(MAX_FLEET):
            s = _slot(state, "S%d" % i)
            if s is not None:
                self.fleet[i] = s
        self.foes = {}
        for i in range(MAX_FOES):
            s = _slot(state, "F%d" % i)
            if s is not None:
                self.foes[i] = s

    def live_fleet(self):
        return {i: s for i, s in self.fleet.items() if s["st"] == SLOT_ALIVE}

    def live_foes(self):
        return {i: s for i, s in self.foes.items() if s["st"] == SLOT_ALIVE}


def snapshot(game_root=DEFAULT_GAME_ROOT):
    return Snapshot(read_state(game_root))


# --------------------------------------------------------------- order write

class Order:
    """One order in a batch."""

    def __init__(self, cmd, arg=0, ship=-1, text=""):
        self.cmd, self.arg, self.ship, self.text = cmd, int(arg), ship, text

    def __repr__(self):
        return "Order(cmd=%d arg=%d ship=%d text=%r)" % (
            self.cmd, self.arg, self.ship, self.text)


# Convenience constructors --------------------------------------------------

def spawn(ship_class, name="", side=0):
    text = ship_class if not name else ship_class + "," + name
    return Order(CMD_SPAWN, side, -1, text)


def stance(name, ship=-1, close=0):
    """close: the gunboat close band in metres.  ClaudeCommander reads it off
    the order text and keeps it on the slot for every later AI rebuild."""
    return Order(CMD_STANCE, STANCES[name], ship,
                 ("%d" % int(close)) if close else "")


def target(sub, ship=-1):
    return Order(CMD_TARGET, TARGETS[sub], ship)


def power(mode, ship=-1):
    return Order(CMD_POWER, POWERS[mode], ship)


def cloak(on, ship=-1):
    return Order(CMD_CLOAK, 1 if on else 0, ship)


def taunt(text, ship=-1):
    return Order(CMD_TAUNT, 0, ship, text)


def brk(seconds, ship=-1):
    return Order(CMD_BREAK, seconds, ship)


def circle(seconds, ship=-1):
    return Order(CMD_CIRCLE, seconds, ship)


def guard(seconds, ship=-1):
    return Order(CMD_GUARD, seconds, ship)


def focus(who, ship=-1):
    """who: foe slot 0-7, 'player', 'spar' (ignore the player), or 'none'."""
    if who == "player":
        arg = FOCUS_PLAYER
    elif who == "spar":
        arg = 9
    elif who in ("none", "release"):
        arg = FOCUS_RELEASE
    else:
        arg = int(who)
    return Order(CMD_FOCUS, arg, ship)


def free(ship=-1):
    return Order(CMD_FREE, 0, ship)


def autobreak(hull=40, face=20, drop=12, secs=12, ship=-1):
    return Order(CMD_AUTOBREAK, 1, ship,
                 "%d,%d,%d,%d" % (hull, face, drop, secs))


def autobreak_off(ship=-1):
    return Order(CMD_AUTOBREAK, 0, ship)


def warp(seconds, ship=-1):
    """Warp break: in-system warp away from the current target, mend for
    `seconds` (10-90; 0 cancels), then the attack AI warps back in."""
    return Order(CMD_WARP, seconds, ship)


def repair(ship=-1):
    return Order(CMD_REPAIR, 0, ship)


def reap_fleet():
    """Remove every live fleet ship (both sides) - spar board clear."""
    return Order(16, 0, -1)


def spawn_range(metres):
    """Order 17: how far ahead of the player RED spawns.  BLUE spawns beside
    the player, so this IS how far apart the two sides start.  Game default is
    5000, at which slow or short-ranged hulls never reach each other inside a
    round.  ClaudeCommander clamps to 150..20000."""
    return Order(17, int(metres), -1)


def launch(count=7, ship=-1):
    return Order(CMD_LAUNCH, count, ship)


def end():
    return Order(CMD_END, 0, -1)


# ---------------------------------------------------------------------------

_CMD_NAMES = {1: "stance", 2: "target", 3: "power", 4: "cloak", 5: "taunt",
              6: "end", 7: "break", 8: "circle", 9: "guard", 10: "spawn",
              11: "focus", 12: "free", 13: "autobreak", 14: "repair",
              15: "launch", 17: "spawn_range", 18: "warp"}
_ARG_NAMES = {1: {v: k for k, v in STANCES.items()},
              2: {v: k for k, v in TARGETS.items()},
              3: {v: k for k, v in POWERS.items()}}


def describe(o):
    """'target hull', 'power weapons', 'break 15' - the order as a human
    reads it, for the battle log."""
    name = _CMD_NAMES.get(o.cmd, "cmd%d" % o.cmd)
    arg = _ARG_NAMES.get(o.cmd, {}).get(o.arg, o.arg)
    out = "%s %s" % (name, arg) if arg not in ("", 0, None) or o.cmd in (1, 2, 3) else name
    if o.text:
        out += " %r" % o.text[:40]
    if o.ship != -1:
        out += " @%d" % o.ship
    return out


class OrderWriter:
    """Writes numbered order batches, tracking Seq against the game's ack.

    Every batch is also kept in `history` - (seq, wall-time, tick, source,
    [orders as text]) - so a battle log can say what was actually sent and
    by which layer.  Three "in range, not firing" fights were diagnosed
    from hull curves and a battery reading because the orders themselves
    were nowhere on the record."""

    def __init__(self, game_root=DEFAULT_GAME_ROOT):
        self.game_root = game_root
        self.path = os.path.join(game_root, ORDERS_FILE)
        self.seq = self._initial_seq()
        self.history = []
        self.mark = self.seq

    def batches_since(self, seq):
        return [b for b in self.history if b[0] > seq]

    def _initial_seq(self):
        st = read_state(self.game_root)
        return st.get("AckSeq", 0)

    def resync(self):
        """Re-read the game's AckSeq (call after link reset/re-arm)."""
        st = read_state(self.game_root)
        ack = st.get("AckSeq", 0)
        if ack > self.seq:
            self.seq = ack
        return self.seq

    def send(self, orders, source="", tick=0):
        """Write one batch.  Returns the Seq used."""
        if not orders:
            return self.seq
        self.seq += 1
        import time as _t
        self.history.append((self.seq, int(_t.time()), int(tick), source,
                             [describe(o) for o in orders]))
        if len(self.history) > 3000:
            del self.history[:-3000]
        lines = ["[ClaudeOrders]", "Seq|%d" % self.seq, "Num|%d" % len(orders)]
        for j, o in enumerate(orders):
            lines.append("Cmd%d|%d" % (j, o.cmd))
            lines.append("Arg%d|%d" % (j, o.arg))
            lines.append("Ship%d|%d" % (j, o.ship))
            if o.text:
                lines.append("Text%d=%s" % (j, _clean(o.text)))
        with open(self.path, "w", encoding="ascii", errors="replace",
                  newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        return self.seq

    def acked(self, snap=None):
        """Has the game consumed our latest batch?"""
        if snap is None:
            snap = Snapshot(read_state(self.game_root))
        return snap.ack_seq >= self.seq


def _clean(text):
    """Order text rides in a cfg line: strip newlines, pipes, brackets."""
    text = text.replace("\n", " ").replace("\r", " ")
    for ch in "|[]=":
        text = text.replace(ch, " ")
    text = " ".join(text.split())
    return text[:200].encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------- link probe

class LinkProbe:
    """Detects whether the in-game link is armed and ticking."""

    def __init__(self, game_root=DEFAULT_GAME_ROOT):
        self.game_root = game_root
        self.last_tick = -1
        self.last_change = 0.0

    def alive(self, snap=None):
        """True if telemetry advanced within the last ~6 seconds."""
        if snap is None:
            snap = Snapshot(read_state(self.game_root))
        now = time.time()
        if snap.tick != self.last_tick:
            self.last_tick = snap.tick
            self.last_change = now
        return (now - self.last_change) < 6.0 and snap.tick > 0
