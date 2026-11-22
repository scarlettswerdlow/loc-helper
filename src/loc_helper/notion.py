"""Push transcribed documents (docs/*.txt) to a Notion database.

Each document becomes one page: `Description` (the title property) and `Date` are filled in,
and the page body holds a short metadata callout followed by the transcription, reflowed to
paragraphs. Other properties (checkboxes etc.) are never written, so edits made in Notion survive
a re-run. Page IDs are remembered in <workdir>/notion.json so re-runs update pages in place.
"""
from __future__ import annotations

import calendar
import glob
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import output

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"  # the API version that introduced data sources
TOKEN_ENV = "NOTION_API_KEY"
STATE_FILE = "notion.json"
MAX_TEXT = 2000    # characters per rich-text object
MAX_BLOCKS = 100   # blocks per request


class NotionError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def require_token() -> str:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise SystemExit(f"{TOKEN_ENV} is not set. Create an internal integration at "
                         "https://www.notion.so/profile/integrations, share your database with it "
                         f"(••• menu > Connections), and export {TOKEN_ENV}=<its token>.")
    return token


_UUID_RE = re.compile(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                      re.I)


def parse_database_id(s: str) -> str:
    """Accept a Notion database link (view ?v= ignored) or a bare ID; return the 32-hex ID."""
    s = (s or "").strip()
    path = urllib.parse.urlparse(s).path if "/" in s else s
    last = path.rstrip("/").rsplit("/", 1)[-1]
    m = _UUID_RE.search(last)
    if not m:
        raise ValueError(f"can't find a Notion database ID in {s!r}")
    return m.group(1).replace("-", "").lower()


# --- HTTP -----------------------------------------------------------------------------------

class Client:
    """Minimal Notion REST client. Tests swap in a fake with the same `request` method."""

    def __init__(self, token: str, pause: float = 0.35):
        self.token = token
        self.pause = pause  # Notion allows ~3 requests/second on average

    def request(self, method: str, path: str, body: dict | None = None, tries: int = 6) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": VERSION,
                   "Content-Type": "application/json", "User-Agent": "loc-helper"}
        delay = 2.0
        for attempt in range(1, tries + 1):
            if self.pause:
                time.sleep(self.pause)
            try:
                req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                try:
                    detail = json.loads(detail).get("message", detail)
                except ValueError:
                    pass
                if e.code in (429, 500, 502, 503, 504) and attempt < tries:
                    wait = float(e.headers.get("Retry-After") or delay)
                    print(f"  retry {attempt}/{tries - 1} (Notion {e.code}) in {wait:g}s",
                          file=sys.stderr)
                    time.sleep(wait)
                    delay = min(delay * 2, 30)
                    continue
                raise NotionError(f"Notion {e.code} on {method} {path}: {detail}", e.code)
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException, OSError) as e:
                if attempt == tries:
                    raise NotionError(f"giving up on {method} {path}: {e}")
                print(f"  retry {attempt}/{tries - 1} ({e}) in {delay:g}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise NotionError(f"giving up on {method} {path}")


def resolve_data_source(client, database_id: str) -> tuple[str, str, str]:
    """-> (data_source_id, title_property, date_property); checks the schema fits."""
    db = client.request("GET", f"/databases/{database_id}")
    sources = db.get("data_sources") or []
    if not sources:
        raise SystemExit(f"Notion database {database_id} has no data sources visible to the "
                         "integration. Share the database with it (••• menu > Connections).")
    if len(sources) > 1:
        names = ", ".join(f"{s.get('name')!r}" for s in sources)
        raise SystemExit(f"This database has several data sources ({names}); "
                         "loc supports databases with one.")
    ds_id = sources[0]["id"]
    props = client.request("GET", f"/data_sources/{ds_id}").get("properties", {})
    title = next((n for n, p in props.items() if p.get("type") == "title"), None)
    if props.get("Date", {}).get("type") == "date":
        date = "Date"
    else:
        dates = [n for n, p in props.items() if p.get("type") == "date"]
        date = dates[0] if len(dates) == 1 else None
    if not title or not date:
        have = ", ".join(n + " (" + str(p.get("type")) + ")" for n, p in props.items())
        raise SystemExit("The database needs a title property (Description) and a date property "
                         f"(Date); it has: {have}")
    return ds_id, title, date


# --- dates ----------------------------------------------------------------------------------

def _bound(part: str, latest: bool) -> str | None:
    """Earliest (or latest) calendar day an EDTF date part could mean, as YYYY-MM-DD."""
    part = part.rstrip("?~%")
    if not part or part == "..":
        return None
    bits = part.split("-")
    year, month, day = (bits + [None, None])[:3]
    if year == "XXXX":
        return None
    try:
        y = int(year.replace("X", "9" if latest else "0"))
        m = int(month) if month not in (None, "XX") else (12 if latest else 1)
        last = calendar.monthrange(y, m)[1]
        d = int(day) if day not in (None, "XX") else (last if latest else 1)
        if not 1 <= d <= last:
            return None
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, calendar.IllegalMonthError):
        return None


