"""One-time Memory Alpha lore indexer - stdlib only.

Streams a MediaWiki XML dump (pages, current versions) into a SQLite
FTS5 database at data/lore.db for the stage-time lore briefs.

  python build_lore.py <dump.xml>          build the index
  python build_lore.py --query "martok"    sanity-check a search

Content is CC-BY-NC from Memory Alpha (memory-alpha.fandom.com);
personal offline use, credit Memory Alpha in any user-facing output.
"""

import html
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "lore.db")

CHUNK_CHARS = 1400          # target chunk size
MAX_CHUNKS = 8              # per article - covers lead + episode summaries
MIN_ARTICLE = 200           # skip stubs/disambigs shorter than this

# ------------------------------------------------------------- wikitext

_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
# real-world/production sections poison an in-character briefing - cut
# each article at the first of these headings before cleaning
_RE_STOP_SECTION = re.compile(
    r"^=+ *(appendices|background information|memorable quotes|"
    r"external links?|apocrypha|see also|related topics?|"
    r"links and references)\b", re.I | re.M)
# unwrap the ship-name templates instead of deleting them wholesale
_RE_T_USS = re.compile(r"\{\{[Uu]SS\|([^{}|]+)(?:\|[^{}]*)?\}\}")
_RE_T_CLASS = re.compile(r"\{\{[Cc]lass\|([^{}|]+)(?:\|[^{}]*)?\}\}")
_RE_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}", re.S)
_RE_TABLE = re.compile(r"\{\|[^{}]*?\|\}", re.S)
_RE_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_RE_GALLERY = re.compile(r"<gallery[^>]*>.*?</gallery>", re.S | re.I)
_RE_FILELINK = re.compile(r"\[\[(?:file|image|media):[^\[\]]*\]\]", re.I)
_RE_LINK_PIPE = re.compile(r"\[\[[^\[\]|]*\|([^\[\]]*)\]\]")
_RE_LINK = re.compile(r"\[\[([^\[\]]*)\]\]")
_RE_EXTLINK = re.compile(r"\[https?://\S*( [^\]]*)?\]")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_HEADING = re.compile(r"^=+ *(.*?) *=+ *$", re.M)
_RE_MAGIC = re.compile(r"__[A-Z_]+__")
_RE_QUOTES = re.compile(r"'{2,}")
_RE_BULLET = re.compile(r"^[*#:;]+ *", re.M)
_RE_BLANK = re.compile(r"\n{3,}")
_RE_SPACES = re.compile(r"[ \t]{2,}")


def clean_wikitext(text):
    m = _RE_STOP_SECTION.search(text)
    if m:
        text = text[:m.start()]
    text = _RE_COMMENT.sub("", text)
    text = _RE_T_USS.sub(r"USS \1", text)
    text = _RE_T_CLASS.sub(r"\1-class", text)
    for pattern in (_RE_TEMPLATE, _RE_TABLE, _RE_FILELINK):
        for _ in range(24):                 # peel nested layers, innermost first
            text, n = pattern.subn("", text)
            if not n:
                break
    text = _RE_REF.sub("", text)
    text = _RE_GALLERY.sub("", text)
    for _ in range(8):                      # [[a|[[b]] c]] style nesting
        text, n1 = _RE_LINK_PIPE.subn(r"\1", text)
        text, n2 = _RE_LINK.subn(r"\1", text)
        if not (n1 or n2):
            break
    text = _RE_EXTLINK.sub(lambda m: (m.group(1) or "").strip(), text)
    text = _RE_HEADING.sub(r"\1.", text)
    text = _RE_TAG.sub(" ", text)
    text = _RE_MAGIC.sub("", text)
    text = _RE_QUOTES.sub("", text)
    text = html.unescape(text)
    text = _RE_BULLET.sub("", text)
    text = _RE_SPACES.sub(" ", text)
    text = _RE_BLANK.sub("\n\n", text)
    return text.strip()


def chunk(text):
    """Pack paragraphs into ~CHUNK_CHARS pieces, MAX_CHUNKS per article."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) > CHUNK_CHARS:
            out.append(cur)
            if len(out) >= MAX_CHUNKS:
                return out
            cur = para
        else:
            cur = (cur + "\n" + para) if cur else para
    if cur and len(out) < MAX_CHUNKS:
        out.append(cur)
    return out


# ------------------------------------------------------------- dump walk

def iter_articles(path):
    """Yield (title, wikitext) for main-namespace, non-redirect pages."""
    ns_uri = None
    title = text = None
    page_ns = redirect = None
    for event, elem in ET.iterparse(path, events=("start", "end")):
        tag = elem.tag.rsplit("}", 1)[-1]
        if event == "start":
            if tag == "page":
                title = text = page_ns = None
                redirect = False
            continue
        if tag == "title":
            title = elem.text or ""
        elif tag == "ns":
            page_ns = elem.text
        elif tag == "redirect":
            redirect = True
        elif tag == "text":
            text = elem.text or ""
        elif tag == "page":
            if page_ns == "0" and not redirect and title and text \
                    and not text.lstrip()[:9].upper().startswith("#REDIRECT"):
                yield title, text
            elem.clear()


def build(dump_path):
    if not os.path.isfile(dump_path):
        sys.exit("dump not found: %s" % dump_path)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE VIRTUAL TABLE lore USING fts5("
               "title, body, tokenize='porter unicode61')")
    t0 = time.time()
    pages = chunks = 0
    batch = []
    for title, wikitext in iter_articles(dump_path):
        if "(disambiguation)" in title.lower():
            continue
        cleaned = clean_wikitext(wikitext)
        if len(cleaned) < MIN_ARTICLE:
            continue
        for piece in chunk(cleaned):
            batch.append((title, piece))
            chunks += 1
        pages += 1
        if len(batch) >= 2000:
            db.executemany("INSERT INTO lore(title, body) VALUES (?, ?)",
                           batch)
            db.commit()
            batch = []
        if pages % 5000 == 0:
            print("  %d articles, %d chunks, %.0fs"
                  % (pages, chunks, time.time() - t0), flush=True)
    if batch:
        db.executemany("INSERT INTO lore(title, body) VALUES (?, ?)", batch)
    db.commit()
    db.execute("INSERT INTO lore(lore) VALUES ('optimize')")
    db.commit()
    db.close()
    size = os.path.getsize(DB_PATH) / (1024 * 1024)
    print("done: %d articles, %d chunks, %.1f MB, %.0fs"
          % (pages, chunks, size, time.time() - t0))


# ------------------------------------------------------------- query test

def query(text):
    from duelai import lore
    print("query:", text)
    for title, body in lore.search(text, limit=5):
        print("\n### %s\n%s" % (title, body[:300]))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--query":
        query(" ".join(sys.argv[2:]))
    elif len(sys.argv) == 2:
        build(sys.argv[1])
    else:
        sys.exit(__doc__)
