import json
import os

import pytest

from loc_helper import cluster, llm, output, transcribe
from loc_helper import loc_api
from loc_helper.loc_api import item_pages, jpeg_size, parse_item_id

from conftest import tiny_jpeg


# ---------- loc_api ----------

@pytest.mark.parametrize("s,expected", [
    ("mss318980007", "mss318980007"),
    ("https://www.loc.gov/item/mss318980007/", "mss318980007"),
    ("https://www.loc.gov/item/mss318980007/?st=gallery", "mss318980007"),
])
def test_parse_item_id(s, expected):
    assert parse_item_id(s, lookup=lambda u: pytest.fail("no lookup expected")) == expected


@pytest.mark.parametrize("url", [
    "https://www.loc.gov/resource/mss31898.0007/?sp=117",
    "https://www.loc.gov/resource/mss31898.mss31898-003_0154_0463/?sp=117",
])
def test_parse_item_id_resolves_resource_urls(url):
    assert parse_item_id(url, lookup=lambda u: "mss318980007") == "mss318980007"


@pytest.mark.parametrize("item_field", [
    "http://www.loc.gov/item/mss318980007/",
    {"id": "http://www.loc.gov/item/mss318980007/"},
])
def test_resource_item_id_reads_item_link(monkeypatch, item_field):
    seen = []
    def fake_fetch_json(url, **kw):
        seen.append(url)
        return {"item": item_field}
    monkeypatch.setattr(loc_api, "fetch_json", fake_fetch_json)
    url = "https://www.loc.gov/resource/mss31898.mss31898-003_0154_0463/?sp=117"
    assert loc_api.resource_item_id(url) == "mss318980007"
    assert seen == ["https://www.loc.gov/resource/mss31898.mss31898-003_0154_0463/?fo=json"]


def test_fetch_item_unknown_id_fails_fast(monkeypatch):
    def boom(url, **kw):
        assert kw["tries"] <= 3
        raise RuntimeError("giving up")
    monkeypatch.setattr(loc_api, "fetch_json", boom)
    with pytest.raises(SystemExit, match="no item 'mss000'"):
        loc_api.fetch_item("mss000")


@pytest.mark.parametrize("start,end", [(0, 5), (50, 10)])
def test_download_rejects_bad_range(tmp_path, start, end):
    with pytest.raises(SystemExit, match="bad image range"):
        loc_api.download("mss318980007", str(tmp_path / "w"), start, end)
    assert not (tmp_path / "w").exists()


def test_missing_files_give_next_step(tmp_path):
    with pytest.raises(SystemExit, match="loc download"):
        loc_api.read_manifest(str(tmp_path))
    with pytest.raises(SystemExit, match="loc cluster"):
        cluster.load_plan(str(tmp_path))


def test_require_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY_FOR_LOC", raising=False)
    with pytest.raises(SystemExit, match="API_KEY_FOR_LOC"):
        llm.require_api_key()
    monkeypatch.setenv("API_KEY_FOR_LOC", "x")
    llm.require_api_key()


def test_parse_item_id_rejects_garbage():
    with pytest.raises(ValueError):
        parse_item_id("not an id at all")


def test_item_pages_numbers_across_resources():
    v = lambda n: [{"url": f"https://tile.loc.gov/x{n}/full/pct:12.5/0/default.jpg"}]
    item = {"resources": [{"url": "https://r1/", "files": [v(1), v(2)]},
                          {"url": "https://r2/", "files": [v(3)]}]}
    pages = item_pages(item)
    assert [p.n for p in pages] == [1, 2, 3]
    assert pages[2].viewer_url == "https://r2/?sp=1"
    assert pages[0].iiif(50) == "https://tile.loc.gov/x1/full/pct:50/0/default.jpg"


def test_jpeg_size(tmp_path):
    p = tmp_path / "a.jpg"
    tiny_jpeg(p, 640, 480)
    assert jpeg_size(str(p)) == (640, 480)


# ---------- clustering ----------

def test_windows_overlap_and_cover_everything():
    ws = cluster.windows(list(range(1, 21)), 8, 3)
    assert ws[0] == list(range(1, 9)) and ws[1][0] == 6
    assert sorted({n for w in ws for n in w}) == list(range(1, 21))


