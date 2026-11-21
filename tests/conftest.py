import csv
import json
import os
import re
import struct
from types import SimpleNamespace

import pytest


def tiny_jpeg(path, w=1000, h=1400):
    """Just enough JPEG for size parsing: SOI, SOF0, EOI."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    with open(path, "wb") as f:
        f.write(b"\xff\xd8" + sof + b"\xff\xd9")


@pytest.fixture
def workdir(tmp_path):
    """An item workdir as `loc download` leaves it, for images 1-8."""
    d = tmp_path / "mss123"
    (d / "images").mkdir(parents=True)
    (d / "thumbs").mkdir()
    item = {"item": {"title": "Test Papers: General Correspondence, 1862", "date": "1862"},
            "resources": [{"url": "https://www.loc.gov/resource/mss12.3/", "files": []}]}
    (d / "item.json").write_text(json.dumps(item))
    with open(d / "manifest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["image_number", "loc_viewer_url", "source_url", "file"])
        for n in range(1, 9):
            tiny_jpeg(d / "images" / f"image-{n:03d}.jpg")
            tiny_jpeg(d / "thumbs" / f"image-{n:03d}.jpg", 250, 350)
            w.writerow([n, f"https://www.loc.gov/resource/mss12.3/?sp={n}", "x", f"images/image-{n:03d}.jpg"])
    return str(d)


# Roles a "model" assigns to images 1-8: two letters, a blank, a docket, a target.
ROLES = {1: "target", 2: "start", 3: "continuation", 4: "envelope", 5: "blank",
         6: "start", 7: "continuation", 8: "docket"}


def _images_in(params):
    texts = [b["text"] for b in params["messages"][0]["content"] if b["type"] == "text"]
    return [int(m.group(1)) for t in texts for m in [re.match(r"Image (\d+):$", t)] if m]


def fake_answer(params):
    images = _images_in(params)
    if "pages" in params["output_config"]["format"]["schema"]["properties"]:
        body = {"pages": [{"image": n, "role": ROLES[n], "note": f"note {n}"} for n in images]}
    else:
        body = {"transcription": "\n".join(f"[image {n}]\nDear Sir, text {n}" for n in images),
                "description": f"Letter starting image {images[0]}",
                "date": "1862-09-17" if images[0] < 6 else "1862-09?",
                "date_basis": "written", "quality_notes": "Faded ink."}
    return SimpleNamespace(
        stop_reason="end_turn", model=params["model"],
        content=[SimpleNamespace(type="text", text=json.dumps(body))],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50))


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self):
        self.sync_calls = 0
        self.batches_created = 0
        self._batches = {}
        self._polls = {}
        client = self

        class Messages:
            def stream(self_inner, **params):
                client.sync_calls += 1
                params.pop("betas"), params.pop("fallbacks")
                return FakeStream(fake_answer(params))

        class Batches:
            def create(self_inner, requests):
                client.batches_created += 1
                bid = f"batch_{client.batches_created}"
                client._batches[bid] = requests
                client._polls[bid] = 0
                return SimpleNamespace(id=bid)

            def retrieve(self_inner, bid):
                client._polls[bid] += 1
                status = "ended" if client._polls[bid] > 1 else "in_progress"
                return SimpleNamespace(processing_status=status,
                                       request_counts=SimpleNamespace(processing=1, succeeded=0))

            def results(self_inner, bid):
                for r in client._batches[bid]:
                    yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
                        type="succeeded", message=fake_answer(r["params"])))

        self.beta = SimpleNamespace(messages=Messages())
        self.messages = SimpleNamespace(batches=Batches())


@pytest.fixture
def fake_client():
    return FakeClient()
