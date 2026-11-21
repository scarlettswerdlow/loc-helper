"""loc.gov JSON / IIIF access. Ported from the original download_loc.py."""
from __future__ import annotations

import csv
import http.client
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

UA = {"User-Agent": "Mozilla/5.0 (loc-helper research download)"}


def fetch(url: str, tries: int = 8, timeout: int = 90, max_delay: int = 60) -> tuple[bytes, str]:
    delay = 3
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError) as e:
            code = getattr(e, "code", None)
            if code is not None and code not in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                raise
            if attempt == tries:
                break
            print(f"  retry {attempt}/{tries - 1} ({e}) in {delay}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    raise RuntimeError(f"giving up on {url}")


def fetch_json(url: str, **fetch_kw) -> dict:
    for attempt in range(5):
        body, _ = fetch(url, **fetch_kw)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"no valid JSON from {url}")


_ITEM_RE = re.compile(r"loc\.gov/item/([A-Za-z0-9._-]+?)/?(?:[?#].*)?$")
_RESOURCE_RE = re.compile(r"loc\.gov/resource/([A-Za-z0-9._-]+?)/?(?:[?#].*)?$")


def resource_item_id(resource_url: str) -> str:
    """Look up the item a loc.gov/resource/ URL belongs to.

    Resource IDs can't be turned into item IDs by rule: mss31898.0007 is item
    mss318980007, but mss31898.mss31898-003_0154_0463 is also part of it. The
    resource's JSON names its item, so ask loc.gov.
    """
    url = resource_url.split("?")[0].split("#")[0].rstrip("/") + "/?fo=json"
    try:
        data = fetch_json(url, tries=3, max_delay=10)
    except (RuntimeError, urllib.error.HTTPError) as e:
        raise SystemExit(f"couldn't look up the item for {resource_url} ({e}). "
                         "Pass the loc.gov/item/... URL or item ID instead.")
    item = data.get("item")
    if isinstance(item, dict):
        item = item.get("id") or item.get("url")
    m = _ITEM_RE.search(item or "")
    if not m:
        raise SystemExit(f"loc.gov didn't say which item {resource_url} belongs to. "
                         "Pass the loc.gov/item/... URL or item ID instead.")
    return m.group(1)


def parse_item_id(s: str, lookup=resource_item_id) -> str:
    """Accept a bare item ID, a loc.gov/item/ URL, or a loc.gov/resource/ URL.

    Resource URLs are resolved with `lookup`, which asks loc.gov.
    """
    s = s.strip()
    m = _ITEM_RE.search(s)
    if m:
        return m.group(1)
    if _RESOURCE_RE.search(s):
        return lookup(s)
    if re.fullmatch(r"[A-Za-z0-9._-]+", s):
        return s
    raise ValueError(f"can't find a loc.gov item ID in {s!r}")


def fetch_item(item_id: str) -> dict:
    """Fetch an item's JSON, failing fast if loc.gov doesn't know it.

    loc.gov answers an unknown item ID with 503, the same as an outage, so a
    long retry loop here would just make typos slow. Images keep the full
    retries in `fetch`.
    """
    url = f"{item_url(item_id)}?fo=json"
    try:
        item = fetch_json(url, tries=3, max_delay=10)
    except (RuntimeError, urllib.error.HTTPError) as e:
        raise SystemExit(f"loc.gov has no item {item_id!r} ({e}).\n"
                         f"Check the ID: loc.gov sometimes answers unknown items with the same 503 error as an outage. "
                         f"If {item_url(item_id)} opens in a browser, loc.gov may be down; "
                         f"try again later.")
    if not item.get("resources"):
        raise SystemExit(f"loc.gov item {item_id!r} has no page images to download.")
    return item


def item_url(item_id: str) -> str:
    return f"https://www.loc.gov/item/{item_id}/"


@dataclass
class Page:
    n: int            # global 1-based image number
    viewer_url: str   # LOC viewer link for this image
    iiif_prefix: str  # IIIF base, before /full/...

    def iiif(self, pct: float) -> str:
        return f"{self.iiif_prefix}/full/pct:{pct:g}/0/default.jpg"


