# loc-helper

`loc` downloads the page images of a Library of Congress manuscript item. It then groups the images into documents, transcribes each document with Claude, and saves one text file per document. Each file starts with metadata: a short description, an EDTF date, the LOC image links, and a one-sentence note on transcription quality.

## Install

```bash
pip install -e .
export API_KEY_FOR_LOC=...   # needed for cluster/transcribe
```

## Usage

```bash
# 1. Download a range of images and cluster them into documents. Stops for review.
loc run mss318980007 --start 117 --end 160

# 2. Check mss318980007/documents.json. Merge or split documents by editing their "images" lists.

# 3. Transcribe.
loc transcribe mss318980007

# Or do everything in one go:
loc run mss318980007 --start 117 --end 160 --auto --yes
```

The item can be given as a bare ID, a `loc.gov/item/...` URL, or a `loc.gov/resource/...` URL (looked up on loc.gov to find its item). Each stage can also be run on its own with `loc download`, `loc cluster` and `loc transcribe`. Run `loc <cmd> --help` for the options.

## How it works

| Stage | What happens | Default model |
|---|---|---|
| download | Saves `item.json` and full images (IIIF `pct:50`) to `images/`, low-res thumbnails (`pct:25`) to `thumbs/`, and `manifest.csv` | – |
| cluster | Sends overlapping windows of 12 thumbnails. Each image is classified as `start`, `continuation`, `envelope`, `docket`, `enclosure`, `blank`, `target` or `cover`. A `start` opens a new document. Envelopes, dockets and enclosures join the document before them. Blanks, targets and covers are skipped. Writes `documents.json` | `claude-sonnet-5-5` |
| transcribe | One request per document, sent with its full images. Documents longer than 10 pages are split into parts, and each part is shown the previous page for context. The parts' quality notes are merged into one sentence by a small text-only request, always sent immediately. Writes `docs/*.txt`, `index.csv` and `skipped.csv` | `claude-opus-5-5` (`--effort medium`); merge: `claude-haiku-5-5` |

The transcriptions are **diplomatic**:
- Original spelling and line breaks are kept.
- `[image N]` marks each page.
- `[illegible]` marks unreadable words and `[?word?]` uncertain readings.
- `~~struck~~` marks struck-through text and `^inserted^` interlined text.
- `[envelope]` and `[docket]` label those parts.

### Output file

`docs/012_1855-03-30_cooper-telegram-ordering-mcclellan-to-washington.txt` (an example written by hand from image 117 of `mss318980007`; real output will differ in detail):

```
---
description: Telegram from Adjutant General S. Cooper ordering Capt. McClellan to report to Washington
date: 1855-03-30
date_basis: written
item: https://www.loc.gov/item/mss318980007/
images: [117]
loc_urls:
- https://www.loc.gov/resource/mss31898.mss31898-003_0154_0463/?sp=117
model: claude-opus-5-5
quality_notes: Printed telegraph form with clear handwriting; the docket notation at lower left is partly uncertain.
---
[image 117]
Dated Washington March 30 1855
Rec'd, Philadelphia, Mar 30 1855, 6 o'clock, 15 min. P. M.
To Capt Geo B McClellan
1st Regiment Cavalry
Care Major Crossman
Q.M. — U.S.A.
Repair to this city without
delay & report to the
Adjutant General
S Cooper
Adjt Genl.
```

Dates are EDTF:
- `1862-09` is a partial date.
- `1862-09-17?` is uncertain and `1862~` approximate.
- `1862-09-15/1862-09-17` is a range.
- `XXXX` means unknown. Undated files are named `_undated_`.

`date_basis` says whether the date was written on the document or inferred, and from what.

`quality_notes` is one sentence: how far to trust the transcription, and the most important thing to check against the images. If the model returns more than one sentence, only the first is kept, with a warning.

## Notion

`loc notion` pushes every document in `docs/` to a Notion database, one page per document. `Description` and `Date` are filled in. No other property is ever written, so checkboxes you tick in Notion survive re-runs. The page body is a short callout (date and basis, LOC links, quality notes, model) followed by the transcription.

Setup, once:

1. Create an internal integration at <https://www.notion.so/profile/integrations> and copy its token.
2. Share the database with it (••• menu > Connections).
3. `export NOTION_API_KEY=...`

```bash
loc notion mss318980007 --database "https://www.notion.so/...?v=..."   # link or bare ID
loc notion mss318980007 --database <link> --dry-run                     # preview, no token needed

# or as the last step of a full run:
loc run mss318980007 --start 117 --end 160 --auto --yes --notion --database <link>
```

`--database` is required every time. The database needs a title property and a date property named `Date`. Notes:

- **Re-runs update in place.** Page IDs are saved in `notion.json`. Unchanged documents are skipped. Edited ones are updated, and their page body is replaced. A page you deleted in Notion is recreated.
- **Dates** are converted from EDTF: `1862-09` becomes 1862-09-01 and a range becomes a Notion date range. The `?`, `~` and `%` qualifiers can't be expressed in Notion, so the callout keeps the original EDTF. `XXXX` leaves `Date` empty, with a warning.
- **Line breaks.** The files keep the original line breaks. For Notion, wrapped lines are joined into paragraphs. `[image N]` and label lines such as `[envelope]` stay on their own, as do letterhead, dateline, salutation and closing lines. This is a heuristic, so check the first few pages.
- **`~~struck~~` and `^inserted^` markers** are written literally, not converted to Notion formatting.

## Cost controls

- Both API stages check for `API_KEY_FOR_LOC` first. Before spending anything, they print a rough cost estimate and ask for confirmation. Pass `--yes` to skip the prompt.
- Requests go through the **Message Batches API** by default, which costs half as much but can take up to an hour. Pass `--no-batch` for immediate requests at full price.
- Every response is cached in `cache/`, keyed on the exact request. Re-running after an edit to `documents.json` only pays for documents that changed. Changing a prompt changes every key, so after upgrading loc-helper a re-run of `loc transcribe` on an existing item pays again in full.
- Open batch IDs are kept in `state.json`, so Ctrl-C while waiting is safe. Run the same command again and it picks up the same batch instead of resubmitting.
- Synchronous requests use server-side refusal fallbacks (`fallbacks: "default"`). If a safety classifier declines a page, the request is retried on another model. That parameter isn't available on the Batch API. In both modes, a refused request is reported and left uncached, so it is retried on the next run.

## Working directory layout

```
<item_id>/
  item.json  manifest.csv  README.txt
  images/  thumbs/
  documents.json   # editable clustering plan
  docs/  index.csv  skipped.csv
  cache/  state.json
  notion.json      # Notion page IDs, written by `loc notion`
```

## Tests

```bash
pip install -e '.[dev]'
pytest
```

The tests use a fake Anthropic client and a fake Notion client, and need no network access or API key.
