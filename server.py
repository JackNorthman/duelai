"""DUELAI web server - stdlib only.

Serves the LCARS UI and a small JSON API over localhost:8790.

  GET  /                    ui/index.html
  GET  /lcars/...           design bundle static files
  GET  /api/state?since=N   phase + chat messages with i > N
  GET  /api/ships           ship picker data
  GET  /api/battles         archive index of logs/battle_*.json
  GET  /api/battle?f=NAME   one battle log + its learning record
  GET  /api/cloaklearn      cloak learner: plays banked per hull (sparring)
  POST /api/plan            {difficulty, ship_class, ship_name, persona,
                             player_ship}
  POST /api/chat            {text}
  POST /api/end             stand down / clear plan
  POST /api/rearm           debrief -> idle for a rematch
  POST /api/launch          start stbc.exe -TestMode (no-op if running)
"""

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from duelai import bridge, doctrine, learning    # noqa: E402
from duelai.director import Director, PERSONAS   # noqa: E402

PORT = int(os.environ.get("DUELAI_PORT", "8790"))

MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css", ".js": "text/javascript",
        ".json": "application/json", ".mp3": "audio/mpeg",
        ".woff": "font/woff", ".woff2": "font/woff2",
        ".png": "image/png", ".svg": "image/svg+xml"}

