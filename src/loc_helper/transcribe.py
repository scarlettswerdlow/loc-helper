"""Transcribe each document in documents.json and write the text files."""
from __future__ import annotations

import json
import os

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


def assemble(doc: dict, results: list[dict], item_id: str, manifest: dict[int, dict]) -> dict:
    first = results[0]
    text = "\n\n".join(r["transcription"].strip() for r in results)
    notes = [r["quality_notes"].strip() for r in results if r["quality_notes"].strip()]
    if len(results) > 1:
        notes = [f"part {i + 1}: {n}" for i, n in enumerate(notes)]
    date = first["date"].strip()
    if not output.valid_edtf(date):
        notes.append(f"Model returned a non-EDTF date {date!r}; recorded as XXXX.")
        date = "XXXX"
    meta = {
        "description": first["description"].strip(),
        "date": date,
        "date_basis": first["date_basis"].strip(),
        "item": item_url(item_id),
        "images": doc["images"],
        "loc_urls": [manifest[n]["loc_viewer_url"] for n in doc["images"]],
        "model": first.get("_model", ""),
        "quality_notes": " ".join(notes) or "No significant problems.",
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

    records, failed = [], []
    for doc, js in per_doc:
        if all(j.key() in results for j in js):
            records.append(assemble(doc, [results[j.key()] for j in js], plan["item_id"], manifest))
        else:
            failed.append(doc["id"])
    names = output.write_documents(workdir, records, plan.get("skipped", []))
    print(f"  wrote {len(names)} documents to {os.path.join(workdir, 'docs')}")
    if failed:
        print(f"  {len(failed)} documents failed ({', '.join(map(str, failed))}); "
              "re-run `loc transcribe` to retry them")
    return names
