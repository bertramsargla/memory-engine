"""Durable, explicitly configured projection work. No model loads during ingestion.

Transactions claim/commit one job; inference owns no SQLite transaction. A random
lease fences expired workers. Subscribers bind work to a space, scope and grant.
This module never starts a thread or downloads a model on import/open/ingest.
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
import numpy as np
from . import vector_index

RECIPE = 'durable-embedding-jobs-v1'
MAX_ATTEMPTS = 3


def ensure_indexes(db):
    # Privacy triggers filter jobs by revision; an unindexed lookup would make
    # capture cost grow linearly with the entire durable job history.
    db.execute('CREATE INDEX IF NOT EXISTS embedding_job_revision ON embedding_jobs(revision_id,state)')
    db.execute('CREATE INDEX IF NOT EXISTS embedding_subscription_grant ON embedding_subscriptions(grant_id,enabled)')
    db.execute('CREATE INDEX IF NOT EXISTS occurrences_author_id ON occurrences(author_id,revision_id)')


def migrate(db):
    db.execute('''CREATE TABLE projection_clock(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)''')
    db.execute('INSERT INTO projection_clock VALUES (1,0)')
    db.execute('''CREATE TABLE embedding_subscriptions(
        id TEXT PRIMARY KEY,scope TEXT NOT NULL,space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
        grant_id TEXT REFERENCES grants(id),enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
        generation INTEGER NOT NULL,backfill_cursor INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL)''')
    db.execute('CREATE INDEX embedding_subscription_scope ON embedding_subscriptions(scope,enabled)')
    db.execute('''CREATE TABLE embedding_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,subscription_id TEXT NOT NULL REFERENCES embedding_subscriptions(id),
        generation INTEGER NOT NULL,chunk_id INTEGER REFERENCES retrieval_chunks(id) ON DELETE SET NULL,
        revision_id TEXT,revision_hash TEXT NOT NULL,chunk_recipe TEXT NOT NULL,input_hash TEXT,
        deletion_generation INTEGER NOT NULL,priority INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL CHECK(state IN('PENDING','RUNNING','DONE','RETRY','FAILED','CANCELLED')),
        attempts INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_until REAL,retry_at REAL NOT NULL DEFAULT 0,
        error_class TEXT,created REAL NOT NULL,updated REAL NOT NULL,
        UNIQUE(subscription_id,generation,chunk_id))''')
    db.execute('CREATE INDEX embedding_job_ready ON embedding_jobs(state,retry_at,priority DESC,id)')
    db.execute('CREATE INDEX embedding_job_chunk ON embedding_jobs(chunk_id,state)')
    db.execute('CREATE INDEX embedding_job_subscription ON embedding_jobs(subscription_id,generation,state)')
    db.execute('''CREATE TABLE embedding_worker_lease(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, until REAL NOT NULL DEFAULT 0)''')
    db.execute('INSERT INTO embedding_worker_lease(id) VALUES(1)')
    # New projections are scheduled in the ingest transaction, without inference.
    db.execute('''CREATE TRIGGER embedding_chunk_insert AFTER INSERT ON retrieval_chunks BEGIN
        INSERT OR IGNORE INTO embedding_jobs(subscription_id,generation,chunk_id,revision_id,revision_hash,
            chunk_recipe,deletion_generation,priority,state,created,updated)
        SELECT s.id,s.generation,NEW.id,r.id,r.content_hash,NEW.recipe,
            (SELECT generation FROM projection_clock WHERE id=1),10,'PENDING',unixepoch(),unixepoch()
        FROM embedding_subscriptions s JOIN revisions r ON r.id=NEW.revision_id
        WHERE s.enabled=1 AND s.scope=r.scope;
        UPDATE projection_clock SET generation=generation+1 WHERE id=1;
        END''')
    db.execute('''CREATE TRIGGER embedding_chunk_delete BEFORE DELETE ON retrieval_chunks BEGIN
        UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,
            revision_id=NULL,error_class='evidence_deleted',updated=unixepoch() WHERE chunk_id=OLD.id;
        UPDATE projection_clock SET generation=generation+1 WHERE id=1;
        END''')
    db.execute('''CREATE TRIGGER embedding_chunk_update AFTER UPDATE OF text,start_byte,end_byte,recipe ON retrieval_chunks BEGIN
        UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,
            error_class='projection_changed',updated=unixepoch() WHERE chunk_id=NEW.id;
        DELETE FROM chunk_vectors WHERE chunk_id=NEW.id;
        UPDATE projection_clock SET generation=generation+1 WHERE id=1;
        END''')
    for operation in ('INSERT','UPDATE OF sensitivity','DELETE'):
        event=operation.split()[0].lower();alias='OLD' if event=='delete' else 'NEW'
        db.execute(f'''CREATE TRIGGER embedding_privacy_{event} AFTER {operation} ON occurrences BEGIN
            UPDATE projection_clock SET generation=generation+1 WHERE id=1;
            UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,
                error_class='privacy_ineligible',updated=unixepoch()
            WHERE revision_id={alias}.revision_id AND state IN('PENDING','RUNNING','RETRY')
            AND EXISTS(SELECT 1 FROM occurrences WHERE revision_id={alias}.revision_id AND sensitivity IN('private','secret'));
            END''')
    db.execute('''CREATE TRIGGER embedding_grant_update AFTER UPDATE OF revoked,scopes ON grants BEGIN
        UPDATE projection_clock SET generation=generation+1 WHERE id=1;
        UPDATE embedding_subscriptions SET enabled=0 WHERE grant_id=NEW.id;
        UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,
            error_class='grant_changed',updated=unixepoch()
            WHERE subscription_id IN(SELECT id FROM embedding_subscriptions WHERE grant_id=NEW.id)
            AND state IN('PENDING','RUNNING','RETRY');
        END''')
    for operation in ('INSERT','UPDATE','DELETE'):
        db.execute(f'''CREATE TRIGGER embedding_vector_{operation.lower()} AFTER {operation} ON chunk_vectors BEGIN
            UPDATE projection_clock SET generation=generation+1 WHERE id=1; END''')
    db.execute('PRAGMA user_version=10')
    ensure_indexes(db)


def _grant_ok(db, sub):
    if not sub['enabled']:
        return False
    if sub['grant_id'] is None:
        return True  # Explicit local owner configuration; not an LLM permission.
    grant=db.execute('SELECT scopes,revoked FROM grants WHERE id=?',(sub['grant_id'],)).fetchone()
    return bool(grant and not grant['revoked'] and sub['scope'] in json.loads(grant['scopes']))


def configure(memory,model,*,scope,token=None,enabled=True,now=None):
    if type(enabled) is not bool:
        raise ValueError('Explicit subscription state required')
    now=time.time() if now is None else now
    contract=json.dumps(model.contract,sort_keys=True,allow_nan=False)
    if type(model.dimensions) is not int or not 1<=model.dimensions<=8192 or len(contract)>64000:
        raise ValueError('Bounded embedding contract required')
    with memory.transaction(write=True) as db:
        memory._authorize(db,scope,token)
        grant_id=None
        if token is not None:
            grant_id=db.execute('SELECT id FROM grants WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0]
        key=hashlib.sha256(json.dumps([scope,model.space_id,grant_id]).encode()).hexdigest()
        old=db.execute('SELECT * FROM embedding_spaces WHERE id=?',(model.space_id,)).fetchone()
        if old and (old['contract']!=contract or old['dimensions']!=model.dimensions):
            raise ValueError('Incompatible vector space identity')
        db.execute('INSERT OR IGNORE INTO embedding_spaces VALUES (?,?,?)',(model.space_id,contract,model.dimensions))
        sub=db.execute('SELECT * FROM embedding_subscriptions WHERE id=?',(key,)).fetchone()
        if sub and bool(sub['enabled'])==enabled:
            return dict(sub)
        generation=sub['generation']+1 if sub else 1
        db.execute('''INSERT INTO embedding_subscriptions VALUES(?,?,?,?,?,?,0,?)
            ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,generation=excluded.generation,backfill_cursor=0''',
            (key,scope,model.space_id,grant_id,int(enabled),generation,now))
        db.execute('''UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,
            error_class='subscription_changed',updated=? WHERE subscription_id=? AND state IN('PENDING','RUNNING','RETRY')''',(now,key))
        return dict(db.execute('SELECT * FROM embedding_subscriptions WHERE id=?',(key,)).fetchone())


def backfill(memory,subscription_id,*,limit=100,now=None):
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('Backfill batch must be 1..1000')
    now=time.time() if now is None else now
    with memory.transaction(write=True) as db:
        sub=db.execute('SELECT * FROM embedding_subscriptions WHERE id=?',(subscription_id,)).fetchone()
        if not sub or not _grant_ok(db,sub):
            return dict(scheduled=0,state='disabled')
        rows=db.execute('''SELECT c.*,r.content_hash FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id
            JOIN revisions r ON r.id=c.revision_id WHERE e.scope=? AND c.id>? ORDER BY c.id LIMIT ?''',
            (sub['scope'],sub['backfill_cursor'],limit)).fetchall()
        generation=db.execute('SELECT generation FROM projection_clock WHERE id=1').fetchone()[0]
        count=0
        for row in rows:
            if not db.execute('SELECT 1 FROM chunk_vectors WHERE space_id=? AND chunk_id=?',(sub['space_id'],row['id'])).fetchone():
                count+=db.execute('''INSERT OR IGNORE INTO embedding_jobs(subscription_id,generation,chunk_id,revision_id,
                    revision_hash,chunk_recipe,input_hash,deletion_generation,state,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,'PENDING',?,?)''',
                    (sub['id'],sub['generation'],row['id'],row['revision_id'],row['content_hash'],row['recipe'],
                     vector_index.fingerprint(row),generation,now,now)).rowcount
        if rows:
            db.execute('UPDATE embedding_subscriptions SET backfill_cursor=? WHERE id=?',(rows[-1]['id'],sub['id']))
        return dict(scheduled=count,scanned=len(rows),state='more' if len(rows)==limit else 'caught_up')


def claim(memory,model,*,now=None,lease_seconds=120,interactive_pending=False):
    if interactive_pending:
        return None
    if not 1<=lease_seconds<=300:
        raise ValueError('Bounded lease required')
    now=time.time() if now is None else now
    with memory.transaction(write=True) as db:
        active=db.execute('SELECT * FROM embedding_worker_lease WHERE id=1').fetchone()
        if active['token'] and active['until']>now:
            return None
        db.execute('''UPDATE embedding_jobs SET state=CASE WHEN attempts>=? THEN 'FAILED' ELSE 'RETRY' END,
            error_class='lease_expired',lease_token=NULL,lease_until=NULL,retry_at=?,updated=?
            WHERE state='RUNNING' AND lease_until<=?''',(MAX_ATTEMPTS,now,now,now))
        jobs=db.execute('''SELECT j.* FROM embedding_jobs j JOIN embedding_subscriptions s ON s.id=j.subscription_id
            WHERE s.space_id=? AND j.state IN('PENDING','RETRY') AND j.retry_at<=?
            ORDER BY j.priority DESC,j.id LIMIT 100''',(model.space_id,now)).fetchall()
        for job in jobs:
            sub=db.execute('SELECT * FROM embedding_subscriptions WHERE id=?',(job['subscription_id'],)).fetchone()
            row=db.execute('''SELECT c.*,r.content_hash FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id
                JOIN revisions r ON r.id=c.revision_id WHERE c.id=? AND e.scope=?''',(job['chunk_id'],sub['scope'])).fetchone()
            if not _grant_ok(db,sub) or sub['generation']!=job['generation'] or not row or row['content_hash']!=job['revision_hash'] or row['recipe']!=job['chunk_recipe']:
                db.execute("UPDATE embedding_jobs SET state='CANCELLED',error_class='input_ineligible',updated=? WHERE id=?",(now,job['id']))
                continue
            space=db.execute('SELECT * FROM embedding_spaces WHERE id=?',(model.space_id,)).fetchone()
            if space['contract']!=json.dumps(model.contract,sort_keys=True) or space['dimensions']!=model.dimensions:
                raise ValueError('Worker model contract mismatch')
            lease=uuid.uuid4().hex;identity=vector_index.fingerprint(row)
            db.execute("UPDATE embedding_jobs SET state='RUNNING',attempts=attempts+1,lease_token=?,lease_until=?,input_hash=?,updated=? WHERE id=?",
                       (lease,now+lease_seconds,identity,now,job['id']))
            db.execute('UPDATE embedding_worker_lease SET token=?,until=? WHERE id=1',(lease,now+lease_seconds))
            return dict(job_id=job['id'],lease_token=lease,input_hash=identity,text=row['text'],space_id=model.space_id)
    return None


def finish(memory,work,model,*,vector=None,error_class=None,now=None):
    now=time.time() if now is None else now
    if error_class is not None and error_class not in ('model_unavailable','model_timeout','invalid_vector','model_error'):
        raise ValueError('Safe structured worker error required')
    if error_class is None:
        vector=np.asarray(vector,dtype='<f4')
        if vector.shape!=(model.dimensions,) or not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector))-1)>.001:
            error_class='invalid_vector'
    with memory.transaction(write=True) as db:
        job=db.execute('SELECT * FROM embedding_jobs WHERE id=?',(work['job_id'],)).fetchone()
        if not job or job['state']!='RUNNING' or job['lease_token']!=work['lease_token'] or job['lease_until']<=now:
            db.execute('UPDATE embedding_worker_lease SET token=NULL,until=0 WHERE id=1 AND token=?',(work['lease_token'],))
            return 'STALE'
        sub=db.execute('SELECT * FROM embedding_subscriptions WHERE id=?',(job['subscription_id'],)).fetchone()
        row=db.execute('''SELECT c.* FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id
            WHERE c.id=? AND e.scope=?''',(job['chunk_id'],sub['scope'])).fetchone()
        space=db.execute('SELECT * FROM embedding_spaces WHERE id=?',(sub['space_id'],)).fetchone()
        valid=(_grant_ok(db,sub) and sub['generation']==job['generation'] and row is not None
               and vector_index.fingerprint(row)==job['input_hash']==work['input_hash']
               and model.space_id==work['space_id']==sub['space_id']
               and space['contract']==json.dumps(model.contract,sort_keys=True) and space['dimensions']==model.dimensions)
        state='CANCELLED' if not valid else ('FAILED' if error_class=='invalid_vector' or job['attempts']>=MAX_ATTEMPTS else 'RETRY') if error_class else 'DONE'
        if state=='DONE':
            db.execute('INSERT OR REPLACE INTO chunk_vectors VALUES(?,?,?,?,?)',
                (model.space_id,job['chunk_id'],job['input_hash'],vector.tobytes(),datetime.fromtimestamp(now,timezone.utc).isoformat()))
        db.execute('''UPDATE embedding_jobs SET state=?,lease_token=NULL,lease_until=NULL,retry_at=?,error_class=?,updated=? WHERE id=?''',
            (state,now+min(300,5*2**job['attempts']),error_class if valid else 'input_ineligible',now,job['id']))
        db.execute('UPDATE embedding_worker_lease SET token=NULL,until=0 WHERE id=1 AND token=?',(work['lease_token'],))
        return state


def run_one(memory,model,*,interactive_pending=False):
    from .model_work_slot import background_slot
    with background_slot(memory.home) as acquired:
        if not acquired:return dict(state='BUSY',model_calls=0)
        return _run_one(memory,model,interactive_pending=interactive_pending)


def _run_one(memory,model,*,interactive_pending=False):
    work=claim(memory,model,interactive_pending=interactive_pending)
    if work is None:
        return dict(state='IDLE',model_calls=0)
    started=time.perf_counter()
    try:
        vector=model.embed(work['text'],'passage');error=None
    except TimeoutError:
        vector=None;error='model_timeout'
    except Exception:
        vector=None;error='model_error'
    return dict(state=finish(memory,work,model,vector=vector,error_class=error),
                model_calls=1,seconds=time.perf_counter()-started)


def cancel(memory,job_id,*,scope,token=None):
    with memory.transaction(write=True) as db:
        memory._authorize(db,scope,token)
        row=db.execute('''SELECT j.id FROM embedding_jobs j JOIN embedding_subscriptions s ON s.id=j.subscription_id
            WHERE j.id=? AND s.scope=?''',(job_id,scope)).fetchone()
        if not row:
            raise KeyError('Job not found in authorized scope')
        db.execute("UPDATE embedding_jobs SET state='CANCELLED',lease_token=NULL,lease_until=NULL,error_class='cancelled',updated=? WHERE id=? AND state IN('PENDING','RUNNING','RETRY')",(time.time(),job_id))


def status(memory,*,scope,token=None,now=None):
    now=time.time() if now is None else now
    with memory.transaction() as db:
        memory._authorize(db,scope,token)
        rows=db.execute('''SELECT j.state,count(*) AS n,min(j.created) AS oldest FROM embedding_jobs j
            JOIN embedding_subscriptions s ON s.id=j.subscription_id WHERE s.scope=? GROUP BY j.state''',(scope,)).fetchall()
        counts={r['state']:r['n'] for r in rows}
        oldest=min((r['oldest'] for r in rows if r['state'] in ('PENDING','RUNNING','RETRY')),default=None)
        return dict(counts=counts,oldest_pending_seconds=max(0,now-oldest) if oldest is not None else 0,
                    recipe=RECIPE,model_loaded_by_status=False)
