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
    props = params["output_config"]["format"]["schema"]["properties"]
    if list(props) == ["quality_notes"]:
        body = {"quality_notes": "Merged: faded ink in places. A second sentence."}
    elif "pages" in props:
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


class FakeNotion:
    """In-memory stand-in for notion.Client: one database, one data source, pages and blocks."""

    def __init__(self, properties=None):
        self.properties = properties or {
            "Description": {"type": "title"}, "Date": {"type": "date"},
            "Copy": {"type": "checkbox"}, "Read": {"type": "checkbox"},
            "Relevant": {"type": "checkbox"}}
        self.pages = {}     # page id -> {"properties", "children": [block ids], "trashed"}
        self.blocks = {}    # block id -> block
        self.calls = []
        self._n = 0

    def _new_blocks(self, page_id, blocks):
        for b in blocks:
            self._n += 1
            bid = f"block{self._n}"
            self.blocks[bid] = b
            self.pages[page_id]["children"].append(bid)

    def request(self, method, path, body=None, tries=6):
        self.calls.append((method, path, body))
        path = path.split("?")[0]
        if method == "GET" and path.startswith("/databases/"):
            return {"data_sources": [{"id": "ds1", "name": "Papers"}]}
        if method == "GET" and path == "/data_sources/ds1":
            return {"properties": self.properties}
        if method == "POST" and path == "/pages":
            assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
            assert len(body["children"]) <= 100
            self._n += 1
            pid = f"page{self._n}"
            self.pages[pid] = {"properties": body["properties"], "children": [], "trashed": False}
            self._new_blocks(pid, body["children"])
            return {"id": pid, "url": f"https://www.notion.so/{pid}"}
        if method == "PATCH" and path.startswith("/pages/"):
            page = self.pages.get(path.rsplit("/", 1)[1])
            if page is None:
                from loc_helper.notion import NotionError
                raise NotionError("not found", 404)
            page["properties"].update(body["properties"])
            return {"id": path.rsplit("/", 1)[1], "in_trash": page["trashed"]}
        if method == "GET" and path.startswith("/blocks/") and path.endswith("/children"):
            page = self.pages[path.split("/")[2]]
            return {"results": [{"id": b} for b in page["children"][:100]]}
        if method == "DELETE" and path.startswith("/blocks/"):
            bid = path.rsplit("/", 1)[1]
            self.blocks.pop(bid)
            for page in self.pages.values():
                if bid in page["children"]:
                    page["children"].remove(bid)
            return {}
        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            assert len(body["children"]) <= 100
            self._new_blocks(path.split("/")[2], body["children"])
            return {}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def fake_notion():
    return FakeNotion()
