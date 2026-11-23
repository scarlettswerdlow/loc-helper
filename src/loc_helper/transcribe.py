"""Transcribe each document in documents.json and write the text files."""
from __future__ import annotations

import json
import os
import re
import sys

from . import llm, output
from .cluster import load_plan, prompt, validate_plan
from .loc_api import item_context, item_url, read_manifest

CHUNK_PAGES = 10  # longer documents are split so each response stays well under max_tokens

SCHEMA = {
    "type": "object",
    "properties": {
        "transcription": {"type": "string"},
        "description": {"type": "string"},
        "date": {"type": "string"},
        "date_basis": {"type": "string"},
        "quality_notes": {"type": "string"},
    },
    "required": ["transcription", "description", "date", "date_basis", "quality_notes"],
    "additionalProperties": False,
}


NOTES_MODEL = "claude-haiku-5-5"  # merges per-part quality notes into one sentence
NOTES_SCHEMA = {
    "type": "object",
    "properties": {"quality_notes": {"type": "string"}},
    "required": ["quality_notes"],
    "additionalProperties": False,
}
NO_PROBLEMS = "No significant problems."

# Words that end in a full stop without ending the sentence ("Genl. McClellan", "c. 1868").
ABBREVIATIONS = {
    "adjt", "asst", "capt", "cf", "co", "col", "dept", "do", "dr", "e.g", "esq", "etc", "gen",
    "genl", "gov", "hon", "i.e", "jr", "lieut", "lt", "maj", "messrs", "mr", "mrs", "no",
    "nos", "p", "pp", "prof", "pvt", "regt", "rev", "sec", "sergt", "sgt", "sr", "st", "vol",
    "vs", "c", "ca", "approx", "fig", "ms", "mss", "viz",
}
# A full stop, ! or ? (plus any closing quote or bracket) followed by space and a capital,
# digit, quote or bracket. "?" right after "[" or before "]" is an uncertain-reading marker.
_BOUNDARY = re.compile(r"""(?<!\[)([.!?])(["')]*)\s+(?=["'(\[A-Z0-9])""")


def first_sentence(text: str) -> tuple[str, bool]:
    """(first sentence of text, whether anything after it was dropped)."""
    text = " ".join((text or "").split())
    for m in _BOUNDARY.finditer(text):
        if m.group(1) == ".":
            word = re.search(r"([\w.]+)$", text[:m.start()])
            w = word.group(1).lower() if word else ""
            if w in ABBREVIATIONS or (len(w) == 1 and w.isalpha()):
                continue  # abbreviation or an initial ("S. Cooper")
        return text[:m.end(2)], True
    return text, False


def one_sentence(note: str, where: str) -> str:
    sentence, cut = first_sentence(note)
    if cut:
        print(f"  warning: {where}: quality_notes had more than one sentence; kept the first",
              file=sys.stderr)
    return sentence or NO_PROBLEMS


def condense_job(notes: list[str], name: str) -> llm.Job:
    """A small text-only request that turns several quality notes into one sentence."""
    listing = "\n".join(f"- {n.strip()}" for n in notes if n.strip()) or f"- {NO_PROBLEMS}"
    return llm.Job(
        name=f"quality notes for {name}", model=NOTES_MODEL, system=prompt("condense"),
        content=[{"type": "text", "text": f"Notes:\n{listing}"}], schema=NOTES_SCHEMA,
        max_tokens=4000, effort="low", expected_output_tokens=200,
    )


def condense_notes(notes: list[str], workdir: str, client=None) -> str | None:
    """One sentence summarising notes, or None if the request failed. Cached in workdir/cache."""
    job = condense_job(notes, "document")
    result = llm.run_jobs([job], workdir, batch=False, client=client).get(job.key())
    return None if result is None else result["quality_notes"]


def chunks(images: list[int], size: int | None = None) -> list[list[int]]:
    size = size or CHUNK_PAGES
    return [images[i:i + size] for i in range(0, len(images), size)]