def item_pages(item_json: dict) -> list[Page]:
    """Number every image across every resource of the item."""
    resources = item_json.get("resources") or []
    if len(resources) > 1:
        print(f"note: item has {len(resources)} resources; numbering images across all of them",
              file=sys.stderr)
    pages: list[Page] = []
    for res in resources:
        viewer = res.get("url", "")
        for i, variants in enumerate(res.get("files") or [], start=1):
            iiif = next((v["url"] for v in variants if "/full/pct:" in v.get("url", "")), None)
            if iiif is None:
                continue
            # The viewer's ?sp= is per resource; the global number is ours.
            pages.append(Page(len(pages) + 1, f"{viewer}?sp={i}", iiif.split("/full/")[0]))
    return pages


def jpeg_size(path: str) -> tuple[int, int]:
    """(width, height) of a JPEG, read from its SOF marker."""
    with open(path, "rb") as f:
        data = f.read()
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (seg_len,) = struct.unpack(">H", data[i + 2:i + 4])
        i += 2 + seg_len
    raise ValueError(f"no JPEG size marker in {path}")


def _download_set(pages: list[Page], pct: float, outdir: str, pause: float, label: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    for p in pages:
        path = os.path.join(outdir, f"image-{p.n:03d}.jpg")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        body, ctype = fetch(p.iiif(pct))
        if not body.startswith(b"\xff\xd8"):
            raise RuntimeError(f"image {p.n}: not a JPEG ({ctype})")
        with open(path + ".part", "wb") as f:
            f.write(body)
        os.replace(path + ".part", path)
        print(f"  {label} image-{p.n:03d}.jpg {len(body) // 1024} KB")
        time.sleep(pause)


def download(item_id: str, workdir: str, start: int = 1, end: int | None = None,
             size: float = 50, thumb_size: float = 25, pause: float = 1.0) -> dict:
    """Fetch item.json, full-size images and clustering thumbnails. Returns item.json."""
    if start < 1 or (end is not None and end < start):
        raise SystemExit(f"bad image range {start}-{end}: need 1 <= --start <= --end")
    item = fetch_item(item_id)
    pages = item_pages(item)
    if start > len(pages):
        raise SystemExit(f"--start {start} is past the last image ({len(pages)})")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "item.json"), "w") as f:
        json.dump(item, f, indent=1)

    end = min(end or len(pages), len(pages))
    chosen = [p for p in pages if start <= p.n <= end]
    title = item.get("item", {}).get("title", item_id)
    print(f"{title}\n{len(pages)} images; downloading {start}-{end} at pct:{size:g} "
          f"(+ thumbnails at pct:{thumb_size:g})")

    _download_set(chosen, size, os.path.join(workdir, "images"), pause, "image")
    _download_set(chosen, thumb_size, os.path.join(workdir, "thumbs"), pause, "thumb")

    with open(os.path.join(workdir, "manifest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image_number", "loc_viewer_url", "source_url", "file"])
        for p in chosen:
            w.writerow([p.n, p.viewer_url, p.iiif(size), f"images/image-{p.n:03d}.jpg"])
    with open(os.path.join(workdir, "README.txt"), "w") as f:
        f.write(f"{title}\n{item_url(item_id)}\nImages {start}-{end} of {len(pages)}\n")
    print(f"done: {len(chosen)} images in {workdir}")
    return item


def read_manifest(workdir: str) -> dict[int, dict]:
    path = os.path.join(workdir, "manifest.csv")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found: run `loc download` first "
                         "(or pass --out if you used a different working directory)")
    with open(path, newline="") as f:
        return {int(r["image_number"]): r for r in csv.DictReader(f)}


def item_context(item: dict) -> str:
    """A few lines of catalog metadata to orient the model."""
    it = item.get("item", {})
    parts = [f"Title: {it.get('title', '')}"]
    for key in ("date", "dates", "created_published", "contributor_names", "partof"):
        v = it.get(key)
        if v:
            if isinstance(v, list):
                v = "; ".join(str(x.get("title", x) if isinstance(x, dict) else x) for x in v[:5])
            parts.append(f"{key}: {v}")
    return "\n".join(parts)