def test_stitch_attaches_and_skips():
    pages = [{"image": n, "role": r, "note": ""} for n, r in
             [(1, "continuation"), (2, "start"), (3, "envelope"), (4, "blank"),
              (5, "continuation"), (6, "start"), (7, "docket"), (8, "target")]]
    plan = cluster.stitch([pages])
    assert [d["images"] for d in plan["documents"]] == [[1], [2, 3, 5], [6, 7]]
    assert [s["image"] for s in plan["skipped"]] == [4, 8]


def test_stitch_earlier_window_wins_on_overlap():
    w1 = [{"image": 1, "role": "start", "note": ""}, {"image": 2, "role": "continuation", "note": ""}]
    w2 = [{"image": 2, "role": "start", "note": ""}, {"image": 3, "role": "continuation", "note": ""}]
    plan = cluster.stitch([w1, w2])
    assert [d["images"] for d in plan["documents"]] == [[1, 2, 3]]


def test_fill_gaps_attaches_missing_image():
    plan = {"documents": [{"id": 1, "images": [1, 3], "roles": {}, "hint": ""}], "skipped": []}
    plan = cluster.fill_gaps(plan, [1, 2, 3])
    assert plan["documents"][0]["images"] == [1, 2, 3]
    assert plan["documents"][0]["roles"]["2"] == "unclassified"


def test_validate_plan_errors():
    plan = {"documents": [{"id": 1, "images": [1, 2]}, {"id": 2, "images": [2, 9]},
                          {"id": 3, "images": [5, 4]}]}
    errs = cluster.validate_plan(plan, set(range(1, 9)))
    assert any("both document 1 and document 2" in e for e in errs)
    assert any("image 9 was not downloaded" in e for e in errs)
    assert any("not in ascending order" in e for e in errs)
    assert cluster.validate_plan({"documents": [{"id": 1, "images": [1]}]}, {1}) == []


# ---------- output ----------

@pytest.mark.parametrize("d,ok", [
    ("1862-09-17", True), ("1862-09", True), ("1862", True), ("1862-09-17?", True),
    ("1862~", True), ("XXXX", True), ("1862-XX-XX", True), ("1862-09-15/1862-09-17", True),
    ("Sept 17 1862", False), ("", False), ("17/09/1862", False),
])
def test_edtf(d, ok):
    assert output.valid_edtf(d) is ok


def test_filename():
    assert output.filename(7, "1862-09-17?", "McClellan to Halleck on Antietam!") == \
        "007_1862-09-17_mcclellan-to-halleck-on-antietam.txt"
    assert output.filename(1, "XXXX", "Ünknown—note") == "001_undated_unknown-note.txt"
    assert output.filename(2, "1862-09-15/1862-09-17", "x") == "002_1862-09-15_to_1862-09-17_x.txt"


def test_front_matter_round_trip():
    meta = {"description": "A: tricky 'quote'", "date": "1862-09?", "images": [1, 2],
            "quality_notes": "Line one\nline two"}
    text = "---\nbody with a --- rule\n"
    back_meta, back_text = output.parse(output.render(meta, text))
    assert back_meta == meta
    assert back_text == text


# ---------- estimates ----------

def test_estimate_batch_is_half(workdir):
    jobs = cluster.build_jobs(workdir, "claude-sonnet-5-5", size=4, overlap=1)
    full, tin, tout = llm.estimate(jobs, batch=False)
    half, _, _ = llm.estimate(jobs, batch=True)
    assert full > 0 and tin > 0 and tout > 0
    assert half == pytest.approx(full / 2)


# ---------- end to end with a fake client ----------

def test_end_to_end_sync(workdir, fake_client):
    plan = cluster.cluster(workdir, "mss123", "claude-sonnet-5-5", batch=False, yes=True,
                           size=4, overlap=1, client=fake_client)
    assert [d["images"] for d in plan["documents"]] == [[2, 3, 4], [6, 7, 8]]
    assert [s["image"] for s in plan["skipped"]] == [1, 5]

    names = transcribe.transcribe(workdir, "claude-opus-5-5", batch=False, yes=True,
                                  client=fake_client)
    assert names == ["001_1862-09-17_letter-starting-image-2.txt",
                     "002_1862-09_letter-starting-image-6.txt"]
    meta, text = output.parse(open(os.path.join(workdir, "docs", names[0])).read())
    assert meta["images"] == [2, 3, 4]
    assert meta["loc_urls"][0] == "https://www.loc.gov/resource/mss12.3/?sp=2"
    assert meta["item"] == "https://www.loc.gov/item/mss123/"
    assert meta["quality_notes"] == "Faded ink."
    assert "[image 3]" in text
    assert "skipped" not in open(os.path.join(workdir, "skipped.csv")).read().split("\n")[1]

    # Re-running is free: everything comes from the cache.
    calls = fake_client.sync_calls
    transcribe.transcribe(workdir, "claude-opus-5-5", batch=False, yes=True, client=fake_client)
    assert fake_client.sync_calls == calls


