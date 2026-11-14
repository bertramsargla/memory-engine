from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import uuid

from .storage import EncryptedStore, statements
from . import layers, provenance, evidence, retrieval
from .query import parse_query
from . import chunking
from . import vector_index
from . import reranking
from . import answerability
from . import context_selection
from . import lineage_integrity
from . import embedding_jobs

DEFAULTS = {
    "capture_paused": False, "capture_all_apps": True, "allowed_apps": [], "ignored_apps": [], "retention_days": 30,
    "language": "auto", "speech_model": "small", "ollama_model": "qwen2.5:3b",
    "scope": "project:memory_engine", "dictation_hotkey": "Left Ctrl",
    "compose_hotkey": "Right Ctrl", "insert_transcript_hotkey": "Ctrl+Alt+Shift+V", "microphone": None,
    "terminal_dictation": True,
    "field_badge": True,
    "dictation_enabled": True,
    "dictation_popups": True,
    "live_dictation": False,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL,
 kind TEXT NOT NULL, title TEXT NOT NULL, app TEXT NOT NULL, locator TEXT NOT NULL,
 observed TEXT NOT NULL, last_seen TEXT NOT NULL, content TEXT NOT NULL,
 attribution TEXT NOT NULL, quality TEXT NOT NULL, UNIQUE(scope,source,revision)
);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, event TEXT NOT NULL,
 start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS tombstones (scope TEXT,source TEXT,PRIMARY KEY(scope,source));
CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY,scope TEXT,text TEXT,evidence TEXT,status TEXT,created TEXT,supersedes TEXT);
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY,name TEXT,hash TEXT UNIQUE,scopes TEXT,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
PRAGMA user_version=1;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_home() -> Path:
    return Path(os.environ.get("MEMORY_ENGINE_HOME", str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MemoryEngine")))


def validate_scope(scope: str) -> str:
    if not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*:[\w.-]{1,100}", scope):
        raise ValueError("Choose an exact scope, for example project:memory_engine.")
    return scope


def chunks(text: str, size: int = 1536):
    offset, part, count = 0, [], 0
    for char in text:
        length = len(char.encode("utf-8"))
        if count + length > size:
            yield offset, offset + count, "".join(part)
            offset += count
            part, count = [], 0
        part.append(char)
        count += length
    if part:
        yield offset, offset + count, "".join(part)


def validate_settings(changes):
    if not isinstance(changes, dict):
        raise ValueError("Settings changes must be an object.")
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError("Unknown setting.")
        if key == "scope":
            validate_scope(value)
        elif key == "retention_days":
            if type(value) is not int or not 1 <= value <= 3650:
                raise ValueError("Retention must be between 1 and 3650 days.")
        elif key in ("capture_paused", "capture_all_apps", "terminal_dictation", "field_badge", "dictation_enabled", "dictation_popups", "live_dictation") and type(value) is not bool:
            raise ValueError("Invalid capture setting.")
        elif key in ("allowed_apps", "ignored_apps"):
            if not isinstance(value, list) or any(not isinstance(a, str) or not re.fullmatch(r"[\w .+-]+\.exe", a, re.I) for a in value):
                raise ValueError("Enter executable names such as notepad.exe.")
        elif key == "language" and value not in ("auto", "en", "et"):
            raise ValueError("Unsupported language selection.")
        elif key == "speech_model" and value not in ("small", "small.en", "large-v3"):
            raise ValueError("Unsupported speech model.")


SCHEMA_VERSION = 10


def initialize_schema(db, *, target_version=SCHEMA_VERSION):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in range(SCHEMA_VERSION + 1):
        raise ValueError('Unsupported vault schema; existing data was not replaced.')
    if version == 0:
        statements(db, SCHEMA)
    if db.execute('PRAGMA user_version').fetchone()[0] == 1:
        layers.migrate(db)
    if db.execute('PRAGMA user_version').fetchone()[0] == 2:
        layers.migrate_v3(db)
    if db.execute('PRAGMA user_version').fetchone()[0] == 3:
        provenance.migrate(db)
    if db.execute('PRAGMA user_version').fetchone()[0] == 4:
        # Actual predicates/orderings in recent, fetch/erase, observe, layer_list
        # and reverse lineage erasure; unique(scope,stream,at) already exists.
        for sql in (
            'CREATE INDEX events_scope_observed ON events(scope,observed DESC)',
            'CREATE INDEX events_last_seen ON events(last_seen)',
            'CREATE INDEX chunks_event_start ON chunks(event,start_byte)',
            'CREATE INDEX observations_event ON observations(event)',
            'CREATE INDEX items_scope_layer_status_created ON memory_items(scope,layer,status,created DESC)',
            'CREATE INDEX items_event ON memory_items(event)',
            'CREATE INDEX wiki_evidence_event ON wiki_evidence(event,page)',
            'CREATE INDEX chronicle_event ON chronicle_entries(event)',
        ):
            db.execute(sql)
        # Requires SQLite >=3.42. Removes obsolete FTS terms as part of deletion.
        for table in ('search_index', 'layer_index'):
            db.execute(f"INSERT INTO {table}({table},rank) VALUES ('secure-delete',1)")
        db.execute('PRAGMA user_version=5')
    if target_version >= 6 and db.execute('PRAGMA user_version').fetchone()[0] == 5:
        evidence.migrate(db)
    if target_version >= 7 and db.execute('PRAGMA user_version').fetchone()[0] == 6:
        retrieval.migrate(db)
    if target_version >= 8 and db.execute('PRAGMA user_version').fetchone()[0] == 7:
        vector_index.migrate(db)
    if target_version >= 9 and db.execute('PRAGMA user_version').fetchone()[0] == 8:
        lineage_integrity.migrate(db)
    if target_version >= 10 and db.execute('PRAGMA user_version').fetchone()[0] == 9:
        embedding_jobs.migrate(db)
    if target_version >= 10 and db.execute('PRAGMA user_version').fetchone()[0] == 10:
        embedding_jobs.ensure_indexes(db)


class Memory(layers.LayerMemory):
    """Synchronous domain API backed by a long-lived encrypted store.

    Desktop callers dispatch this API from workers. CLI/MCP may call directly.
    Every transaction is a stable read snapshot or an atomic incremental write.
    """

    def __init__(self, home: Path | None = None, *, test_key: bytes | None = None, migrate_legacy=False, chunk_recipe='natural-v1:b1200:o160:m160'):
        self.chunk_recipe=chunk_recipe
        if chunk_recipe!='bytes1536-v1':chunking.parse_recipe(chunk_recipe)
        self.home = Path(home or default_home())
        self.store = EncryptedStore(self.home, test_key=test_key,
                                    initialize=initialize_schema, migrate_legacy=migrate_legacy)
        self.path, self.cipher = self.store.path, self.store.cipher
        self.lock = self.store.lock_path
        self._settings_lock = threading.Lock()
        with self.transaction(write=True) as db:
            if not db.execute("SELECT 1 FROM settings WHERE key='input_defaults_v2'").fetchone():
                db.execute("UPDATE settings SET value=? WHERE key='dictation_hotkey' AND value=?",
                           (json.dumps('Left Ctrl'), json.dumps('Ctrl+Alt+Space')))
                db.execute("INSERT INTO settings VALUES ('input_defaults_v2','true')")
            for key, value in DEFAULTS.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('capture_device_id', json.dumps(str(uuid.uuid4()))))
            initial = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM settings')}
        self._settings_cache = initial

    def transaction(self, *, write=False):
        return self.store.transaction(write=write)

    def _chunks(self, content, meta=None):
        return chunks(content) if self.chunk_recipe=='bytes1536-v1' else chunking.project(
            content,chunking.parse_recipe(self.chunk_recipe),(meta or {}).get('message_boundaries',()))

    def close(self):
        self.store.close()  # Wait for active read transactions before clearing RAM.
        cache=getattr(self,'_vector_cache',None)
        if cache is not None:cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _authorize(self, db, scope: str, token: str | None):
        validate_scope(scope)
        if token is None:  # Native UI owner; MCP refuses absent credentials separately.
            return
        row = db.execute("SELECT scopes FROM grants WHERE hash=? AND revoked=0",
                         (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None or scope not in json.loads(row[0]):
            raise PermissionError("This connection does not have access to that scope.")

    @property
    def cached_settings(self):
        # A UI hint only: authorization/capture commit must read current DB state.
        return json.loads(json.dumps(self._settings_cache))

    def settings(self, changes: dict | None = None) -> dict:
        if changes is not None:
            validate_settings(changes)
        # Serialize publication of the cache with settings reads/writes: a slow
        # reader must not overwrite the cache after a newer write has committed.
        with self._settings_lock:
            with self.transaction(write=changes is not None) as db:
                for key, value in (changes or {}).items():
                    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
                result = {r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM settings")}
            self._settings_cache = result
            return self.cached_settings

    def ingest(self, content: str, *, scope="project:memory_engine", source=None,
               kind="note", title="Untitled", app="MemoryEngine", locator="",
               attribution="user_provided", quality="complete", observed=None, context=None, evidence_metadata=None, _capture_guard=None) -> dict:
        validate_scope(scope)
        context = layers.clean_context(context)
        meta = evidence.clean_metadata(evidence_metadata)
        if context.get('completeness') and not meta.get('completeness'):
            meta['completeness'] = context['completeness']
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("Memory must contain 1 to 1,000,000 UTF-8 bytes.")
        if kind not in ("note", "capture", "dictation", "document", "reference"):
            raise ValueError("Unsupported source kind.")
        if any(not isinstance(v, str) or len(v) > 4096 for v in (title, app, locator, attribution, quality)):
            raise ValueError("Invalid source metadata.")
        if source is not None and (not isinstance(source, str) or len(source) > 4096):
            raise ValueError("Invalid source identifier.")
        source, identity_confidence = evidence.identity(scope, source, app, context, meta)
        revision = hashlib.sha256(content.encode()).hexdigest()
        now = utcnow()
        context['observation_basis'] = context['observation_basis'] or ('caller_supplied' if observed else 'ingest_received' if kind == 'capture' else 'save_time')
        if observed:
            parsed = datetime.fromisoformat(observed)
            if parsed.tzinfo is None:
                raise ValueError("Observation time needs a timezone.")
            observed = parsed.astimezone(timezone.utc).isoformat()
            context['observed_offset'] = context['observed_offset'] or parsed.strftime('%z')
        else:
            context['observed_offset'] = context['observed_offset'] or datetime.now().astimezone().strftime('%z')
        with self.transaction(write=True) as db:
            def check_capture():
                if _capture_guard is not None:
                    current = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM settings')}
                    if not _capture_guard(current):
                        raise ValueError('Capture eligibility changed before evidence committed.')
            check_capture()
            if not context['device_id']:
                device = db.execute("SELECT value FROM settings WHERE key='capture_device_id'").fetchone()
                context['device_id'] = json.loads(device[0]) if device else ''
            if not context['capture_method'] and kind != 'capture':
                context['capture_method'] = {'note': 'user_entry', 'document': 'text_import',
                    'dictation': 'reviewed_transcript', 'reference': 'reference_import'}[kind]
            if db.execute("SELECT 1 FROM tombstones WHERE scope=? AND source=?", (scope, source)).fetchone():
                raise ValueError("This source was deleted and is blocked from reimport.")
            source_id = evidence.ensure_source(db, scope, source, app=app, kind=kind, locator=locator,
                observed=observed or now, context=context, meta=meta, confidence=identity_confidence)
            old = db.execute("SELECT id FROM events WHERE scope=? AND source=? AND revision=?", (scope, source, revision)).fetchone()
            if old:
                db.execute("UPDATE revisions SET last_seen=max(last_seen,?) WHERE id=?", (observed or now, old[0]))
                layers.derive(db, old[0])
                layers.observe(db, old[0], context, observed or now)
                occurrence = provenance.record(db, old[0], context, observed or now, utcnow(), title=title, app=app, locator=locator,
                                               kind=kind, quality=quality, attribution=attribution)
                evidence.enrich_occurrence(db, occurrence, meta, recorded_at=now)
                check_capture()
                return {"id": old[0], "revision_id": old[0], "source_id": source_id, "duplicate": True, "occurrence_id": occurrence}
            event = str(uuid.uuid4())
            evidence.insert_revision(db, (
                event, scope, source, revision, kind, title, app, locator,
                observed or now, observed or now, content, attribution, quality), source_id, meta)
            previous = None
            for ordinal, (start, end, part) in enumerate(self._chunks(content,meta)):
                cursor = db.execute("INSERT INTO retrieval_chunks(revision_id,start_byte,end_byte,text,ordinal,previous_id,parent_id,recipe) VALUES (?,?,?,?,?,?,?,?)", (event, start, end, part, ordinal, previous, event,self.chunk_recipe))
                db.execute("INSERT INTO search_index(rowid,text) VALUES (?,?)", (cursor.lastrowid, part))
                retrieval.index_chunk(db,cursor.lastrowid,part)
                if previous is not None:
                    db.execute('UPDATE retrieval_chunks SET next_id=? WHERE id=?', (cursor.lastrowid, previous))
                previous = cursor.lastrowid
            layers.derive(db, event)
            layers.observe(db, event, context, observed or now)
            occurrence = provenance.record(db, event, context, observed or now, utcnow(), title=title, app=app, locator=locator,
                                           kind=kind, quality=quality, attribution=attribution)
            evidence.enrich_occurrence(db, occurrence, meta, recorded_at=now)
            check_capture()
            return {"id": event, "revision_id": event, "source_id": source_id, "duplicate": False, "occurrence_id": occurrence}

    def search(self, query: str, *, scope="project:memory_engine", token=None, limit=20, now=None, timezone=None, trace=False, debug=False,
               strategy='lexical',embedder=None,reranker=None,rerank_depth=20,protect_candidates=True,use_vector_cache=False) -> dict:
        if not isinstance(query, str) or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid search query or limit.")
        if timezone is None:
            from tzlocal import get_localzone_name
            timezone=get_localzone_name()
            if not timezone:raise ValueError('Local timezone unavailable; supply an explicit IANA timezone.')
        plan = parse_query(query, scope=scope, now=now, tz=timezone)
        if strategy not in ('lexical','dense','hybrid_rrf','hybrid_normalized'):raise ValueError('Unsupported retrieval strategy')
        if type(rerank_depth) is not int or rerank_depth not in (20,50,100):raise ValueError('Invalid reranker depth')
        if type(protect_candidates) is not bool:raise ValueError('Invalid candidate protection setting')
        if type(use_vector_cache) is not bool:raise ValueError('Explicit vector cache flag required')
        vector=None;semantic_state={'state':'not_requested'}
        if strategy!='lexical':
            with self.transaction() as db:self._authorize(db,scope,token)
            if embedder is None:semantic_state={'state':'model_unavailable'}
            else:
                try:vector=embedder.embed(plan.semantic_query,'query')
                except Exception as exc:semantic_state={'state':'query_embedding_failed','error_type':type(exc).__name__}
        with self.transaction() as db:
            self._authorize(db, scope, token)
            dense=None
            if vector is not None:
                try:
                    if use_vector_cache:
                        from .vector_cache import VectorCache
                        if getattr(self,'_vector_cache',None) is None:self._vector_cache=VectorCache()
                        dense,semantic_state=self._vector_cache.candidates(db,vector,embedder,scope,plan=plan)
                    else:dense,semantic_state=vector_index.candidates(db,vector,embedder,scope,plan=plan)
                except ValueError as exc:semantic_state={'state':'invalid_vector_projection','error_type':type(exc).__name__}
            response=retrieval.search(db,plan,max(limit,rerank_depth) if reranker else limit,trace=trace,debug=debug,strategy=strategy,
                                      dense=dense,semantic_state=semantic_state,reserve_heads=bool(reranker and protect_candidates))
        if reranker is not None:
            response=reranking.apply(self,response,plan.semantic_query,scope=scope,token=token,model=reranker,
                                     depth=rerank_depth,limit=limit)
        response['sufficiency']=answerability.assess(plan,response['results'],response['coverage'])
        if trace:response['trace']['sufficiency']={'evaluated':True,**response['sufficiency']}
        return response

    def index_embeddings(self,model,*,scope='project:memory_engine',limit=100):
        """Worker API: model inference never holds the store transaction."""
        return vector_index.index(self,model,scope=scope,limit=limit)

    def configure_embeddings(self, model, *, scope='project:memory_engine', token=None, enabled=True):
        return embedding_jobs.configure(self,model,scope=scope,token=token,enabled=enabled)

    def backfill_embeddings(self, subscription_id, *, limit=100):
        return embedding_jobs.backfill(self,subscription_id,limit=limit)

    def run_embedding_job(self, model, *, interactive_pending=False):
        return embedding_jobs.run_one(self,model,interactive_pending=interactive_pending)

    def embedding_job_status(self, *, scope='project:memory_engine', token=None):
        return embedding_jobs.status(self,scope=scope,token=token)

    def expand_chunk(self,chunk_id,*,revision_id,scope='project:memory_engine',token=None,max_bytes=2400):
        with self.transaction() as db:
            self._authorize(db,scope,token)
            return context_selection.expand(db,chunk_id,scope,revision_id,max_bytes=max_bytes)

    def recent(self, *, scope="project:memory_engine", token=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid limit.")
        with self.transaction() as db:
            self._authorize(db, scope, token)
            result = [dict(r) for r in db.execute("SELECT id,title,kind,app,observed,last_seen,quality,scope FROM eligible_events WHERE scope=? ORDER BY observed DESC LIMIT ?", (scope, limit))]
            for row in result:
                row['provenance'] = provenance.summary(db, row['id'])
            return result

    def read_evidence(self, query, references, *, scope='project:memory_engine', token=None,
                      now=None, timezone=None, debug=False,support_model=None,semantic_normalizer=None):
        """Read selected original evidence through the bounded v3 contract.

        Retrieval and reader quality remain separately measurable. This synchronous
        domain API belongs on the existing memory worker, never directly on Qt.
        No interpretation is persisted. A support model is used only when explicitly
        supplied; the default path has no model calls.
        """
        from .reader_api import read_evidence
        return read_evidence(self, query, references, scope=scope, token=token,
                             now=now, timezone=timezone, debug=debug,support_model=support_model,
                             semantic_normalizer=semantic_normalizer)

    def fetch(self, event_id: str, *, scope="project:memory_engine", token=None, start_byte=0, end_byte=None):
        with self.transaction() as db:
            self._authorize(db, scope, token)
            row = db.execute("SELECT * FROM eligible_events WHERE id=? AND scope=?", (event_id, scope)).fetchone()
            if row is None:
                raise KeyError("Evidence not found in this scope.")
            result = dict(row)
            raw = result.pop("content").encode("utf-8")
            if type(start_byte) is not int or not 0 <= start_byte <= len(raw):
                raise ValueError("Invalid UTF-8 start offset.")
            if end_byte is None:
                end_byte = min(len(raw), start_byte + 64000)
                while end_byte < len(raw) and raw[end_byte] & 0xC0 == 0x80:
                    end_byte -= 1
            if type(end_byte) is not int or not start_byte <= end_byte <= len(raw) or end_byte-start_byte > 64000:
                raise ValueError("Invalid UTF-8 end offset or page larger than 64KB.")
            try:
                result["content"] = raw[start_byte:end_byte].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Citation must end on a UTF-8 character boundary.") from exc
            result.update(start_byte=start_byte, end_byte=end_byte, total_bytes=len(raw),
                          next_start_byte=end_byte if end_byte < len(raw) else None)
            result["citation"] = f"memory-engine://evidence/{event_id}#{start_byte}:{end_byte}"
            result['provenance'] = provenance.summary(db, event_id)
            result['lineage'] = evidence.lineage(db, event_id)
            return result

    def rebuild_chunks(self, revision_id, *, scope='project:memory_engine'):
        """Explicit bounded projection replacement; original revision is unchanged."""
        with self.transaction(write=True) as db:
            self._authorize(db,scope,None)
            row=db.execute('SELECT content,message_boundaries FROM revisions WHERE id=? AND scope=?',(revision_id,scope)).fetchone()
            if row is None: raise KeyError('Evidence not found in this scope.')
            db.execute('DELETE FROM search_index WHERE rowid IN (SELECT id FROM retrieval_chunks WHERE revision_id=?)',(revision_id,))
            db.execute('DELETE FROM retrieval_chunks WHERE revision_id=?',(revision_id,))
            previous=None; count=0
            for ordinal,(start,end,text) in enumerate(self._chunks(row['content'],{'message_boundaries':json.loads(row['message_boundaries'])})):
                cursor=db.execute('''INSERT INTO retrieval_chunks(revision_id,start_byte,end_byte,text,ordinal,previous_id,parent_id,recipe)
                    VALUES (?,?,?,?,?,?,?,?)''',(revision_id,start,end,text,ordinal,previous,revision_id,self.chunk_recipe))
                cid=cursor.lastrowid
                db.execute('INSERT INTO search_index(rowid,text) VALUES (?,?)',(cid,text))
                retrieval.index_chunk(db,cid,text)
                if previous is not None: db.execute('UPDATE retrieval_chunks SET next_id=? WHERE id=?',(cid,previous))
                previous=cid; count+=1
            return {'revision_id':revision_id,'chunks':count,'recipe':self.chunk_recipe}

    def _erase(self, db, ids, *, keep_decisions=False):
        layers.erase(db, ids, keep_decisions=keep_decisions)
        for event in ids:
            db.execute('DELETE FROM occurrences WHERE revision_id=?', (event,))
            db.execute("DELETE FROM search_index WHERE rowid IN (SELECT id FROM chunks WHERE event=?)", (event,))
            db.execute("DELETE FROM retrieval_chunks WHERE revision_id=?", (event,))
            db.execute("DELETE FROM revisions WHERE id=?", (event,))
            # Claim lineage is a JSON list; use parsed IDs, never a substring match.
            for row in db.execute("SELECT id,evidence FROM claims").fetchall():
                if event in json.loads(row[1]):
                    db.execute("DELETE FROM claims WHERE id=?", (row[0],))
        db.execute('DELETE FROM sources WHERE id NOT IN (SELECT source_id FROM revisions)')
        # FTS5 secure-delete removes index entries transactionally. No internal
        # commit or whole-index rebuild; backups/old WAL frames are not erased.

    def delete_source(self, event_id: str, *, scope="project:memory_engine") -> int:
        with self.transaction(write=True) as db:
            row = db.execute("SELECT source FROM events WHERE id=? AND scope=?", (event_id, scope)).fetchone()
            if row is None:
                raise KeyError("Evidence not found.")
            ids = [r[0] for r in db.execute("SELECT id FROM events WHERE source=? AND scope=?", (row[0], scope))]
            db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?)", (scope, row[0]))
            self._erase(db, ids)
            return len(ids)

    def purge_expired(self):
        with self.transaction(write=True) as db:
            days = json.loads(db.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()[0])
            before = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
            # Owner-confirmed active context remains durable with its original
            # source. Explicit source deletion still erases every descendant.
            ids = [r[0] for r in db.execute("""SELECT id FROM events WHERE last_seen<? AND NOT EXISTS (
                SELECT 1 FROM interpretation_evidence support JOIN interpretations i ON i.id=support.interpretation_id
                WHERE i.review_state='confirmed' AND support.revision_id=events.id)""", (before,))]
            self._erase(db, ids, keep_decisions=True)
            return len(ids)

    def propose(self, text: str, evidence_ids: list[str], *, scope="project:memory_engine"):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000 or not evidence_ids:
            raise ValueError("A proposal needs text and original evidence.")
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            for eid in evidence_ids:
                if not db.execute("SELECT 1 FROM events WHERE id=? AND scope=?", (eid, scope)).fetchone():
                    raise ValueError("Evidence must belong to the same scope.")
            cid = str(uuid.uuid4())
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?)", (cid, scope, text, json.dumps(evidence_ids), "pending", utcnow(), ""))
            return cid

    def claims(self, *, scope="project:memory_engine", token=None):
        with self.transaction() as db:
            self._authorize(db, scope, token)
            return [dict(r) for r in db.execute("SELECT * FROM claims WHERE scope=? AND id IN (SELECT id FROM eligible_interpretations) ORDER BY created DESC LIMIT 100", (scope,))]

    def interpretation(self, value, supports, *, scope='project:memory_engine', subject_id=None,
                       predicate=None, owner='unknown', confidence=None, review_state='pending',
                       known_from=None, valid_from=None, valid_to=None, supersedes=None,
                       contradiction_state='unknown'):
        """Explicit owner/adapter interpretation, never automatic assertion of truth."""
        if not isinstance(value, str) or not value.strip() or len(value)>8000 or not supports:
            raise ValueError('An interpretation needs a value and original support.')
        if not isinstance(supports,list) or len(supports)>1000 or any(not isinstance(s,dict) for s in supports):
            raise ValueError('Support must be a bounded list of original spans.')
        if confidence is not None and (type(confidence) not in (int,float) or not 0<=confidence<=1):
            raise ValueError('Confidence must be unknown or a finite number between zero and one.')
        if owner not in ('unknown','user','other') or review_state not in ('pending','confirmed'):
            raise ValueError('Invalid review or ownership state.')
        if contradiction_state not in ('unknown','unresolved','resolved'):
            raise ValueError('Invalid contradiction state.')
        known_from = evidence.utc(known_from) or utcnow()
        valid_from, valid_to = evidence.utc(valid_from), evidence.utc(valid_to)
        with self.transaction(write=True) as db:
            self._authorize(db, scope, None)
            if supersedes:
                old = db.execute('SELECT * FROM interpretations WHERE id=? AND scope=?',(supersedes,scope)).fetchone()
                if not old or (old['known_from'] and known_from < old['known_from']):
                    raise ValueError('Supersession must reference an earlier same-scope interpretation.')
                db.execute('UPDATE interpretations SET known_to=? WHERE id=?',(known_from,supersedes))
            cid = str(uuid.uuid4())
            db.execute('''INSERT INTO interpretations(id,scope,type,subject_id,predicate,value,owner,confidence,
                review_state,known_from,valid_from,valid_to,supersedes,contradiction_state,recipe)
                VALUES (?,?,'explicit',?,?,?,?,?,?,?,?,?,?,?,'owner-assertion-v1')''',
                (cid,scope,subject_id,predicate,value,owner,confidence,review_state,known_from,valid_from,valid_to,supersedes,contradiction_state))
            for support in supports:
                rid = support['revision_id']
                row = db.execute('SELECT content FROM revisions WHERE id=? AND scope=?',(rid,scope)).fetchone()
                if row is None:
                    raise ValueError('Support must exist in the same scope.')
                raw = row['content'].encode('utf-8')
                start,end = support.get('start_byte',0),support.get('end_byte',len(raw))
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
                    raise ValueError('Invalid support span.')
                raw[start:end].decode('utf-8')
                db.execute('INSERT INTO interpretation_evidence VALUES (?,?,?,?)',(cid,rid,start,end))
            return cid

    def interpretations(self, *, scope='project:memory_engine', token=None, known_at=None, valid_at=None):
        known_at, valid_at = evidence.utc(known_at), evidence.utc(valid_at)
        with self.transaction() as db:
            self._authorize(db,scope,token)
            rows = db.execute('''SELECT * FROM eligible_interpretations WHERE scope=?
                AND (? IS NULL OR (known_from<=? AND (known_to IS NULL OR known_to>?)))
                AND (? IS NULL OR (valid_from<=? AND (valid_to IS NULL OR valid_to>?)))
                ORDER BY known_from DESC,id LIMIT 100''',
                (scope,known_at,known_at,known_at,valid_at,valid_at,valid_at)).fetchall()
            return [{**dict(r),'supports':[dict(s) for s in db.execute(
                'SELECT revision_id,start_byte,end_byte FROM interpretation_evidence WHERE interpretation_id=?',(r['id'],))]} for r in rows]

    def confirm(self, claim_id: str, replacement: str | None = None):
        with self.transaction(write=True) as db:
            row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                raise KeyError("Proposal not found.")
            if replacement is not None:
                if not replacement.strip() or len(replacement) > 8000:
                    raise ValueError("Invalid correction.")
                db.execute("UPDATE claims SET status='superseded' WHERE id=?", (claim_id,))
                new_id = str(uuid.uuid4())
                db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?)", (new_id, row['scope'], replacement, row['evidence'], 'confirmed', utcnow(), claim_id))
                return new_id
            db.execute("UPDATE claims SET status='confirmed' WHERE id=?", (claim_id,))
            return claim_id

    def grant(self, name: str, scopes: list[str]):
        if not name.strip() or not scopes:
            raise ValueError("Connection needs a name and scope.")
        for scope in scopes:
            validate_scope(scope)
        token, gid = secrets.token_urlsafe(32), str(uuid.uuid4())
        with self.transaction(write=True) as db:
            db.execute("INSERT INTO grants(id,name,hash,scopes) VALUES (?,?,?,?)", (gid, name, hashlib.sha256(token.encode()).hexdigest(), json.dumps(scopes)))
        return {"id": gid, "token": token}

    def grants(self):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT id,name,scopes,revoked FROM grants")]

    def revoke(self, grant_id: str):
        with self.transaction(write=True) as db:
            db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant_id,))

    def status(self, *, scope="project:memory_engine", token=None):
        with self.transaction() as db:
            self._authorize(db, scope, token)
            return {"events": db.execute("SELECT count(*) FROM events WHERE scope=?", (scope,)).fetchone()[0],
                    "scope": scope, "storage": "SQLCipher incremental SQLite / WAL / Windows DPAPI key",
                    "search": "typed exact/FTS5; optional local dense and bounded reranking", "schema": db.execute("PRAGMA user_version").fetchone()[0], "sync": "not implemented",
                    "organization": {
                        "processed": db.execute('SELECT count(*) FROM layer_processed p JOIN events e ON e.id=p.event WHERE e.scope=?', (scope,)).fetchone()[0],
                        "pending": db.execute('SELECT count(*) FROM events WHERE scope=? AND id NOT IN (SELECT event FROM layer_processed)', (scope,)).fetchone()[0],
                        "mode": "local extractive organization", "cloud_jobs": "not configured",
                        "summaries": "extractive previews generated on read; no model synthesis queue"},
                    "layers": "extractive wiki/Chronicle, sessions/time, pending loops/identity/writing/reminders",
                    "wiki_pages": db.execute('SELECT count(*) FROM wiki_pages WHERE scope=?', (scope,)).fetchone()[0],
                    "pending_items": db.execute("SELECT count(*) FROM memory_items WHERE scope=? AND status='pending'", (scope,)).fetchone()[0]}