def build_jobs(workdir: str, plan: dict, model: str, effort: str) -> list[tuple[dict, list[llm.Job]]]:
    with open(os.path.join(workdir, "item.json")) as f:
        context = item_context(json.load(f))
    system = prompt("transcribe")
    out = []
    for doc in plan["documents"]:
        parts = chunks(doc["images"])
        jobs = []
        for k, part in enumerate(parts):
            content: list = [{"type": "text", "text": f"Catalog record for this item:\n{context}\n"}]
            if doc.get("hint"):
                content.append({"type": "text", "text": f"Reviewer's note on this document: {doc['hint']}"})
            paths = []
            if k > 0:
                # Batch requests run independently, so give a later chunk the previous page
                # as context instead of the previous chunk's output.
                prev = parts[k - 1][-1]
                p = os.path.join(workdir, "images", f"image-{prev:03d}.jpg")
                content += [{"type": "text", "text": f"Image {prev} (CONTEXT ONLY: already "
                             "transcribed, do not transcribe it again):"}, llm.image_block(p)]
                paths.append(p)
            for n in part:
                p = os.path.join(workdir, "images", f"image-{n:03d}.jpg")
                content += [{"type": "text", "text": f"Image {n}:"}, llm.image_block(p)]
                paths.append(p)
            ask = f"Transcribe images {part[0]}-{part[-1]}."
            if len(parts) > 1:
                ask += (f" This is part {k + 1} of {len(parts)} of a document spanning images "
                        f"{doc['images'][0]}-{doc['images'][-1]}; give description and date for "
                        "the whole document as best you can.")
            content.append({"type": "text", "text": ask})
            jobs.append(llm.Job(
                name=f"document {doc['id']} images {part[0]}-{part[-1]}", model=model,
                system=system, content=content, schema=SCHEMA, max_tokens=32000, effort=effort,
                image_paths=paths, expected_output_tokens=700 * len(part) + 1500,
            ))
        out.append((doc, jobs))
    return out


def assemble(doc: dict, results: list[dict], item_id: str, manifest: dict[int, dict],
             merged_note: str | None = None) -> dict:
    """merged_note: the one-sentence note for a multi-part document (see condense_job)."""
    first = results[0]
    text = "\n\n".join(r["transcription"].strip() for r in results)
    where = f"document {doc['id']}"
    if len(results) == 1:
        note = one_sentence(first["quality_notes"], where)
    elif merged_note is not None:
        note = one_sentence(merged_note, where)
    else:
        print(f"  warning: {where}: couldn't merge the quality notes of its {len(results)} parts; "
              "kept part 1's", file=sys.stderr)
        note = one_sentence(first["quality_notes"], where)
    date = first["date"].strip()
    if not output.valid_edtf(date):
        # Keep it one sentence: fold the warning into the note.
        bad = f"model returned a non-EDTF date {date!r}, recorded as XXXX"
        note = bad[0].upper() + bad[1:] + "." if note == NO_PROBLEMS else f"{note.rstrip('.')}; {bad}."
        date = "XXXX"
    meta = {
        "description": first["description"].strip(),
        "date": date,
        "date_basis": first["date_basis"].strip(),
        "item": item_url(item_id),
        "images": doc["images"],
        "loc_urls": [manifest[n]["loc_viewer_url"] for n in doc["images"]],
        "model": first.get("_model", ""),
        "quality_notes": note,
    }
    return {"seq": doc["id"], "meta": meta, "text": text}


def transcribe(workdir: str, model: str, batch: bool, yes: bool, effort: str = "medium",
               client=None) -> list[str] | None:
    plan = load_plan(workdir)
    manifest = read_manifest(workdir)
    errs = validate_plan(plan, set(manifest))
    if errs:
        raise SystemExit("documents.json has problems:\n  " + "\n  ".join(errs))

    per_doc = build_jobs(workdir, plan, model, effort)
    jobs = [j for _, js in per_doc for j in js]
    store = llm.Store(workdir)
    uncached = [j for j in jobs if store.get(j.key()) is None]
    if uncached and client is None:
        llm.require_api_key()
    if not llm.confirm_spend(uncached, batch, yes, "Transcription"):
        return None
    results = llm.run_jobs(jobs, workdir, batch, client=client)

    done, failed = [], []
    for doc, js in per_doc:
        if all(j.key() in results for j in js):
            done.append((doc, [results[j.key()] for j in js]))
        else:
            failed.append(doc["id"])

    # Long documents come back as several parts, each with its own note: merge them into one
    # sentence with a small text-only request. Always sent immediately (it's tiny), and cached.
    merges = {doc["id"]: condense_job([r["quality_notes"] for r in rs], f"document {doc['id']}")
              for doc, rs in done if len(rs) > 1}
    merged: dict = {}
    if merges:
        if client is None and any(store.get(j.key()) is None for j in merges.values()):
            llm.require_api_key()
        print(f"  merging quality notes for {len(merges)} multi-part documents ({NOTES_MODEL})")
        out = llm.run_jobs(list(merges.values()), workdir, batch=False, client=client)
        merged = {d: out[j.key()]["quality_notes"] for d, j in merges.items() if j.key() in out}

    records = [assemble(doc, rs, plan["item_id"], manifest, merged.get(doc["id"]))
               for doc, rs in done]
    names = output.write_documents(workdir, records, plan.get("skipped", []))
    print(f"  wrote {len(names)} documents to {os.path.join(workdir, 'docs')}")
    if failed:
        print(f"  {len(failed)} documents failed ({', '.join(map(str, failed))}); "
              "re-run `loc transcribe` to retry them")
    return names
