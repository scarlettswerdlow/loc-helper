"""`loc` command line: download, cluster, transcribe, run."""
from __future__ import annotations

import argparse
import os
import sys

from . import cluster as cluster_mod
from . import transcribe as transcribe_mod
from .loc_api import download, parse_item_id

DEFAULT_CLUSTER_MODEL = "claude-sonnet-5-5"
DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("item", help="LOC item ID (e.g. mss318980007) or loc.gov item/resource URL")
    p.add_argument("--out", help="working directory (default: ./<item_id>)")


def _download_args(p):
    p.add_argument("--start", type=int, default=1, help="first image number (as in the LOC viewer)")
    p.add_argument("--end", type=int, default=None, help="last image number (default: last)")
    p.add_argument("--size", type=float, default=50,
                   help="IIIF percentage for transcription images: 12.5/25/50/100 (default 50)")
    p.add_argument("--thumb-size", type=float, default=25,
                   help="IIIF percentage for clustering thumbnails (default 25)")
    p.add_argument("--pause", type=float, default=1.0, help="seconds between downloads")


def _api_args(p):
    p.add_argument("--no-batch", action="store_true",
                   help="send requests one at a time instead of via the Batch API (2x the price, "
                        "but no waiting)")
    p.add_argument("-y", "--yes", action="store_true", help="skip the cost confirmation")


def _cluster_args(p):
    p.add_argument("--cluster-model", default=DEFAULT_CLUSTER_MODEL)
    p.add_argument("--window", type=int, default=12, help="images per clustering request")
    p.add_argument("--overlap", type=int, default=3, help="images shared by adjacent windows")


def _transcribe_args(p):
    p.add_argument("--model", default=DEFAULT_MODEL, help="transcription model")
    p.add_argument("--effort", default="medium", choices=EFFORTS,
                   help="reasoning effort for transcription (default medium)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="loc", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("download", help="download page images and item metadata")
    _common(p); _download_args(p)

    p = sub.add_parser("cluster", help="group downloaded images into documents -> documents.json")
    _common(p); _cluster_args(p); _api_args(p)

    p = sub.add_parser("transcribe", help="transcribe the documents in documents.json -> docs/")
    _common(p); _transcribe_args(p); _api_args(p)

    p = sub.add_parser("run", help="download + cluster, then stop for review (or --auto to finish)")
    _common(p); _download_args(p); _cluster_args(p); _transcribe_args(p); _api_args(p)
    p.add_argument("--auto", action="store_true",
                   help="go straight on to transcription without stopping to review documents.json")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        item_id = parse_item_id(a.item)
    except ValueError as e:
        ap.error(str(e))
    if a.cmd in ("cluster", "run") and not 0 <= a.overlap < a.window:
        ap.error(f"--overlap ({a.overlap}) must be at least 0 and less than --window ({a.window})")
    workdir = a.out or item_id
    batch = not getattr(a, "no_batch", False)

    if a.cmd in ("download", "run"):
        download(item_id, workdir, a.start, a.end, a.size, a.thumb_size, a.pause)
    if a.cmd in ("cluster", "run"):
        if cluster_mod.cluster(workdir, item_id, a.cluster_model, batch, a.yes,
                               a.window, a.overlap) is None:
            return 1
        if a.cmd == "run" and not a.auto:
            plan_path = os.path.join(workdir, "documents.json")
            print(f"\nReview {plan_path}: merge or split documents by editing their 'images' "
                  f"lists, then run:\n  loc transcribe {item_id}"
                  + (f" --out {a.out}" if a.out else ""))
            return 0
    if a.cmd in ("transcribe", "run"):
        if transcribe_mod.transcribe(workdir, a.model, batch, a.yes, a.effort) is None:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
