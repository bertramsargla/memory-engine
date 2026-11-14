"""Read-only, revocable, exactly scoped MCP over newline-delimited stdio."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import quote, unquote, urlparse

from . import __version__
from .crypto import atomic_write, protect
from .knowledge import Knowledge
from .memory import Memory


def tool(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}


STRING = {"type": "string"}
TOOLS = [
    tool("memory_search", "Find original evidence with exact citations. Literal all-term search; no semantic recall.", {"query": STRING}, ["query"]),
    tool("memory_recent", "List recent evidence within the granted project." , {}),
    tool("memory_fetch", "Read cited original UTF-8 text. Advance next_start_byte for long records.", {
        "event_id": STRING, "start_byte": {"type": "integer", "minimum": 0}, "end_byte": {"type": "integer", "minimum": 0}}, ["event_id"]),
    tool("memory_status", "Inspect scope and actual available memory capabilities.", {}),
    tool("memory_claims", "Read claims and their pending, confirmed or superseded status.", {}),
    tool("memory_layers", "Browse wiki, open_loops, identity, writing, reminders, sessions or time. Extracted candidates are unconfirmed.",
         {"layer": STRING, "query": STRING}, ["layer"]),
    tool("memory_layer_record", "Read a wiki page with source previews/related pages, or a candidate with its original quoted span.", {"ref": STRING}, ["ref"]),
    tool("memory_chronicle", "Read bounded extractive UTC Chronicle periods: hour, day, week, month, year. No generated factual synthesis.",
         {"level": STRING, "period": STRING}, ["level"]),
    tool("memory_snapshot_context", "Read origin metadata, per-occurrence timestamp/location history, field context and estimated foreground seconds. Advance history.next_before_id for older occurrences.",
         {"event_id": STRING, "before_id": {"type": "integer", "minimum": 1},
          "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["event_id"]),
    tool("memory_context", "Select lexically relevant wiki evidence and owner-confirmed items; excludes pending interpretations.", {"query": STRING}, ["query"]),
    tool("knowledge_search", "Find this project's Markdown skills, protocols and research documents.", {"query": STRING}, ["query"]),
    tool("knowledge_read", "Read one Markdown resource in this project's knowledge folder.", {"path": STRING}, ["path"]),
]


def create_connection(memory: Memory, scope: str, name="Claude Code"):
    grant = memory.grant(name, [scope])
    path = memory.home / "connections" / f"{grant['id']}.dpapi"
    path.parent.mkdir(exist_ok=True)
    atomic_write(path, protect(json.dumps({"token": grant["token"], "scope": scope}).encode()))
    python = str(Path(sys.executable).with_name("python.exe"))
    launcher = str(Path(__file__).resolve().parent.parent / "run.py")
    return {"grant_id": grant["id"], "mcpServers": {"memory-engine": {
        "command": python, "args": [launcher, "mcp", "--home", str(memory.home), "--credential", str(path)]}}}


class Server:
    def __init__(self, memory, token, scope):
        if not token:
            raise PermissionError("MCP requires a scoped connection credential.")
        self.memory, self.token, self.scope = memory, token, scope
        self.knowledge = Knowledge(memory)

    def authorize(self):
        self.memory.status(scope=self.scope, token=self.token)

    def call_tool(self, name, args):
        self.authorize()
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            raise ValueError("Unknown tool.")
        schema = spec["inputSchema"]
        if not isinstance(args, dict) or set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
            raise ValueError("Invalid tool arguments.")
        for key, value in args.items():
            expected = schema["properties"][key]["type"]
            if (expected == "string" and not isinstance(value, str)) or (expected == "integer" and type(value) is not int):
                raise ValueError("Invalid argument type.")
        common = {"scope": self.scope, "token": self.token}
        if name == "memory_search":
            return self.memory.search(args["query"], **common)
        if name == "memory_recent":
            return self.memory.recent(**common)
        if name == "memory_fetch":
            return self.memory.fetch(**args, **common)
        if name == "memory_status":
            return self.memory.status(**common)
        if name == "memory_claims":
            return self.memory.claims(**common)
        if name == 'memory_layers':
            return self.memory.layer_list(**args, **common)
        if name == 'memory_layer_record':
            return self.memory.layer_record(**args, **common)
        if name == 'memory_chronicle':
            return self.memory.chronicle(**args, **common)
        if name == 'memory_snapshot_context':
            return self.memory.snapshot_details(**args, **common)
        if name == 'memory_context':
            return self.memory.relevant_layers(**args, **common)
        if name == "knowledge_search":
            return self.knowledge.search(self.scope, args["query"])
        if name == "knowledge_read":
            return {"path": args["path"], "text": self.knowledge.read(self.scope, args["path"])}

    def dispatch(self, message):
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid request parameters.")
        self.authorize()
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25"
            return {"protocolVersion": version, "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "memory-engine", "version": __version__},
                    "instructions": "Use source citations. Captured statements are not confirmed facts. Scope is fixed by the connection grant."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            try:
                result = self.call_tool(params.get("name"), params.get("arguments", {}))
                return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
            except (ValueError, KeyError, PermissionError, OSError) as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if method == "resources/list":
            return {"resources": [{"uri": "memory-engine://knowledge/" + quote(d["path"], safe="/"),
                "name": d["path"], "mimeType": "text/markdown"} for d in self.knowledge.list(self.scope)]}
        if method == "resources/read":
            uri = params.get("uri", "")
            parsed = urlparse(uri)
            if parsed.scheme != "memory-engine" or parsed.netloc != "knowledge":
                raise ValueError("Unknown resource.")
            return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                "text": self.knowledge.read(self.scope, unquote(parsed.path.lstrip("/")))}]}
        raise ValueError("Method not supported.")


def serve(memory, credential):
    saved = json.loads(protect(Path(credential).read_bytes(), decrypt=True))
    server = Server(memory, saved["token"], saved["scope"])
    for raw in iter(lambda: sys.stdin.buffer.readline(1_000_001), b""):
        response_id = None
        try:
            if len(raw) > 1_000_000:
                raise ValueError("Request too large.")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC request.")
            response_id = message.get("id")
            if "id" not in message:
                continue
            result = server.dispatch(message)
            response = {"jsonrpc": "2.0", "id": response_id, "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": response_id, "error": {"code": -32600, "message": str(exc) if isinstance(exc, (ValueError, PermissionError)) else "Memory request failed."}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
