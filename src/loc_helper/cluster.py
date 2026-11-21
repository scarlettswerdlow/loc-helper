"""Group page images into documents (contiguous runs) with Claude."""
from __future__ import annotations

import json
import os
from importlib import resources

from . import llm
from .loc_api import item_context, read_manifest

ROLES = ["start", "continuation", "envelope", "docket", "enclosure", "blank", "target", "cover"]
ATTACH = {"continuation", "envelope", "docket", "enclosure"}
SKIP = {"blank", "target", "cover"}

SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image": {"type": "integer"},
                    "role": {"type": "string", "enum": ROLES},
                    "note": {"type": "string"},
                },
                "required": ["image", "role", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pages"],
    "additionalProperties": False,
}


def prompt(name: str) -> str:
    return resources.files("loc_helper").joinpath(f"prompts/{name}.md").read_text()


def windows(images: list[int], size: int, overlap: int) -> list[list[int]]:
    if size <= overlap:
        raise ValueError("window size must be larger than overlap")
    out, i = [], 0
    while i < len(images):
        out.append(images[i:i + size])
        if i + size >= len(images):
            break
        i += size - overlap
    return out


def build_jobs(workdir: str, model: str, size: int = 12, overlap: int = 3,
               effort: str = "low") -> list[llm.Job]:
    with open(os.path.join(workdir, "item.json")) as f:
        context = item_context(json.load(f))
    images = sorted(read_manifest(workdir))
    jobs = []
    for w in windows(images, size, overlap):
        content: list = [{"type": "text", "text": f"Catalog record for this item:\n{context}\n"}]
        paths = []
        for n in w:
            path = os.path.join(workdir, "thumbs", f"image-{n:03d}.jpg")
            content.append({"type": "text", "text": f"Image {n}:"})
            content.append(llm.image_block(path))
            paths.append(path)
        content.append({"type": "text", "text": f"Classify images {w[0]}-{w[-1]}."})
        jobs.append(llm.Job(
            name=f"cluster images {w[0]}-{w[-1]}", model=model, system=prompt("cluster"),
            content=content, schema=SCHEMA, max_tokens=8000, effort=effort,
            image_paths=paths, expected_output_tokens=60 * len(w) + 500,
        ))
    return jobs


def stitch(window_results: list[list[dict]]) -> dict:
    """Merge per-window classifications into documents.

    Where windows overlap, the EARLIER window's call wins: an image near the end of a window
    was judged with the pages before it in view, which is what start-vs-continuation needs.
    The overlap exists to give the next window that same preceding context.
    """
    roles: dict[int, dict] = {}
    for pages in window_results:
        for p in pages:
            roles.setdefault(int(p["image"]), p)

    docs: list[dict] = []
    skipped: list[dict] = []
    for n in sorted(roles):
        p = roles[n]
        role = p["role"]
        if role in SKIP:
            skipped.append({"image": n, "role": role, "note": p.get("note", "")})
            continue
        if role == "start" or not docs:  # a leading continuation still opens a document
            docs.append({"id": len(docs) + 1, "images": [], "roles": {}, "hint": p.get("note", "")})
        docs[-1]["images"].append(n)
        docs[-1]["roles"][str(n)] = role
    return {"documents": docs, "skipped": skipped}


def cluster(workdir: str, item_id: str, model: str, batch: bool, yes: bool, size: int = 12,
            overlap: int = 3, client=None) -> dict | None:
    jobs = build_jobs(workdir, model, size, overlap)
    uncached = [j for j in jobs if llm.Store(workdir).get(j.key()) is None]
    if not llm.confirm_spend(uncached, batch, yes, "Clustering"):
        return None
    results = llm.run_jobs(jobs, workdir, batch, client=client)
    missing = [j.name for j in jobs if j.key() not in results]
    if missing:
        raise SystemExit(f"clustering incomplete ({', '.join(missing)}); re-run to retry")

    expected = sorted(read_manifest(workdir))
    plan = stitch([results[j.key()]["pages"] for j in jobs])
    plan = {"item_id": item_id, **fill_gaps(plan, expected)}
    write_plan(workdir, plan)
    print(f"  {len(plan['documents'])} documents, {len(plan['skipped'])} skipped images "
          f"-> {os.path.join(workdir, 'documents.json')}")
    return plan


def fill_gaps(plan: dict, expected: list[int]) -> dict:
    """Attach any image the model failed to classify to the preceding document, flagged."""
    seen = {n for d in plan["documents"] for n in d["images"]}
    seen |= {s["image"] for s in plan["skipped"]}
    for n in expected:
        if n in seen:
            continue
        print(f"  warning: no classification for image {n}; attached to preceding document")
        prev = [d for d in plan["documents"] if d["images"] and d["images"][0] < n]
        target = prev[-1] if prev else (plan["documents"][0] if plan["documents"] else None)
        if target is None:
            target = {"id": 1, "images": [], "roles": {}, "hint": ""}
            plan["documents"].append(target)
        target["images"] = sorted(target["images"] + [n])
        target["roles"][str(n)] = "unclassified"
    return plan


def write_plan(workdir: str, plan: dict) -> None:
    path = os.path.join(workdir, "documents.json")
    with open(path + ".part", "w") as f:
        json.dump(plan, f, indent=2)
    os.replace(path + ".part", path)


def load_plan(workdir: str) -> dict:
    with open(os.path.join(workdir, "documents.json")) as f:
        return json.load(f)


def validate_plan(plan: dict, available: set[int]) -> list[str]:
    errs = []
    docs = plan.get("documents")
    if not isinstance(docs, list) or not docs:
        return ["documents.json has no 'documents' list"]
    owner: dict[int, object] = {}
    last_first = 0
    for d in docs:
        did = d.get("id")
        imgs = d.get("images")
        if not isinstance(imgs, list) or not imgs or not all(isinstance(n, int) for n in imgs):
            errs.append(f"document {did}: 'images' must be a non-empty list of image numbers")
            continue
        for n in imgs:
            if n not in available:
                errs.append(f"document {did}: image {n} was not downloaded")
            if n in owner:
                errs.append(f"image {n} is in both document {owner[n]} and document {did}")
            owner[n] = did
        if imgs != sorted(imgs):
            errs.append(f"document {did}: images are not in ascending order")
        if imgs[0] < last_first:
            errs.append(f"document {did}: out of order (starts before the previous document)")
        last_first = imgs[0]
    ids = [d.get("id") for d in docs]
    if len(set(ids)) != len(ids):
        errs.append("document ids are not unique")
    return errs