def test_edited_plan_is_respected(workdir, fake_client):
    cluster.cluster(workdir, "mss123", "claude-sonnet-5-5", False, True, 4, 1, client=fake_client)
    plan = cluster.load_plan(workdir)
    plan["documents"] = [{"id": 1, "images": [2, 3]}, {"id": 2, "images": [4]},
                         {"id": 3, "images": [6, 7, 8]}]
    cluster.write_plan(workdir, plan)
    names = transcribe.transcribe(workdir, "claude-opus-5-5", False, True, client=fake_client)
    assert len(names) == 3 and names[1].startswith("002_")


def test_invalid_plan_stops_before_spending(workdir, fake_client):
    cluster.cluster(workdir, "mss123", "claude-sonnet-5-5", False, True, 4, 1, client=fake_client)
    plan = cluster.load_plan(workdir)
    plan["documents"][1]["images"] = [4, 6]  # 4 is already in document 1
    cluster.write_plan(workdir, plan)
    calls = fake_client.sync_calls
    with pytest.raises(SystemExit, match="both document 1 and document 2"):
        transcribe.transcribe(workdir, "claude-opus-5-5", False, True, client=fake_client)
    assert fake_client.sync_calls == calls


def test_long_document_is_chunked(workdir, monkeypatch):
    monkeypatch.setattr(transcribe, "CHUNK_PAGES", 3)
    plan = {"item_id": "mss123", "documents": [{"id": 1, "images": [1, 2, 3, 4, 5, 6, 7]}]}
    [(doc, jobs)] = transcribe.build_jobs(workdir, plan, "claude-opus-5-5", "medium")
    assert len(jobs) == 3
    texts = [b["text"] for b in jobs[1].content if b["type"] == "text"]
    assert any("Image 3 (CONTEXT ONLY" in t for t in texts)


def test_batch_path_and_resume(workdir, fake_client):
    llm.run_jobs(cluster.build_jobs(workdir, "claude-sonnet-5-5", 4, 1), workdir, batch=True,
                 client=fake_client, poll_seconds=0)
    assert fake_client.batches_created == 1
    assert json.load(open(os.path.join(workdir, "state.json")))["batches"] == {}

    # Simulate an interrupted run: a batch was submitted but its results never collected.
    jobs = transcribe.build_jobs(workdir, {"item_id": "mss123", "documents": [
        {"id": 1, "images": [2, 3]}]}, "claude-opus-5-5", "medium")[0][1]
    created = fake_client.messages.batches.create(
        requests=[{"custom_id": j.key(), "params": j.params()} for j in jobs])
    llm.Store(workdir).save_state({"batches": {created.id: [j.key() for j in jobs]}})
    before = fake_client.batches_created
    res = llm.run_jobs(jobs, workdir, batch=True, client=fake_client, poll_seconds=0)
    assert fake_client.batches_created == before  # resumed, not resubmitted
    assert set(res) == {j.key() for j in jobs}


def test_refusal_is_reported_not_cached(workdir, fake_client):
    from types import SimpleNamespace
    refusal = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                              content=[], model="m", usage=None)
    fake_client.beta.messages.stream = lambda **p: __import__("conftest").FakeStream(refusal)
    jobs = cluster.build_jobs(workdir, "claude-sonnet-5-5", 4, 1)
    assert llm.run_jobs(jobs, workdir, batch=False, client=fake_client) == {}
    assert os.listdir(os.path.join(workdir, "cache")) == []


@pytest.mark.parametrize("argv", [
    ["download", "not an id!"],
    ["cluster", "mss318980007", "--window", "3", "--overlap", "3"],
])
def test_cli_rejects_bad_args_without_traceback(argv, capsys):
    from loc_helper.cli import main
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


