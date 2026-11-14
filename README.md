# memory-engine

A local, evidence-first personal memory engine in Python. It stores captured text as immutable, encrypted evidence, organises it into browsable layers (wiki pages, chronicle, open loops and more), and retrieves it with exact byte-level citations.

Nothing here makes network calls. Organising is extractive: summaries quote retained text and never invent new facts.

## What's inside

```
text in ──> evidence (source → revision → occurrence)      evidence.py, provenance.py
              │   encrypted SQLCipher/WAL store              storage.py, crypto.py, memory.py
              ├─> chunks with UTF-8 byte offsets            chunking.py
              │     ├─> FTS5 / BM25 lexical index
              │     └─> optional local vector index         embeddings.py, vector_index.py, vector_cache.py
              │
              ├─> organising layers                         layers.py
              │     wiki pages · chronicle (hour→year) · open loops
              │     identity · writing · reminders · sessions · time
              │
              └─> retrieval                                 query.py, retrieval.py, candidate_pool.py,
                    query planning (time windows, scopes)   fusion.py, reranking.py, temporal.py
                    lexical + vector fusion, reranking      retrieval_trace.py
                    evidence reader / answerability         evidence_reader.py, reader_*.py, answerability.py
                    bounded context packets                 context_packet.py, context_selection.py
```

Design rules the code enforces:

- **Evidence is immutable.** A correction creates a new revision. Seeing the same text again records an occurrence instead of duplicating it.
- **Citations are exact.** Every result points to `memory-engine://evidence/<id>#<start_byte>:<end_byte>` in the original UTF-8 text.
- **Everything is scoped.** Reads and writes need an explicit scope such as `project:demo`. Scoped grants and tokens limit what a reader can see.
- **Interpretations aren't facts.** Extracted candidates stay *pending* until the owner reviews them. Unknown authors and unknown times stay unknown.
- **Deletion carries through.** Deleting a source removes its chunks, index entries and derived layer items, and leaves a tombstone so replayed data can't bring it back.
- **Redaction:** `privacy.redact` removes common credential, card, IBAN and SSN patterns before text is stored.

## Quickstart

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .            # add [embeddings] for the optional local ONNX vector path
python examples/quickstart.py
```

```python
from pathlib import Path
import secrets
from memory_engine.memory import Memory

memory = Memory(Path("./my-memory"), test_key=secrets.token_bytes(32))
memory.ingest("Atlas launch moved to Friday.", scope="project:demo", source="notes-1", title="Standup")
memory.search("Atlas launch", scope="project:demo")["results"]   # cited hits
memory.layer_list("wiki", scope="project:demo")                   # organised pages
memory.chronicle("day", scope="project:demo")                     # extractive timeline
memory.close()
```

### Key custody

On Windows the store keeps its database key protected with DPAPI automatically. On macOS and Linux you supply a 32-byte key yourself through the `test_key` argument (the name is a holdover from the original code). In real use, keep that key in the OS keychain, not in code or on disk next to the database.

### MCP

`memory_engine.mcp.Server` exposes read-only, scope-limited MCP tools over the store: `memory_search`, `memory_fetch`, `memory_layers`, `memory_chronicle`, `memory_context` and others. The stdio launcher helper (`create_connection`) still assumes the original Windows desktop launcher, so on other platforms wire `serve()` up yourself.

## Tests

```bash
pip install -e .
cd tests && PYTHONPATH=../src:. python -m unittest discover -s . -p 'test_*.py'
```

306 tests. Two DPAPI tests are skipped outside Windows. All fixtures are synthetic.

## Status

This is research code, pulled out of a larger desktop memory app. Capture, UI, voice and reply-generation code were left out. The storage, layer and retrieval parts are the mature ones. The `reader_*` evidence-reader stack is experimental: it is deliberately conservative and often answers `INSUFFICIENT`.
