"""Claude API plumbing: request building, cost estimates, response cache, sync + batch runs."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

from .loc_api import jpeg_size

# Prices in USD per million tokens (input, output). Batch API is 50% of these.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.00, 40.00),
}
IMAGE_MAX_EDGE = 2576        # images larger than this are downscaled by the API
BATCH_MAX_BYTES = 180 * 2**20  # API limit is 256 MB per batch; leave headroom
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def image_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def image_tokens(path: str) -> int:
    w, h = jpeg_size(path)
    scale = min(1.0, IMAGE_MAX_EDGE / max(w, h))
    return int((w * scale) * (h * scale) / 750) + 1


@dataclass
class Job:
    """One Messages API request whose answer is a JSON object matching `schema`."""
    name: str                 # human-readable, for logs
    model: str
    system: str
    content: list             # user content blocks (images + text)
    schema: dict
    max_tokens: int
    effort: str = "medium"
    image_paths: list = field(default_factory=list)  # for estimates only
    expected_output_tokens: int = 1000

    def params(self) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": self.content}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": self.schema},
            },
        }

    def key(self) -> str:
        if "_key" not in self.__dict__:
            blob = json.dumps(self.params(), sort_keys=True).encode()
            self.__dict__["_key"] = hashlib.sha256(blob).hexdigest()[:48]
        return self.__dict__["_key"]


# ---------- cost estimate ----------

def estimate(jobs: list[Job], batch: bool) -> tuple[float, int, int]:
    """Rough (usd, input_tokens, output_tokens) for the jobs."""
    usd = 0.0
    tin = tout = 0
    for j in jobs:
        i = sum(image_tokens(p) for p in j.image_paths)
        i += (len(j.system) + sum(len(b.get("text", "")) for b in j.content)) // 4
        o = j.expected_output_tokens
        pin, pout = PRICES.get(j.model, PRICES["claude-opus-5-5"])
        usd += (i * pin + o * pout) / 1e6
        tin += i
        tout += o
    if batch:
        usd *= 0.5
    return usd, tin, tout


def confirm_spend(jobs: list[Job], batch: bool, yes: bool, what: str) -> bool:
    if not jobs:
        return True
    usd, tin, tout = estimate(jobs, batch)
    models = ", ".join(sorted({j.model for j in jobs}))
    print(f"{what}: {len(jobs)} requests to {models}{' via Batch API (50% off)' if batch else ''}\n"
          f"  estimated ~{tin:,} input + ~{tout:,} output tokens ≈ ${usd:,.2f} "
          f"(rough; thinking tokens vary)")
    if yes:
        return True
    if not sys.stdin.isatty():
        print("not a terminal; pass --yes to proceed", file=sys.stderr)
        return False
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


# ---------- cache + state ----------

class Store:
    def __init__(self, workdir: str):
        self.cache_dir = os.path.join(workdir, "cache")
        self.state_path = os.path.join(workdir, "state.json")
        os.makedirs(self.cache_dir, exist_ok=True)

    def get(self, key: str) -> dict | None:
        p = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return None

    def put(self, key: str, record: dict) -> None:
        p = os.path.join(self.cache_dir, f"{key}.json")
        with open(p + ".part", "w") as f:
            json.dump(record, f, indent=1)
        os.replace(p + ".part", p)

    def state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return json.load(f)
        return {}

    def save_state(self, st: dict) -> None:
        with open(self.state_path + ".part", "w") as f:
            json.dump(st, f, indent=1)
        os.replace(self.state_path + ".part", self.state_path)


class ModelError(RuntimeError):
    pass


def _parse_message(msg) -> dict:
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise ModelError(f"model refused (category: {cat})")
    if msg.stop_reason == "max_tokens":
        raise ModelError("hit max_tokens before finishing; try smaller chunks")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if text is None:
        raise ModelError(f"no text in response (stop_reason={msg.stop_reason})")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelError(f"response was not valid JSON: {e}") from e
    data["_model"] = msg.model
    data["_usage"] = {"input": msg.usage.input_tokens, "output": msg.usage.output_tokens}
    return data


def require_api_key() -> None:
    """Stop with a plain message, before the cost prompt, if there's no key to call Claude with."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit("ANTHROPIC_API_KEY is not set; it's needed to call Claude for this "
                         "step.\n  export ANTHROPIC_API_KEY=sk-ant-...")


