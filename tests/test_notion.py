import json
import os

import pytest

from loc_helper import cli, notion, output

DB_LINK = ("https://app.notion.com/p/418673b101be80e086aedef6e5ba1727"
           "?v=418673b101be80a8a88d000cbf6c97e0&source=copy_link")
DB_ID = "418673b101be80e086aedef6e5ba1727"


def make_docs(workdir, specs):
    """specs: [(images, date, description, text)] -> docs/*.txt as `loc transcribe` writes them."""
    records = []
    for seq, (images, date, desc, text) in enumerate(specs, start=1):
        meta = {"description": desc, "date": date, "date_basis": "written",
                "item": "https://www.loc.gov/item/mss123/", "images": images,
                "loc_urls": [f"https://www.loc.gov/resource/x/?sp={n}" for n in images],
                "model": "claude-opus-5-5", "quality_notes": "Faded ink."}
        records.append({"seq": seq, "meta": meta, "text": text})
    output.write_documents(workdir, records, [])


def page_text(fake, page_id):
    return [b[b["type"]]["rich_text"][0]["text"]["content"]
            for b in (fake.blocks[i] for i in fake.pages[page_id]["children"])]


# --- parse_database_id ----------------------------------------------------------------------

@pytest.mark.parametrize("s", [
    DB_LINK, DB_ID, DB_ID.upper(),
    "https://www.notion.so/workspace/My-Papers-" + DB_ID + "?v=1",
    "418673b1-01be-80e0-86ae-def6e5ba1727",
])
def test_parse_database_id(s):
    assert notion.parse_database_id(s) == DB_ID


@pytest.mark.parametrize("s", ["", "hello", "https://notion.so/", DB_ID[:-1]])
def test_parse_database_id_rejects(s):
    with pytest.raises(ValueError):
        notion.parse_database_id(s)


# --- dates ----------------------------------------------------------------------------------

@pytest.mark.parametrize("edtf,expected", [
    ("1855-03-30", ("1855-03-30", None)),
    ("1855-03", ("1855-03-01", None)),
    ("1855", ("1855-01-01", None)),
    ("1862-09-17?", ("1862-09-17", None)),
    ("1862~", ("1862-01-01", None)),
    ("1862-09-15/1862-09-17", ("1862-09-15", "1862-09-17")),
    ("1862-09/1862-10", ("1862-09-01", "1862-10-31")),
    ("1862/..", ("1862-01-01", None)),
    ("../1862-09-17", ("1862-09-17", None)),
    ("186X", ("1860-01-01", None)),
    ("1862-09-17/1862-09-17", ("1862-09-17", None)),
    ("XXXX", None),
    ("1862-02-30", None),
    ("garbage", None),
    ("", None),
])
def test_edtf_to_notion(edtf, expected):
    assert notion.edtf_to_notion(edtf) == expected


# --- reflow ---------------------------------------------------------------------------------

def test_reflow_joins_wrapped_lines_and_keeps_labels():
    body = ("[image 117]\n"
            "Head Quarters Army of the Potomac\n"
            "Sept 17 1862\n"
            "\n"
            "Dear Sir,\n"
            "I have the honor to report that the enemy was driven from his position this\n"
            "morning after a severe engagement lasting until dark, and that our losses\n"
            "have been heavy but not disproportionate to the object gained by it.\n"
            "Very respectfully\n"
            "Your obt servt\n"
            "[envelope]\n"
            "Genl Halleck\n")
    assert notion.reflow(body) == [
        "[image 117]",
        "Head Quarters Army of the Potomac",  # two short lines stay separate (letterhead, dateline)
        "Sept 17 1862",
        "Dear Sir,",
        "I have the honor to report that the enemy was driven from his position this "
        "morning after a severe engagement lasting until dark, and that our losses "
        "have been heavy but not disproportionate to the object gained by it.",
        "Very respectfully",
        "Your obt servt",
        "[envelope]",
        "Genl Halleck",
    ]


def test_reflow_hyphen_and_indent_and_marginalia():
    body = ("The inter-\nested parties were notified of the proposal and of the terms upon which\n"
            "  It was agreed that the work should proceed without delay upon receipt\n"
            "of the order which was expected shortly.\n"
            "[margin: see letter of the 3d]\n")
    paras = notion.reflow(body)
    assert paras[0].startswith("The inter-ested parties")
    assert paras[1].startswith("It was agreed")
    assert paras[-1] == "[margin: see letter of the 3d]"


