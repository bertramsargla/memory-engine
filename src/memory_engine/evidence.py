"""Normalized evidence and interpretation lineage. No ranking or model calls.

`events` and `chunks` are read compatibility views. Revision IDs retain old event
IDs, so memory-engine://evidence/<id>#<bytes> continues to address original bytes.
Metadata supplied here is an adapter assertion, never extracted from source prose.
"""
from datetime import datetime, timezone
import hashlib
import json
import uuid


UNKNOWN = 'unknown'
RECIPE = 'evidence-v2'
META_FIELDS = {
    'source_type', 'platform', 'native_id', 'account_id', 'workspace_id', 'thread_id',
    'document_id', 'identity_confidence', 'authored_at', 'known_at', 'author_id',
    'author_name', 'recipients', 'action_state', 'action_basis', 'origin_kind',
    'sensitivity', 'trust_class', 'completeness', 'truncated', 'confidence',
    'capture_recipe', 'normalization_recipe', 'message_boundaries',
}
ORIGINS = {'unknown', 'user_authored', 'third_party', 'web_document', 'generated'}


def utc(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError('Time must be an explicit timezone-qualified string.')
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('Time needs an explicit timezone.')
    return dt.astimezone(timezone.utc).isoformat()


def clean_metadata(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - META_FIELDS:
        raise ValueError('Unknown evidence metadata field.')
    value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if len(json.dumps(value).encode()) > 16000:
        raise ValueError('Evidence metadata is oversized.')
    for key in META_FIELDS - {'recipients', 'truncated', 'confidence', 'message_boundaries'}:
        if key in value and value[key] is not None and (not isinstance(value[key], str) or len(value[key]) > 2048):
            raise ValueError('Invalid evidence metadata string.')
    for key in ('authored_at', 'known_at'):
        if key in value:
            value[key] = utc(value[key])
    if value.get('origin_kind', UNKNOWN) not in ORIGINS:
        raise ValueError('Captured evidence cannot acquire configuration or instruction authority.')
    if value.get('trust_class', 'untrusted') != 'untrusted':
        raise ValueError('All captured/imported evidence is untrusted data.')
    if value.get('sensitivity', UNKNOWN) not in {'unknown', 'normal', 'private', 'secret'}:
        raise ValueError('Invalid sensitivity class.')
    if value.get('action_state', UNKNOWN) not in {'unknown', 'draft', 'sent', 'read', 'approved', 'failed'}:
        raise ValueError('Invalid action state.')
    if value.get('action_state') in {'sent', 'read', 'approved', 'failed'} and not value.get('action_basis'):
        raise ValueError('An action outcome requires adapter evidence, not visible text.')
    if 'truncated' in value and value['truncated'] is not None and type(value['truncated']) is not bool:
        raise ValueError('Truncation is true, false or unknown.')
    if 'recipients' in value and (not isinstance(value['recipients'], list) or any(not isinstance(x, str) for x in value['recipients'])):
        raise ValueError('Recipients must be explicit identifiers.')
    confidence = value.get('confidence', {})
    if not isinstance(confidence, dict) or any(type(x) not in (float, int) or not 0 <= x <= 1 for x in confidence.values()):
        raise ValueError('Confidence fields must be numbers between zero and one.')
    boundaries = value.get('message_boundaries', [])
    if not isinstance(boundaries, list) or any(type(x) is not int or x < 0 for x in boundaries) or boundaries != sorted(set(boundaries)):
        raise ValueError('Message boundaries must be ordered original UTF-8 offsets.')
    return value


def identity(scope, supplied, app, context, meta):
    native = meta.get('native_id') or meta.get('document_id') or context.get('document_id')
    if native:
        parts = [meta.get('platform') or app, meta.get('workspace_id'), meta.get('account_id'),
                 meta.get('source_type'), meta.get('thread_id'), native]
        key = 'native:' + hashlib.sha256(json.dumps(parts).encode()).hexdigest()
        confidence = meta.get('identity_confidence') or 'adapter_asserted'
    elif supplied and not supplied.startswith('window:'):
        key, confidence = supplied, 'caller_asserted'
    else:
        # A title/window is not a persistent document identity. Preserve uncertainty
        # rather than merging unrelated same-title browser/editor documents.
        key, confidence = 'observation:' + str(uuid.uuid4()), 'unknown'
    return key, confidence


def ensure_source(db, scope, key, *, app, kind, locator, observed, context, meta, confidence):
    found = db.execute('SELECT id FROM sources WHERE scope=? AND identity_key=?', (scope, key)).fetchone()
    if found:
        return found[0]
    sid = str(uuid.uuid4())
    db.execute('''INSERT INTO sources(id,scope,identity_key,source_type,platform,native_id,account_id,
        workspace_id,thread_id,document_id,canonical_locator,identity_confidence,first_observed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (sid, scope, key, meta.get('source_type') or kind,
        meta.get('platform') or app, meta.get('native_id'), meta.get('account_id'), meta.get('workspace_id'),
        meta.get('thread_id'), meta.get('document_id') or context.get('document_id') or None,
        context.get('url') or locator or None, confidence, observed))
    return sid


def insert_revision(db, values, source_id, meta):
    raw = values[10].encode('utf-8')
    db.execute('''INSERT INTO revisions(id,scope,source,revision,kind,title,app,locator,observed,
        last_seen,content,attribution,quality,source_id,content_hash,byte_length,capture_recipe,
        normalization_recipe,completeness,truncated,message_boundaries)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (*values, source_id, hashlib.sha256(raw).hexdigest(),
        len(raw), meta.get('capture_recipe') or 'caller-text-v1', meta.get('normalization_recipe') or 'identity-utf8-v1',
        meta.get('completeness') or UNKNOWN, meta.get('truncated'), json.dumps(meta.get('message_boundaries', []))))


def enrich_occurrence(db, oid, meta, *, recorded_at):
    recorded_at = db.execute('SELECT recorded_at FROM occurrences WHERE id=?',(oid,)).fetchone()[0]
    db.execute('''UPDATE occurrences SET authored_at=?,known_at=?,knowledge_basis=?,author_id=?,author_name=?,
        recipients=?,action_state=?,action_basis=?,origin_kind=?,sensitivity=?,trust_class='untrusted',
        completeness=?,confidence=?,account_id=?,workspace_id=?,thread_id=? WHERE id=?''',
        (meta.get('authored_at'), meta.get('known_at') or recorded_at, 'adapter_asserted' if meta.get('known_at') else 'system_recorded',
         meta.get('author_id'), meta.get('author_name'), json.dumps(meta.get('recipients')),
         meta.get('action_state') or UNKNOWN, meta.get('action_basis'), meta.get('origin_kind') or UNKNOWN,
         meta.get('sensitivity') or UNKNOWN, meta.get('completeness') or UNKNOWN,
         json.dumps(meta.get('confidence', {})), meta.get('account_id'), meta.get('workspace_id'), meta.get('thread_id'), oid))


def interpretation_triggers(db):
    # Existing review APIs remain writable compatibility projections. These
    # transaction-local triggers keep one common temporal/support contract.
    for table, family in [('claims', 'legacy'), ('memory_items', 'layer')]:
        value = 'NEW.text'
        kind = "'claim'" if family == 'legacy' else 'NEW.category'
        owner = "'unknown'" if family == 'legacy' else 'NEW.ownership'
        recipe = "'legacy-proposal-v1'" if family == 'legacy' else "'explicit-lines-v1'"
        support = ('''INSERT OR IGNORE INTO interpretation_evidence(interpretation_id,revision_id,start_byte,end_byte)
            SELECT NEW.id,r.id,0,r.byte_length FROM json_each(NEW.evidence) e JOIN revisions r ON r.id=e.value
            WHERE r.scope=NEW.scope;''' if family == 'legacy' else '''INSERT INTO interpretation_evidence
            SELECT NEW.id,r.id,NEW.start_byte,NEW.end_byte FROM revisions r WHERE r.id=NEW.event
            AND r.scope=NEW.scope AND NEW.start_byte>=0 AND NEW.end_byte<=r.byte_length;''')
        for action in ('INSERT', 'UPDATE'):
            missing = ('''EXISTS(SELECT 1 FROM json_each(NEW.evidence) e WHERE e.type!='text' OR NOT EXISTS(
                SELECT 1 FROM revisions r WHERE r.id=e.value AND r.scope=NEW.scope))''') if family=='legacy' else '0'
            db.execute(f'''CREATE TRIGGER {table}_normalize_{action.lower()} AFTER {action} ON {table} BEGIN
                INSERT INTO interpretations(id,scope,type,value,owner,review_state,known_from,supersedes,recipe,legacy_family)
                VALUES (NEW.id,NEW.scope,{kind},{value},{owner},NEW.status,NEW.created,nullif(NEW.supersedes,''),{recipe},'{family}')
                ON CONFLICT(id) DO UPDATE SET value=excluded.value,owner=excluded.owner,review_state=excluded.review_state,
                    supersedes=excluded.supersedes;
                DELETE FROM interpretation_evidence WHERE interpretation_id=NEW.id;
                {support}
                UPDATE interpretations SET support_state=CASE WHEN NOT EXISTS(SELECT 1 FROM interpretation_evidence
                    WHERE interpretation_id=NEW.id) THEN 'missing' WHEN {missing} THEN 'partial' ELSE 'supported' END WHERE id=NEW.id;
                UPDATE interpretations SET known_to=NEW.created WHERE id=nullif(NEW.supersedes,'');
                END''')
        db.execute(f'''CREATE TRIGGER {table}_normalize_delete AFTER DELETE ON {table} BEGIN
            DELETE FROM interpretations WHERE id=OLD.id; END''')


def migrate(db):
    """Schema 5 -> 6, atomic in the existing store transaction; no file rewrite."""
    db.execute('''CREATE TABLE sources(id TEXT PRIMARY KEY,scope TEXT NOT NULL,identity_key TEXT NOT NULL,
        source_type TEXT NOT NULL,platform TEXT,native_id TEXT,account_id TEXT,workspace_id TEXT,thread_id TEXT,
        document_id TEXT,canonical_locator TEXT,identity_confidence TEXT NOT NULL,first_observed_at TEXT,
        UNIQUE(scope,identity_key))''')
    db.execute('ALTER TABLE events RENAME TO revisions')
    for column in ('source_id TEXT REFERENCES sources(id)', 'content_hash TEXT', 'byte_length INTEGER',
                   'capture_recipe TEXT', 'normalization_recipe TEXT', 'completeness TEXT',
                   'truncated INTEGER CHECK(truncated IN (0,1))', "message_boundaries TEXT NOT NULL DEFAULT '[]'"):
        db.execute('ALTER TABLE revisions ADD COLUMN '+column)
    for row in db.execute('SELECT * FROM revisions ORDER BY observed,id'):
        sid = ensure_source(db,row['scope'],row['source'],app=row['app'],kind=row['kind'],locator=row['locator'],
                            observed=row['observed'],context={},meta={},confidence='legacy_unverified')
        raw = row['content'].encode('utf-8')
        db.execute('''UPDATE revisions SET source_id=?,content_hash=?,byte_length=?,capture_recipe='legacy-unknown',
            normalization_recipe='legacy-unknown',completeness='unknown' WHERE id=?''',
            (sid,hashlib.sha256(raw).hexdigest(),len(raw),row['id']))
    db.execute('CREATE INDEX revisions_source_time ON revisions(source_id,observed,id)')
    db.execute('''CREATE TRIGGER revision_source_guard BEFORE INSERT ON revisions BEGIN
        SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM sources WHERE id=NEW.source_id AND scope=NEW.scope AND identity_key=NEW.source)
            OR NEW.byte_length!=length(cast(NEW.content AS BLOB)) OR NEW.content_hash IS NULL
            THEN RAISE(ABORT,'Revision needs exact bytes and a same-scope source') END; END''')
    db.execute('''CREATE VIEW events AS SELECT id,scope,source,revision,kind,title,app,locator,observed,
        last_seen,content,attribution,quality FROM revisions''')
    db.execute('''CREATE TRIGGER revision_immutable BEFORE UPDATE OF id,source_id,content,content_hash,
        revision,byte_length,scope,source,capture_recipe,normalization_recipe,observed,completeness,truncated,message_boundaries
        ON revisions BEGIN SELECT RAISE(ABORT,'Original revision is immutable'); END''')
    db.execute('ALTER TABLE chunks RENAME TO retrieval_chunks')
    db.execute('ALTER TABLE retrieval_chunks RENAME COLUMN event TO revision_id')
    # Existing IDs/FTS rowids are intentionally preserved; the new columns describe
    # the old projection until an explicit recipe rebuild is requested.
    for column in ("recipe TEXT NOT NULL DEFAULT 'bytes1536-v1'", "lexical_version TEXT NOT NULL DEFAULT 'fts5-unicode61-v1'",
                   'embedding_version TEXT', 'context_version TEXT', 'ordinal INTEGER',
                   'previous_id INTEGER', 'next_id INTEGER', 'parent_id TEXT'):
        db.execute('ALTER TABLE retrieval_chunks ADD COLUMN '+column)
    db.execute('''UPDATE retrieval_chunks SET ordinal=(SELECT count(*) FROM retrieval_chunks c
        WHERE c.revision_id=retrieval_chunks.revision_id AND c.start_byte<retrieval_chunks.start_byte),
        previous_id=(SELECT id FROM retrieval_chunks c WHERE c.revision_id=retrieval_chunks.revision_id
            AND c.start_byte<retrieval_chunks.start_byte ORDER BY c.start_byte DESC LIMIT 1),
        next_id=(SELECT id FROM retrieval_chunks c WHERE c.revision_id=retrieval_chunks.revision_id
            AND c.start_byte>retrieval_chunks.start_byte ORDER BY c.start_byte LIMIT 1),parent_id=revision_id''')
    db.execute('CREATE VIEW chunks AS SELECT id,revision_id AS event,start_byte,end_byte,text FROM retrieval_chunks')
    db.execute('ALTER TABLE evidence_occurrences RENAME TO occurrences')
    db.execute('ALTER TABLE occurrences RENAME COLUMN event TO revision_id')
    for column in ('authored_at TEXT', 'known_at TEXT', "knowledge_basis TEXT NOT NULL DEFAULT 'unknown'",
        'author_id TEXT','author_name TEXT','recipients TEXT',"action_state TEXT NOT NULL DEFAULT 'unknown'",'action_basis TEXT',
        "origin_kind TEXT NOT NULL DEFAULT 'unknown'", "sensitivity TEXT NOT NULL DEFAULT 'unknown'",
        "trust_class TEXT NOT NULL DEFAULT 'untrusted' CHECK(trust_class='untrusted')",
        "completeness TEXT NOT NULL DEFAULT 'unknown'", "confidence TEXT NOT NULL DEFAULT '{}'",
        'account_id TEXT','workspace_id TEXT','thread_id TEXT'):
        db.execute('ALTER TABLE occurrences ADD COLUMN '+column)
    db.execute("UPDATE occurrences SET known_at=nullif(recorded_at,''),knowledge_basis=CASE WHEN recorded_at='' THEN 'unknown' ELSE 'system_recorded' END")
    db.execute('CREATE INDEX occurrences_known_revision ON occurrences(known_at,revision_id)')
    db.execute('CREATE INDEX occurrences_observed_revision ON occurrences(observed_at,revision_id)')
    db.execute('CREATE VIEW evidence_occurrences AS SELECT id,revision_id AS event,observed_at,recorded_at,metadata FROM occurrences')
    db.execute('''CREATE TABLE interpretations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,type TEXT NOT NULL,
        subject_id TEXT,predicate TEXT,value TEXT,owner TEXT NOT NULL DEFAULT 'unknown',confidence REAL,
        review_state TEXT NOT NULL DEFAULT 'pending',known_from TEXT,known_to TEXT,valid_from TEXT,valid_to TEXT,
        contradiction_state TEXT NOT NULL DEFAULT 'unknown',supersedes TEXT,recipe TEXT NOT NULL,
        retention_policy TEXT NOT NULL DEFAULT 'retain_confirmed_support',legacy_family TEXT,
        support_state TEXT NOT NULL DEFAULT 'supported',
        CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),
        CHECK(known_to IS NULL OR known_from IS NULL OR known_to>=known_from),
        CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from))''')
    db.execute('''CREATE TABLE interpretation_evidence(interpretation_id TEXT NOT NULL REFERENCES interpretations(id) ON DELETE CASCADE,
        revision_id TEXT NOT NULL REFERENCES revisions(id),start_byte INTEGER NOT NULL,end_byte INTEGER NOT NULL,
        PRIMARY KEY(interpretation_id,revision_id,start_byte,end_byte),CHECK(start_byte>=0 AND end_byte>=start_byte))''')
    db.execute('CREATE INDEX support_reverse ON interpretation_evidence(revision_id,interpretation_id)')
    db.execute('CREATE INDEX interpretations_scope_review_time ON interpretations(scope,review_state,known_from)')
    db.execute('''CREATE TRIGGER support_scope_span BEFORE INSERT ON interpretation_evidence BEGIN
        SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM revisions r JOIN interpretations i ON i.scope=r.scope
            WHERE r.id=NEW.revision_id AND i.id=NEW.interpretation_id AND NEW.end_byte<=r.byte_length)
            THEN RAISE(ABORT,'Support must be a same-scope original span') END; END''')
    interpretation_triggers(db)
    # Replay only relational projections, never inference or review transitions.
    db.execute('UPDATE claims SET status=status')
    db.execute('UPDATE memory_items SET status=status')
    db.execute('''CREATE TRIGGER revision_descendants BEFORE DELETE ON revisions BEGIN
        DELETE FROM interpretations WHERE id IN (SELECT interpretation_id FROM interpretation_evidence WHERE revision_id=OLD.id);
        DELETE FROM search_index WHERE rowid IN (SELECT id FROM retrieval_chunks WHERE revision_id=OLD.id);
        DELETE FROM retrieval_chunks WHERE revision_id=OLD.id;
        DELETE FROM occurrences WHERE revision_id=OLD.id;
        END''')
    for table in ('retrieval_chunks','occurrences'):
        for action in ('INSERT','UPDATE OF revision_id'):
            db.execute(f'''CREATE TRIGGER {table}_revision_guard_{action.split()[0].lower()} BEFORE {action} ON {table} BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM revisions WHERE id=NEW.revision_id)
                    THEN RAISE(ABORT,'Missing original revision') END; END''')
    for action in ('INSERT','UPDATE OF text,start_byte,end_byte'):
        db.execute(f'''CREATE TRIGGER chunk_span_guard_{action.split()[0].lower()} BEFORE {action} ON retrieval_chunks BEGIN
            SELECT CASE WHEN NEW.start_byte<0 OR NEW.end_byte<=NEW.start_byte OR NOT EXISTS(
                SELECT 1 FROM revisions WHERE id=NEW.revision_id AND NEW.end_byte<=byte_length
                AND substr(cast(content AS BLOB),NEW.start_byte+1,NEW.end_byte-NEW.start_byte)=cast(NEW.text AS BLOB))
                THEN RAISE(ABORT,'Chunk is not an exact original byte span') END; END''')
    db.execute('PRAGMA user_version=6')


def lineage(db, revision):
    row = db.execute('SELECT source_id,content_hash,capture_recipe,normalization_recipe,completeness,truncated FROM revisions WHERE id=?',(revision,)).fetchone()
    return {'revision_id': revision, **dict(row)}