def _config():
    try:
        with open(os.path.join(ROOT, "data", "config.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _api_key(cfg):
    """ANTHROPIC_API_KEY from the environment wins; else data/config.json
    (gitignored).  Empty = offline only, no strategist."""
    return (os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or cfg.get("api_key", ""))


def _game_root(cfg):
    return (os.environ.get("STBC_GAME_ROOT", "").strip()
            or cfg.get("game_root", "") or bridge.DEFAULT_GAME_ROOT)


director = Director(game_root=_game_root(_config()),
                    model=_config().get("model"),
                    banter=_config().get("banter", True))


def _apply_brains(cfg):
    director.configure_strategist(_api_key(cfg),
                                  cfg.get("api_model", "claude-opus-5-5"),
                                  cfg.get("strategist", True),
                                  cfg.get("strategist_effort", "high"))
    director.configure_online(cfg.get("brain_mode", "offline"),
                              _api_key(cfg),
                              cfg.get("online_model", "claude-sonnet-5-5"),
                              cfg.get("online_effort", "low"))


_apply_brains(_config())


def _save_config(updates):
    cfg = _config()
    cfg.update(updates)
    with open(os.path.join(ROOT, "data", "config.json"), "w",
              encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def settings_view():
    cfg = _config()
    key = _api_key(cfg)
    return {
        "api_key_set": bool(key),
        "api_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "api_key_hint": ("..." + key[-4:]) if len(key) > 8 else "",
        "api_model": cfg.get("api_model", "claude-opus-5-5"),
        "brain_mode": cfg.get("brain_mode", "offline"),
        "online_model": cfg.get("online_model", "claude-sonnet-5-5"),
        "online_effort": cfg.get("online_effort", "low"),
        "brain_online_ok": (director.brain.online.ok
                            if director.brain.online is not None else None),
        "brain_online_error": (director.brain.online.last_error
                               if director.brain.online is not None else ""),
        "strategist": bool(cfg.get("strategist", True)),
        "banter": bool(cfg.get("banter", True)),
        "model": cfg.get("model", ""),
        "strategist_live": director.strategist is not None,
    }

_ships_cache = None

# ---- speech to text (optional; degrades to typed chat if not installed)
_whisper = None
_whisper_lock = threading.Lock()


def whisper_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


STT_MODEL = "small.en"          # noticeably better than base.en; still
                                # ~real-time for short utterances on CPU
STT_PROMPT = ("Star Trek space battle radio chatter: cloak, decloak, "
              "cloaking device, shields, hull, torpedoes, phasers, "
              "sensors, warp core, Romulan, Klingon, Scimitar, Warbird, "
              "USS Draco, Surak, surrender, coward, fire at will, "
              "I'll never surrender.")


def transcribe(audio_bytes):
    global _whisper
    import tempfile
    from faster_whisper import WhisperModel
    with _whisper_lock:
        if _whisper is None:
            _whisper = WhisperModel(STT_MODEL, device="cpu",
                                    compute_type="int8")
        tmp = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
        try:
            tmp.write(audio_bytes)
            tmp.close()
            segments, _info = _whisper.transcribe(
                tmp.name, beam_size=5, vad_filter=True,
                initial_prompt=STT_PROMPT)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            os.unlink(tmp.name)


# ---- battle archive
LOG_RE = re.compile(r"^battle_(\d+)_(\d+)\.json$")


def _learning_raw():
    try:
        with open(os.path.join(ROOT, "data", "learning.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_log(name):
    try:
        with open(os.path.join(ROOT, "logs", name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def battle_index():
    """Every battle log joined with its learning record (matched on t)."""
    recs = {}
    for b in _learning_raw().get("battles", []):
        try:
            recs[int(b.get("t", 0))] = b
        except (TypeError, ValueError):
            pass
    try:
        names = os.listdir(os.path.join(ROOT, "logs"))
    except OSError:
        names = []
    out = []
    for name in names:
        m = LOG_RE.match(name)
        if not m:
            continue
        data = _read_log(name)
        if not data:
            continue
        db = data.get("debrief") or {}
        t = int(m.group(1))
        result = db.get("result", "?")
        rec = recs.get(t)
        won = rec.get("won") if rec else (
            True if result.startswith("victory") else
            False if result.startswith("defeat") else None)
        out.append({
            "file": name, "t": t, "no": int(m.group(2)),
            "result": result,
            "ship": db.get("ship", ""),
            "difficulty": db.get("difficulty", ""),
            "duration_ticks": db.get("duration_ticks", 0),
            "won": won,
            "min_hull": rec.get("player_min_hull") if rec else None,
        })
    out.sort(key=lambda e: e["t"], reverse=True)
    return out


# ---- game launcher
# -TestMode turns on the game's developer/test mode (god mode and debug
# keys); the exe needs its own folder as cwd.
GAME_ARGS = ["-TestMode"]


def game_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq stbc.exe", "/NH"],
            capture_output=True, text=True, timeout=5).stdout
        return "stbc.exe" in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def launch_game():
    """Start the game detached. Returns (ok, already, error)."""
    exe = os.path.join(director.game_root, "stbc.exe")
    if not os.path.isfile(exe):
        return False, False, "stbc.exe not found at %s" % exe
    if game_running():
        return True, True, ""
    try:
        subprocess.Popen([exe] + GAME_ARGS, cwd=director.game_root,
                         close_fds=True)
        return True, False, ""
    except OSError as e:
        return False, False, repr(e)[:200]


def ship_list():
    """Curated picker list from ships.json: real warships, grouped."""
    global _ships_cache
    if _ships_cache is not None:
        return _ships_cache
    path = os.path.join(ROOT, "data", "ships.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        data = {}
    out = []
    for name, d in sorted(data.items()):
        hull = d.get("hull", 0)
        if hull < 3000:            # skip fighters/props for the picker
            continue
        out.append({
            "id": name,
            "label": d.get("label", name),
            "hull": int(hull),
            "cloak": bool(d.get("cloak")),
            "speed": round(d.get("max_speed", 0), 2),
            "phasers": len(d.get("phasers", [])),
            "class": d.get("hclass", ""),
        })
    _ships_cache = out
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --------------------------------------------------------------- helpers

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, relpath, under="ui"):
        # Static files only from ui/ or lcars/ - never data/ (config.json
        # can hold the API key) or logs/.
        base = os.path.join(ROOT, under)
        path = os.path.normpath(os.path.join(ROOT, relpath))
        if (not path.startswith(base + os.sep)
                or not os.path.isfile(path)):
            self.send_error(404)
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._file("ui/index.html")
        elif u.path.startswith("/lcars/"):
            self._file(u.path.lstrip("/"), under="lcars")
        elif u.path == "/api/state":
            q = parse_qs(u.query)
            since = int(q.get("since", ["0"])[0])
            st = director.public_state()
            st["chat"] = director.chat_since(since)
            st["difficulties"] = [
                {"id": d, "blurb": doctrine.PROFILES[d]["blurb"]}
                for d in doctrine.DIFFICULTY_ORDER]
            st["personas"] = sorted(PERSONAS.keys())
            self._json(st)
        elif u.path == "/api/ships":
            self._json(ship_list())
        elif u.path == "/api/races":
            # Every race that can field a side, straight from the database,
            # so the sparring picker never drifts from the game's own ship
            # menu again.  "auto" flags whether the explorer may draft the
            # race unprompted; excluded races stay pickable by hand.
            from duelai import intel, sparring
            auto = set(sparring.auto_races())
            self._json([{"id": r, "ships": n, "pool": p, "auto": (r in auto)}
                        for r, n, p in intel.all_races()])
        elif u.path == "/api/battles":
            self._json(battle_index())
        elif u.path == "/api/battle":
            q = parse_qs(u.query)
            name = (q.get("f") or [""])[0]
            m = LOG_RE.match(name)
            if not m:
                self._json({"ok": False, "error": "bad name"}, 400)
                return
            data = _read_log(name)
            if not data:
                self._json({"ok": False, "error": "not found"}, 404)
                return
            t = int(m.group(1))
            rec = None
            for b in _learning_raw().get("battles", []):
                if int(b.get("t", 0)) == t:
                    rec = b
                    break
            self._json({"ok": True,
                        "debrief": data.get("debrief"),
                        "chat": data.get("chat", []),
                        "record": rec})
        elif u.path == "/api/settings":
            self._json(settings_view())
        elif u.path == "/api/sparring":
            from duelai import sparring
            self._json(sparring.table())
        elif u.path == "/api/cloaklearn":
            from duelai import cloaklearn
            self._json(cloaklearn.table())
        elif u.path == "/api/brains":
            from duelai import sparbrains
            self._json(sparbrains.table())
        elif u.path == "/api/learning":
            self._json(learning.stats())
        elif u.path == "/api/stt/status":
            self._json({"available": whisper_available()})
        else:
            self.send_error(404)

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/plan":
            b = self._body()
            plan = director.stage_plan(
                difficulty=str(b.get("difficulty", "officer")),
                ship_class=str(b.get("ship_class", ""))[:40],
                ship_name=str(b.get("ship_name", ""))[:40],
                persona=str(b.get("persona", ""))[:20],
                spawn=int(b.get("spawn", 0) or 0),
                era=str(b.get("era", ""))[:8],
                player_ship=str(b.get("player_ship", ""))[:40],
                player_name=str(b.get("player_name", ""))[:60],
                player_side=str(b.get("player_side", ""))[:40],
                player_allies=str(b.get("player_allies", ""))[:300],
                scenario=str(b.get("scenario", ""))[:500])
            self._json({"ok": True, "plan": plan})
        elif u.path == "/api/settings":
            b = self._body()
            updates = {}
            if isinstance(b.get("api_key"), str) and b["api_key"].strip():
                updates["api_key"] = b["api_key"].strip()[:200]
            if b.get("api_key_clear"):
                updates["api_key"] = ""
            if isinstance(b.get("api_model"), str) and b["api_model"].strip():
                updates["api_model"] = b["api_model"].strip()[:60]
            if "strategist" in b:
                updates["strategist"] = bool(b["strategist"])
            if "banter" in b:
                updates["banter"] = bool(b["banter"])
            if b.get("brain_mode") in ("online", "offline"):
                updates["brain_mode"] = b["brain_mode"]
            if isinstance(b.get("online_model"), str) and \
                    b["online_model"].strip():
                updates["online_model"] = b["online_model"].strip()[:60]
            if b.get("online_effort") in ("low", "medium", "high"):
                updates["online_effort"] = b["online_effort"]
            cfg = _save_config(updates)
            director.banter = 1 if cfg.get("banter", True) else 0
            _apply_brains(cfg)
            self._json({"ok": True, "settings": settings_view()})
        elif u.path == "/api/chat":
            b = self._body()
            director.user_says(str(b.get("text", "")))
            self._json({"ok": True})
        elif u.path == "/api/stt":
            if not whisper_available():
                self._json({"ok": False, "error": "stt not installed"}, 501)
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0 or n > 20 * 1024 * 1024:
                    self._json({"ok": False, "error": "bad audio size"}, 400)
                    return
                text = transcribe(self.rfile.read(n))
            except Exception as e:
                self._json({"ok": False, "error": repr(e)[:200]}, 500)
                return
            if text:
                director.user_says(text)
            self._json({"ok": True, "text": text})
        elif u.path == "/api/launch":
            ok, already, err = launch_game()
            body = {"ok": ok, "already": already}
            if err:
                body["error"] = err
            self._json(body, 200 if ok else 500)
        elif u.path == "/api/end":
            director.stand_down()
            self._json({"ok": True})
        elif u.path == "/api/rearm":
            director.rearm()
            self._json({"ok": True})
        else:
            self.send_error(404)


def main():
    director.start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("DUELAI up at http://localhost:%d  (game root: %s)"
          % (PORT, director.game_root))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        director.stop()


if __name__ == "__main__":
    main()