def test_reflow_few_lines_just_joins():
    assert notion.reflow("one\ntwo\nthree") == ["one two three"]


def test_rich_text_splits_long_text_on_whitespace():
    text = " ".join(["word"] * 1500)
    parts = notion._rich_text(text)
    assert all(len(p["text"]["content"]) <= notion.MAX_TEXT for p in parts)
    assert " ".join(p["text"]["content"] for p in parts) == text


def test_callout_with_many_images_respects_rich_text_limit():
    n = 150
    meta = {"description": "d", "date": "1856-01-24", "date_basis": "written",
            "images": list(range(1, n + 1)),
            "loc_urls": [f"https://www.loc.gov/resource/x/?sp={i}" for i in range(1, n + 1)]}
    blocks = notion.build_blocks(meta, "text\n")
    for b in blocks:
        assert len(b[b["type"]]["rich_text"]) <= notion.MAX_RICH
    assert blocks[0]["type"] == "callout" and blocks[1]["type"] == "paragraph"
    text = "".join(r["text"]["content"] for b in blocks[:-1] for r in b[b["type"]]["rich_text"])
    assert text.count("image ") == n


def test_build_blocks_callout_and_literal_markers():
    meta = {"description": "d", "date": "1862-09-17?", "date_basis": "written",
            "item": "https://www.loc.gov/item/mss123/", "images": [5],
            "loc_urls": ["https://www.loc.gov/resource/x/?sp=5"], "model": "m",
            "quality_notes": "Faded."}
    blocks = notion.build_blocks(meta, "[image 5]\nA ~~struck~~ and ^inserted^ word\n")
    assert blocks[0]["type"] == "callout"
    callout = "".join(r["text"]["content"] for r in blocks[0]["callout"]["rich_text"])
    assert "1862-09-17? (written)" in callout and "Faded." in callout
    assert blocks[2]["paragraph"]["rich_text"][0]["text"]["content"] == \
        "A ~~struck~~ and ^inserted^ word"


# --- push -----------------------------------------------------------------------------------

def test_push_creates_then_updates_in_place(tmp_path, fake_notion):
    wd = str(tmp_path / "mss123")
    make_docs(wd, [([5, 6], "1862-09-17", "Letter one", "[image 5]\nDear Sir,\n[image 6]\nYours"),
                   ([7], "1862-09?", "Letter two", "[image 7]\nText")])

    counts = notion.push(wd, DB_LINK, client=fake_notion)
    assert counts == {"created": 2, "updated": 0, "unchanged": 0, "failed": 0, "undated": 0}
    assert len(fake_notion.pages) == 2
    page = fake_notion.pages["page1"]
    # Only the title and date properties are ever written: the checkboxes stay untouched.
    assert set(page["properties"]) == {"Description", "Date"}
    assert page["properties"]["Date"] == {"date": {"start": "1862-09-17"}}
    assert page["properties"]["Description"]["title"][0]["text"]["content"] == "Letter one"
    assert page_text(fake_notion, "page1")[1:] == ["[image 5]", "Dear Sir,", "[image 6]", "Yours"]

    # Nothing changed -> nothing sent.
    before = len(fake_notion.calls)
    assert notion.push(wd, DB_LINK, client=fake_notion)["unchanged"] == 2
    assert not [c for c in fake_notion.calls[before:] if c[0] in ("POST", "PATCH", "DELETE")]

    # Edit one document -> that page is updated in place (no duplicate).
    make_docs(wd, [([5, 6], "1862-09-18", "Letter one", "[image 5]\nDear General,\n[image 6]\nYours"),
                   ([7], "1862-09?", "Letter two", "[image 7]\nText")])
    counts = notion.push(wd, DB_LINK, client=fake_notion)
    assert (counts["updated"], counts["unchanged"], counts["created"]) == (1, 1, 0)
    assert len(fake_notion.pages) == 2
    assert fake_notion.pages["page1"]["properties"]["Date"]["date"]["start"] == "1862-09-18"
    assert page_text(fake_notion, "page1")[2] == "Dear General,"
    assert len(fake_notion.pages["page1"]["children"]) == 5  # callout + 4, old body cleared

    state = json.load(open(os.path.join(wd, "notion.json")))
    assert set(state["ds1"]) == {"5", "7"}