# ---------- one-sentence quality notes ----------

@pytest.mark.parametrize("text,expected,cut", [
    ("No significant problems.", "No significant problems.", False),
    ("One sentence with no full stop", "One sentence with no full stop", False),
    ("Faded ink. Docket uncertain.", "Faded ink.", True),
    ("Telegram from Adjt. Genl. S. Cooper is clear. Docket uncertain.",
     "Telegram from Adjt. Genl. S. Cooper is clear.", True),
    ("Names such as Dr [?Arcik?] Leghorn are guessed. Check image 5.",
     "Names such as Dr [?Arcik?] Leghorn are guessed.", True),
    ("Dated c. 1868 from content, e.g. Nell's room. Verify.",
     "Dated c. 1868 from content, e.g. Nell's room.", True),
    ("Clear hand;\n  one word uncertain.", "Clear hand; one word uncertain.", False),
])
def test_first_sentence(text, expected, cut):
    assert transcribe.first_sentence(text) == (expected, cut)


def _results(*notes, date="1862-09-17"):
    return [{"transcription": f"[image {i}]\ntext", "description": "Letter", "date": date,
             "date_basis": "written", "quality_notes": n} for i, n in enumerate(notes, 1)]


MANIFEST = {n: {"loc_viewer_url": f"https://www.loc.gov/resource/x/?sp={n}"} for n in (1, 2)}


def test_assemble_truncates_to_one_sentence(capsys):
    rec = transcribe.assemble({"id": 3, "images": [1]}, _results("Faded. Torn corner."),
                              "mss1", MANIFEST)
    assert rec["meta"]["quality_notes"] == "Faded."
    assert "document 3" in capsys.readouterr().err


def test_assemble_multi_part_uses_merged_note():
    doc = {"id": 1, "images": [1, 2]}
    rec = transcribe.assemble(doc, _results("Part one faded.", "Part two clear."), "mss1",
                              MANIFEST, merged_note="Mostly clear; part one faded.")
    assert rec["meta"]["quality_notes"] == "Mostly clear; part one faded."
    # If the merge failed, keep part 1's sentence instead of joining the parts.
    rec = transcribe.assemble(doc, _results("Part one faded.", "Part two clear."), "mss1", MANIFEST)
    assert rec["meta"]["quality_notes"] == "Part one faded."


def test_assemble_bad_date_stays_one_sentence():
    rec = transcribe.assemble({"id": 1, "images": [1]}, _results("Faded ink.", date="Sept 1862"),
                              "mss1", MANIFEST)
    note = rec["meta"]["quality_notes"]
    assert rec["meta"]["date"] == "XXXX"
    assert note.startswith("Faded ink; model returned a non-EDTF date")
    assert transcribe.first_sentence(note) == (note, False)
    rec = transcribe.assemble({"id": 1, "images": [1]},
                              _results("No significant problems.", date="Sept 1862"), "mss1", MANIFEST)
    assert rec["meta"]["quality_notes"].startswith("Model returned a non-EDTF date")


def test_multi_part_document_gets_one_merged_sentence(workdir, fake_client, monkeypatch):
    cluster.cluster(workdir, "mss123", "claude-sonnet-5-5", batch=False, yes=True,
                    size=4, overlap=1, client=fake_client)
    monkeypatch.setattr(transcribe, "CHUNK_PAGES", 2)  # 3-image documents become 2 parts
    before = fake_client.sync_calls
    names = transcribe.transcribe(workdir, "claude-opus-5-5", batch=True, yes=True,
                                  client=fake_client)
    meta, _ = output.parse(open(os.path.join(workdir, "docs", names[0])).read())
    # The fake merge answer has two sentences; only the first is kept.
    assert meta["quality_notes"] == "Merged: faded ink in places."
    assert "part 1" not in meta["quality_notes"]
    # The merge ran synchronously even in batch mode, and is cached for re-runs.
    calls = fake_client.sync_calls
    assert calls - before == 2
    transcribe.transcribe(workdir, "claude-opus-5-5", batch=True, yes=True, client=fake_client)
    assert fake_client.sync_calls == calls


def test_condense_notes(tmp_path, fake_client):
    assert transcribe.condense_notes(["Long note. More."], str(tmp_path), client=fake_client) == \
        "Merged: faded ink in places. A second sentence."
