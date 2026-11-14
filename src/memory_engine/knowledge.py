from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil

from .memory import Memory, validate_scope


class Knowledge:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.root = memory.home / "knowledge"
        self.root.mkdir(exist_ok=True)

    def scope_root(self, scope):
        validate_scope(scope)
        # '%' is not allowed in a scope, so this encoding cannot collide with an
        # underscore-containing project name or another scope prefix.
        path = self.root / scope.replace(":", "%3A")
        path.mkdir(exist_ok=True)
        return path

    def seed(self, scope):
        root = self.scope_root(scope)
        bundled = Path(__file__).resolve().parent.parent / "knowledge"
        for source in bundled.rglob("*.md"):
            target = root / source.relative_to(bundled)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    def resolve(self, scope, name):
        root = self.scope_root(scope).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() != ".md":
            raise PermissionError("Only Markdown files within this scope's knowledge folder are accessible.")
        # Reject junctions and symlinks even when a link resolves back inside root.
        if any(p.is_symlink() or p.is_junction() for p in [root / name, *(root / name).parents] if p != root.parent):
            raise PermissionError("Linked paths are not supported in knowledge resources.")
        return target

    def list(self, scope):
        root = self.scope_root(scope)
        result = []
        for path in sorted(root.rglob("*.md")):
            name = path.relative_to(root).as_posix()
            try:
                self.resolve(scope, name)
                if path.stat().st_size <= 128000:
                    result.append({"path": name, "kind": "skill" if path.name == "SKILL.md" else "document"})
            except (PermissionError, OSError):
                continue
        return result[:500]

    def read(self, scope, name):
        path = self.resolve(scope, name)
        if path.stat().st_size > 128000:
            raise ValueError("Markdown resource exceeds 128KB.")
        return path.read_text(encoding="utf-8")

    def save(self, scope, name, content):
        if len(content.encode()) > 128000:
            raise ValueError("Markdown resource exceeds 128KB.")
        target = self.resolve(scope, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def search(self, scope, query):
        words = set(re.findall(r"\w+", query.lower()))
        results = []
        for doc in self.list(scope):
            text = self.read(scope, doc["path"])
            score = sum(word in (doc['path'] + " " + text).lower() for word in words)
            if score:
                results.append({**doc, "score": score, "excerpt": text[:400],
                    "revision": hashlib.sha256(text.encode()).hexdigest()})
        return sorted(results, key=lambda item: -item['score'])[:10]


ACTIVITIES = {
    "General": "general-writing",
    "Tweet": "write-tweet",
    "Email reply": "reply-email",
    "Message reply": "reply-message",
    "Coding agent": "coding-handoff",
}


def route_activity(app: str, title: str, explicit="Auto"):
    if explicit != "Auto":
        return explicit if explicit in ACTIVITIES else "General"
    text = title.lower()
    if any(word in text for word in ("gmail", "outlook")):
        return "Email reply"
    if any(word in text for word in ("twitter", " / x", "x.com")):
        return "Tweet"
    if any(word in text for word in ("messenger", "whatsapp", "slack", "discord")):
        return "Message reply"
    if any(word in text for word in ("claude code", "codex", "visual studio code")) or app.lower() in ("code.exe", "claude.exe"):
        return "Coding agent"
    return "General"