def test_push_recreates_page_deleted_in_notion(tmp_path, fake_notion):
    wd = str(tmp_path / "mss123")
    make_docs(wd, [([5], "1862-09-17", "Letter", "[image 5]\nText")])
    notion.push(wd, DB_LINK, client=fake_notion)
    del fake_notion.pages["page1"]
    make_docs(wd, [([5], "1862-09-17", "Letter", "[image 5]\nText, edited")])
    counts = notion.push(wd, DB_LINK, client=fake_notion)
    assert counts["created"] == 1 and counts["failed"] == 0
    assert len(fake_notion.pages) == 1


def test_push_unknown_date_leaves_date_empty(tmp_path, fake_notion, capsys):
    wd = str(tmp_path / "mss123")
    make_docs(wd, [([5], "XXXX", "Undated", "[image 5]\nText")])
    counts = notion.push(wd, DB_LINK, client=fake_notion)
    assert counts["undated"] == 1 and counts["created"] == 1
    assert "Date" not in fake_notion.pages["page1"]["properties"]
    assert "can't be converted" in capsys.readouterr().out


def test_push_chunks_long_bodies(tmp_path, fake_notion):
    wd = str(tmp_path / "mss123")
    text = "\n\n".join(f"Paragraph {i}" for i in range(250))
    make_docs(wd, [([5], "1862-09-17", "Long", text)])
    notion.push(wd, DB_LINK, client=fake_notion)
    assert len(fake_notion.pages["page1"]["children"]) == 251  # the fake asserts <= 100 per call


def test_push_needs_matching_schema(tmp_path):
    from conftest import FakeNotion
    wd = str(tmp_path / "mss123")
    make_docs(wd, [([5], "1862-09-17", "Letter", "[image 5]\nText")])
    with pytest.raises(SystemExit, match="title property"):
        notion.push(wd, DB_LINK, client=FakeNotion({"Name": {"type": "rich_text"}}))


def test_push_without_documents(tmp_path, fake_notion):
    with pytest.raises(SystemExit, match="no documents"):
        notion.push(str(tmp_path / "empty"), DB_LINK, client=fake_notion)


def test_dry_run_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(notion.TOKEN_ENV, raising=False)  # no token needed to preview
    wd = str(tmp_path / "mss123")
    make_docs(wd, [([5], "1862-09-17/1862-09-19", "Letter", "[image 5]\nText")])
    counts = notion.push(wd, DB_LINK, dry_run=True)
    out = capsys.readouterr().out
    assert "would push" in out and "1862-09-17/1862-09-19" in out
    assert counts["created"] == 0
    assert not os.path.exists(os.path.join(wd, "notion.json"))


# --- CLI ------------------------------------------------------------------------------------

def test_require_token(monkeypatch):
    monkeypatch.delenv(notion.TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit, match=notion.TOKEN_ENV):
        notion.require_token()


def test_cli_notion_requires_database(capsys):
    with pytest.raises(SystemExit):
        cli.main(["notion", "mss123"])
    assert "--database" in capsys.readouterr().err


def test_cli_run_notion_requires_database(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "mss123", "--notion", "--auto"])
    assert "--database" in capsys.readouterr().err


def test_cli_rejects_bad_database(capsys):
    with pytest.raises(SystemExit):
        cli.main(["notion", "mss123", "--database", "not-a-link", "--dry-run"])
    assert "Notion database ID" in capsys.readouterr().err


def test_cli_notion_without_token_fails_early(monkeypatch):
    monkeypatch.delenv(notion.TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit, match=notion.TOKEN_ENV):
        cli.main(["notion", "mss123", "--database", DB_LINK])


def test_cli_notion_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(notion.TOKEN_ENV, raising=False)
    wd = str(tmp_path / "out")
    make_docs(wd, [([5], "1862-09-17", "Letter", "[image 5]\nText")])
    assert cli.main(["notion", "mss123", "--out", wd, "--database", DB_LINK, "--dry-run"]) == 0
    assert "would push" in capsys.readouterr().out
