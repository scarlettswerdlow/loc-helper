# loc-helper

`loc` downloads the page images of a Library of Congress manuscript item. It then groups the images into documents, transcribes each document with Claude, and saves one text file per document. Each file starts with metadata: a short description, an EDTF date, the LOC image links, and notes on transcription quality.

## Install

```bash
pip install -e .
export ANTHROPIC_API_KEY=...   # needed for cluster/transcribe
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

The item can be given as a bare ID, a `loc.gov/item/...` URL, or a `loc.gov/resource/...` URL. Each stage can also be run on its own with `loc download`, `loc cluster` and `loc transcribe`. Run `loc <cmd> --help` for the options.

## How it works

| Stage | What happens | Default model |
|---|---|---|
| download | Saves `item.json` and full images (IIIF `pct:50`) to `images/`, low-res thumbnails (`pct:25`) to `thumbs/`, and `manifest.csv` | – |
| cluster | Sends overlapping windows of 12 thumbnails. Each image is classified as `start`, `continuation`, `envelope`, `docket`, `enclosure`, `blank`, `target` or `cover`. A `start` opens a new document. Envelopes, dockets and enclosures join the document before them. Blanks, targets and covers are skipped. Writes `documents.json` | `claude-sonnet-5-5` |
| transcribe | One request per document, sent with its full images. Documents longer than 10 pages are split into parts, and each part is shown the previous page for context. Writes `docs/*.txt`, `index.csv` and `skipped.csv` | `claude-opus-5-5` (`--effort medium`) |

The transcriptions are **diplomatic**:
- Original spelling and line breaks are kept.
- `[image N]` marks each page.
- `[illegible]` marks unreadable words and `[?word?]` uncertain readings.
- `~~struck~~` marks struck-through text and `^inserted^` interlined text.
- `[envelope]` and `[docket]` label those parts.

### Output file

`docs/007_1862-09-17_mcclellan-to-halleck-on-antietam.txt`:

```
---
description: McClellan to Halleck on Antietam
date: 1862-09-17
date_basis: written
item: https://www.loc.gov/item/mss318980007/
images: [117, 118, 119]
loc_urls:
- https://www.loc.gov/resource/mss31898.0007/?sp=117
- ...
model: claude-opus-5-5
quality_notes: Faded ink on p.2; signature partly obscured.
---
[image 117]
Head Qrs Army of the Potomac
...
```

Dates are EDTF:
- `1862-09` is a partial date.
- `1862-09-17?` is uncertain and `1862~` approximate.
- `1862-09-15/1862-09-17` is a range.
- `XXXX` means unknown. Undated files are named `_undated_`.

`date_basis` says whether the date was written on the document or inferred, and from what.

## Cost controls

- Before spending anything, both API stages print a rough cost estimate and ask for confirmation. Pass `--yes` to skip the prompt.
- Requests go through the **Message Batches API** by default, which costs half as much but can take up to an hour. Pass `--no-batch` for immediate requests at full price.
- Every response is cached in `cache/`, keyed on the exact request. Re-running after an edit to `documents.json` only pays for documents that changed.
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
```

## Tests

```bash
pip install -e '.[dev]'
pytest
```

The tests use a fake Anthropic client and need no network access or API key.