def _client():
    import anthropic
    return anthropic.Anthropic()


# ---------- runners ----------

def run_jobs(jobs: list[Job], workdir: str, batch: bool, client=None,
             poll_seconds: int = 30) -> dict[str, dict]:
    """Run jobs, using the cache. Returns {job.key(): result}. Failures are reported, not raised."""
    store = Store(workdir)
    results: dict[str, dict] = {}
    todo: list[Job] = []
    for j in jobs:
        hit = store.get(j.key())
        if hit is not None:
            results[j.key()] = hit
        else:
            todo.append(j)
    if len(results):
        print(f"  {len(results)} cached, {len(todo)} to run")
    if not todo:
        return results
    client = client or _client()
    if batch:
        results.update(_run_batch(todo, store, client, poll_seconds))
    else:
        results.update(_run_sync(todo, store, client))
    return results


def _run_sync(jobs: list[Job], store: Store, client) -> dict[str, dict]:
    out = {}
    for n, j in enumerate(jobs, 1):
        print(f"  [{n}/{len(jobs)}] {j.name}")
        try:
            # Streaming avoids HTTP timeouts on long transcriptions; server-side fallbacks
            # re-run the request on another model if a safety classifier declines it.
            with client.beta.messages.stream(**j.params(), betas=[FALLBACK_BETA],
                                             fallbacks="default") as stream:
                msg = stream.get_final_message()
            rec = _parse_message(msg)
        except ModelError as e:
            print(f"    failed: {e}", file=sys.stderr)
            continue
        store.put(j.key(), rec)
        out[j.key()] = rec
    return out


def _chunk_by_size(jobs: list[Job]) -> list[list[Job]]:
    chunks, cur, size = [], [], 0
    for j in jobs:
        s = len(json.dumps(j.params()))
        if cur and size + s > BATCH_MAX_BYTES:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(j)
        size += s
    if cur:
        chunks.append(cur)
    return chunks


def _run_batch(jobs: list[Job], store: Store, client, poll_seconds: int) -> dict[str, dict]:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    st = store.state()
    pending: dict[str, list[str]] = st.setdefault("batches", {})
    wanted = {j.key() for j in jobs}
    in_flight = {k for keys in pending.values() for k in keys}

    # Submit only what no open batch already covers (a re-run resumes instead of re-paying).
    new = [j for j in jobs if j.key() not in in_flight]
    for chunk in _chunk_by_size(new):
        reqs = [Request(custom_id=j.key(), params=MessageCreateParamsNonStreaming(**j.params()))
                for j in chunk]
        b = client.messages.batches.create(requests=reqs)
        pending[b.id] = [j.key() for j in chunk]
        store.save_state(st)
        print(f"  submitted batch {b.id} ({len(chunk)} requests)")

    out: dict[str, dict] = {}
    for bid in list(pending):
        if not wanted.intersection(pending[bid]):
            continue
        while True:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status == "ended":
                break
            rc = b.request_counts
            print(f"  batch {bid}: {rc.processing} processing, {rc.succeeded} done "
                  f"(safe to Ctrl-C; re-run to resume)")
            time.sleep(poll_seconds)
        for r in client.messages.batches.results(bid):
            if r.result.type == "succeeded":
                try:
                    rec = _parse_message(r.result.message)
                except ModelError as e:
                    print(f"  {r.custom_id[:12]}: {e}", file=sys.stderr)
                    continue
                store.put(r.custom_id, rec)
                out[r.custom_id] = rec
            else:
                print(f"  {r.custom_id[:12]}: {r.result.type} (will retry on next run)",
                      file=sys.stderr)
        del pending[bid]
        store.save_state(st)
    return out
