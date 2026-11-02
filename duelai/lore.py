"""Offline Memory Alpha lore lookup for battle briefs.

Reads data/lore.db (built by build_lore.py). Degrades to nothing if the
database is absent - staging and battles work exactly as before.

Content credit: Memory Alpha (memory-alpha.fandom.com), CC-BY-NC.
"""

import os
import re
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "lore.db")

BRIEF_CHARS = 1400          # total budget injected into the system prompt
PER_HIT_CHARS = 440         # per matched chunk
MAX_HITS = 3
MAX_PER_TITLE = 2

_STOP = frozenset("""a an and are as at be but by for from has have i in is
it its no not of on or s so that the their them they this to was were will
with you your me my our we he she his her him vs versus against""".split())

# chunks about the production, not the universe, would leak studio talk
# into an in-character briefing - skip them at read time
_RE_PRODUCTION = re.compile(
    r"studio model|shooting model|the producers|visual effects|"
    r"behind the scenes|screenplay|stock footage|reused footage|"
    r"was filmed|the episode's|writers?' room|okudagram|call sheet",
    re.I)


def available():
    return os.path.isfile(DB_PATH)


def _connect():
    return sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"),
                           uri=True)


def _tokens(text):
    seen, out = set(), []
    for tok in re.findall(r"[a-z0-9']+", (text or "").lower()):
        tok = tok.strip("'")
        if len(tok) < 2 or tok in _STOP or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out[:14]


def search(text, limit=6):
    """(title, body) rows, best first. Phrase pass, then keyword pass."""
    if not available():
        return []
    toks = _tokens(text)
    if not toks:
        return []
    rows, seen = [], set()
    with _connect() as db:
        sql = ("SELECT title, body FROM lore WHERE lore MATCH ? "
               "ORDER BY bm25(lore, 8.0, 1.0) LIMIT ?")
        phrase = '"%s"' % " ".join(toks)
        keywords = " OR ".join('"%s"' % t for t in toks)
        for match in (phrase, keywords):
            try:
                got = db.execute(sql, (match, limit)).fetchall()
            except sqlite3.OperationalError:
                continue
            for title, body in got:
                key = (title, body[:80])
                if key in seen:
                    continue
                seen.add(key)
                rows.append((title, body))
                if len(rows) >= limit:
                    return rows
    return rows


def _plan_query(plan):
    p = plan or {}
    return " ".join(x for x in (
        p.get("scenario", ""), p.get("ship_class", ""),
        p.get("ship_name", ""), p.get("player_ship", ""),
        p.get("player_side", ""), p.get("persona", "")) if x)


def matches(plan, limit=MAX_HITS):
    """Distinct matched article titles, for the staging chat line."""
    per_title, titles = {}, []
    for title, body in search(_plan_query(plan), limit=limit * 3):
        if _RE_PRODUCTION.search(body):
            continue
        if per_title.setdefault(title, 0) == 0:
            titles.append(title)
        per_title[title] += 1
        if len(titles) >= limit:
            break
    return titles


def brief(plan):
    """System-prompt block with the most relevant lore chunks."""
    hits = search(_plan_query(plan), limit=MAX_HITS * 3)
    if not hits:
        return ""
    lines, used, per_title = [], 0, {}
    for title, body in hits:
        if _RE_PRODUCTION.search(body):
            continue
        if per_title.get(title, 0) >= MAX_PER_TITLE:
            continue
        piece = body.strip().replace("\n", " ")[:PER_HIT_CHARS]
        line = "- %s: %s" % (title, piece)
        if used + len(line) > BRIEF_CHARS or len(lines) >= MAX_HITS:
            break
        per_title[title] = per_title.get(title, 0) + 1
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return ("LORE ARCHIVE (offline memory alpha excerpts):\n"
            + "\n".join(lines) +
            "\nUse these facts for flavour, names and references. The "
            "scenario and live telemetry always take precedence.")