def edtf_to_notion(date: str) -> tuple[str, str | None] | None:
    """EDTF -> (start, end) ISO dates for a Notion date property, or None if there's no basis.

    A single partial date (1862, 1862-09) becomes its first day; qualifiers (? ~ %) are dropped,
    since Notion can't express them (the callout on the page keeps the original EDTF).
    """
    if not output.valid_edtf(date):
        return None
    if "/" in date:
        a, b = date.split("/")
        start, end = _bound(a, False), _bound(b, True)
        if start is None:
            start, end = end, None  # open start: only the end is known
        if start is None:
            return None
        return (start, end if end and end != start else None)
    start = _bound(date, False)
    return (start, None) if start else None


# --- body -----------------------------------------------------------------------------------

_LABEL_RE = re.compile(
    r"^\[(?:image \d+|envelope|docket|enclosure|letterhead|printed|stamp|seal|blank|margin:.*)\]$",
    re.I)
_TERMINAL = ('.', '!', '?', ':', ';', ',', '"', "'", ')', ']', '”', '’')
_SENTENCE_END = ('.', '!', '?')


def reflow(body: str) -> list[str]:
    """Join diplomatic line breaks into paragraphs.

    Blank lines and label lines ([image N], [envelope], ...) always separate paragraphs. Between
    ordinary lines, a paragraph ends when the next line is indented, when a short line ends in
    punctuation, when two short lines meet (address, dateline and signature blocks), or when a
    short line follows a sentence end (a closing such as "Very respectfully"). "Short" means
    under 60% of the document's typical (90th percentile) line length. A hyphen at a line end
    is kept and the halves are joined without a space.
    """
    raw = [ln.rstrip() for ln in body.strip("\n").splitlines()]
    lengths = sorted(len(ln.strip()) for ln in raw
                     if ln.strip() and not _LABEL_RE.match(ln.strip()))
    width = lengths[int(len(lengths) * 0.9)] if len(lengths) >= 4 else 0
    short = width * 0.6

    paragraphs: list[str] = []
    cur: list[str] = []

    def flush():
        if cur:
            paragraphs.append(_join(cur))
            cur.clear()

    for ln in raw:
        text = ln.strip()
        if not text:
            flush()
        elif _LABEL_RE.match(text):
            flush()
            paragraphs.append(text)
        else:
            if cur:
                prev = cur[-1]
                indented = ln[:1].isspace() and len(ln) - len(ln.lstrip()) >= 2
                prev_short = width and len(prev) < short
                if (indented
                        or (prev_short and prev.endswith(_TERMINAL))
                        or (prev_short and len(text) < short)
                        or (width and len(text) < short and prev.endswith(_SENTENCE_END))):
                    flush()
            cur.append(text)
    flush()
    return paragraphs


def _join(lines: list[str]) -> str:
    out = lines[0]
    for ln in lines[1:]:
        out += ln if out.endswith("-") and ln[:1].islower() else " " + ln
    return out


def _rich_text(text: str, link: str | None = None) -> list[dict]:
    """Rich-text objects of at most MAX_TEXT characters, split on whitespace where possible."""
    parts = []
    while text:
        if len(text) <= MAX_TEXT:
            chunk, text = text, ""
        else:
            cut = text.rfind(" ", 0, MAX_TEXT)
            cut = cut if cut > MAX_TEXT // 2 else MAX_TEXT
            chunk, text = text[:cut], text[cut:].lstrip(" ")
        item = {"type": "text", "text": {"content": chunk}}
        if link:
            item["text"]["link"] = {"url": link}
        parts.append(item)
    return parts


def _callout(meta: dict) -> dict:
    segs: list[tuple[str, str | None]] = []

    def line(label: str, value: str, link: str | None = None):
        if segs:
            segs.append(("\n", None))
        segs.append((label, None))
        segs.append((value, link))

    line("Date: ", f"{meta.get('date', '')} ({meta.get('date_basis', '')})")
    if meta.get("item"):
        line("LOC item: ", meta["item"], meta["item"])
    images, urls = meta.get("images") or [], meta.get("loc_urls") or []
    for i, url in enumerate(urls):
        label = f"image {images[i]}" if len(urls) == len(images) else url
        line("LOC image: " if i == 0 else "           ", label, url)
    if meta.get("quality_notes"):
        line("Quality notes: ", str(meta["quality_notes"]))
    if meta.get("model"):
        line("Model: ", str(meta["model"]))
    rich = [r for text, link in segs for r in _rich_text(text, link)]
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich, "icon": {"type": "emoji", "emoji": "📜"}}}


def build_blocks(meta: dict, body: str) -> list[dict]:
    blocks = [_callout(meta)]
    for para in reflow(body):
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": _rich_text(para)}})
    return blocks


