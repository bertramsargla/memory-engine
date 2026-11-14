"""Ingest a few notes, organise them into layers, and retrieve with citations."""
import os
import secrets
import tempfile
from pathlib import Path

from memory_engine.memory import Memory

SCOPE = "project:demo"

with tempfile.TemporaryDirectory() as home:
    # Windows uses DPAPI key custody automatically. Elsewhere, supply a 32-byte key
    # (keep it in the OS keychain in real use; it is random and throwaway here).
    key = None if os.name == "nt" else secrets.token_bytes(32)
    memory = Memory(Path(home), test_key=key)
    try:
        memory.ingest("Atlas launch moved to Friday. Mira owns the release checklist.",
                      scope=SCOPE, source="notes-1", title="Atlas standup")
        memory.ingest("Remember to send Mira the Atlas pricing sheet before Thursday.",
                      scope=SCOPE, source="notes-2", title="Todo")

        memory.organize(scope=SCOPE)  # process any queued organising work

        print("search:")
        for hit in memory.search("Atlas launch", scope=SCOPE)["results"][:3]:
            print(" ", hit["citation"], "|", hit.get("snippet") or hit.get("text", "")[:80])
        print("wiki pages:")
        for page in memory.layer_list("wiki", scope=SCOPE):
            print(" ", page["kind"], page["name"], "-", len(page["evidence"]), "sources")
        print("chronicle:")
        for period in memory.chronicle("day", scope=SCOPE):
            print(" ", period["period"], period["snapshots"], "snapshots")
    finally:
        memory.close()
