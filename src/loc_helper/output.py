"""Text files with YAML front matter, plus index.csv and skipped.csv."""
from __future__ import annotations

import csv
import glob
import os
import re
import unicodedata

import yaml

# EDTF level 0/1 subset: 1862, 1862-09, 1862-09-17, XXXX, 1862-XX, with ?, ~ or % qualifiers,
# and ranges a/b (either end may be open with "..").
_EDTF_PART = r"(?:\d{4}|\d{3}X|\d{2}XX|XXXX)(?:-(?:\d{2}|XX)(?:-(?:\d{2}|XX))?)?[?~%]?"
EDTF_RE = re.compile(rf"^(?:{_EDTF_PART}|(?:{_EDTF_PART}|\.\.)/(?:{_EDTF_PART}|\.\.))$")


def valid_edtf(s: str) -> bool:
    return bool(EDTF_RE.match(s or ""))


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[\W_]+", " ", text)  # punctuation (incl. dashes) separates words
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0]
    return text or "untitled"


def date_for_filename(date: str) -> str:
    if not valid_edtf(date) or date.startswith("XXXX"):
        return "undated"
    d = date.replace("?", "").replace("~", "").replace("%", "")
    return d.replace("/", "_to_").replace("..", "open")


def filename(seq: int, date: str, description: str) -> str:
    return f"{seq:03d}_{date_for_filename(date)}_{slugify(description)}.txt"


def render(meta: dict, text: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100)
    return f"---\n{front}---\n{text.rstrip()}\n"


def parse(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n"):
        raise ValueError("no front matter")
    _, front, body = content.split("---\n", 2)
    return yaml.safe_load(front), body


def write_documents(workdir: str, records: list[dict], skipped: list[dict]) -> list[str]:
    """records: [{seq, meta, text}]. Rewrites docs/ from scratch (it is derived output)."""
    docs_dir = os.path.join(workdir, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    for old in glob.glob(os.path.join(docs_dir, "*.txt")):
        os.remove(old)

    names = []
    rows = []
    for r in records:
        meta = r["meta"]
        name = filename(r["seq"], meta["date"], meta["description"])
        with open(os.path.join(docs_dir, name), "w") as f:
            f.write(render(meta, r["text"]))
        names.append(name)
        rows.append([r["seq"], f"docs/{name}", meta["description"], meta["date"],
                     meta["date_basis"], " ".join(map(str, meta["images"])),
                     meta["loc_urls"][0] if meta["loc_urls"] else "", meta["quality_notes"]])

    with open(os.path.join(workdir, "index.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seq", "file", "description", "date", "date_basis", "images",
                    "first_loc_url", "quality_notes"])
        w.writerows(rows)
    with open(os.path.join(workdir, "skipped.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image_number", "role", "note"])
        for s in skipped:
            w.writerow([s["image"], s.get("role", ""), s.get("note", "")])
    return names