def build_properties(meta: dict, title_prop: str, date_prop: str) -> dict:
    props = {title_prop: {"title": _rich_text(str(meta.get("description", "")).strip() or "Untitled")}}
    span = edtf_to_notion(str(meta.get("date", "")))
    if span:
        props[date_prop] = {"date": {"start": span[0], **({"end": span[1]} if span[1] else {})}}
    return props


# --- push -----------------------------------------------------------------------------------

def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _append(client, page_id: str, blocks: list[dict]) -> None:
    for chunk in _chunks(blocks, MAX_BLOCKS):
        client.request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})


def _clear_body(client, page_id: str) -> None:
    while True:
        res = client.request("GET", f"/blocks/{page_id}/children?page_size={MAX_BLOCKS}")
        ids = [b["id"] for b in res.get("results", [])]
        if not ids:
            return
        for bid in ids:
            client.request("DELETE", f"/blocks/{bid}")


def _create(client, ds_id: str, props: dict, blocks: list[dict]) -> dict:
    first, rest = blocks[:MAX_BLOCKS], blocks[MAX_BLOCKS:]
    page = client.request("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": props, "children": first})
    _append(client, page["id"], rest)
    return page


def _update(client, page_id: str, props: dict, blocks: list[dict]) -> None:
    page = client.request("PATCH", f"/pages/{page_id}", {"properties": props})
    if page.get("in_trash") or page.get("archived"):
        raise NotionError("page is in the trash", 404)
    _clear_body(client, page_id)
    _append(client, page_id, blocks)


def _load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_state(path: str, state: dict) -> None:
    with open(path + ".part", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(path + ".part", path)


def _doc_key(meta: dict, name: str) -> str:
    images = meta.get("images") or []
    return str(images[0]) if images else name


def push(workdir: str, database: str, client=None, dry_run: bool = False) -> dict:
    """Create or update one page per docs/*.txt. Returns counts by outcome."""
    paths = sorted(glob.glob(os.path.join(workdir, "docs", "*.txt")))
    if not paths:
        raise SystemExit(f"no documents in {os.path.join(workdir, 'docs')}: run `loc transcribe` "
                         "first (or pass --out if you used a different working directory)")
    db_id = parse_database_id(database)

    docs = []
    for path in paths:
        name = os.path.basename(path)
        try:
            with open(path) as f:
                meta, body = output.parse(f.read())
        except (ValueError, OSError) as e:
            print(f"  skipping {name}: {e}", file=sys.stderr)
            continue
        if not isinstance(meta, dict):
            print(f"  skipping {name}: front matter is not a mapping", file=sys.stderr)
            continue
        docs.append((name, meta, body))

    counts = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0, "undated": 0}
    if not dry_run:
        client = client or Client(require_token())
        ds_id, title_prop, date_prop = resolve_data_source(client, db_id)
    else:
        title_prop, date_prop, ds_id = "Description", "Date", db_id

    state_path = os.path.join(workdir, STATE_FILE)
    state = _load_state(state_path)
    pages = state.setdefault(ds_id, {})

    for name, meta, body in docs:
        props = build_properties(meta, title_prop, date_prop)
        blocks = build_blocks(meta, body)
        if date_prop not in props:
            counts["undated"] += 1
            print(f"  warning: {name}: date {meta.get('date')!r} can't be converted; "
                  f"leaving {date_prop} empty")
        if dry_run:
            date = props.get(date_prop, {}).get("date")
            print(f"  would push {name}: {title_prop}={props[title_prop]['title'][0]['text']['content']!r}"
                  f" {date_prop}={date and '/'.join(filter(None, (date['start'], date.get('end'))))}"
                  f" ({len(blocks)} blocks)")
            continue

        key = _doc_key(meta, name)
        digest = hashlib.sha256(json.dumps([props, blocks], sort_keys=True).encode()).hexdigest()
        entry = pages.get(key)
        try:
            if entry and entry.get("hash") == digest:
                counts["unchanged"] += 1
                continue
            if entry:
                try:
                    _update(client, entry["page_id"], props, blocks)
                    counts["updated"] += 1
                except NotionError as e:
                    if e.status != 404:
                        raise
                    entry = None  # page was deleted in Notion: make a new one
            if not entry:
                page = _create(client, ds_id, props, blocks)
                entry = {"page_id": page["id"], "url": page.get("url", "")}
                counts["created"] += 1
            entry["hash"] = digest
            pages[key] = entry
            _save_state(state_path, state)
            print(f"  {name} -> {entry['url'] or entry['page_id']}")
        except NotionError as e:
            counts["failed"] += 1
            print(f"  FAILED {name}: {e}", file=sys.stderr)

    if dry_run:
        print(f"dry run: {len(docs)} documents, nothing sent")
    else:
        print(f"Notion: {counts['created']} created, {counts['updated']} updated, "
              f"{counts['unchanged']} unchanged, {counts['failed']} failed")
    return counts
