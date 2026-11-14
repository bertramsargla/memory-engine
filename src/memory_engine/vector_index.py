"""Rebuildable encrypted vector projections. Inference runs outside transactions."""
import hashlib
from datetime import datetime,timezone
import heapq
import json
import time
import numpy as np


def migrate(db):
    db.execute('''CREATE TABLE embedding_spaces(id TEXT PRIMARY KEY,contract TEXT NOT NULL,dimensions INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE chunk_vectors(space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
        chunk_id INTEGER NOT NULL REFERENCES retrieval_chunks(id) ON DELETE CASCADE,input_hash TEXT NOT NULL,
        vector BLOB NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(space_id,chunk_id))''')
    db.execute('CREATE INDEX vectors_chunk ON chunk_vectors(chunk_id,space_id)')
    db.execute('PRAGMA user_version=8')


def fingerprint(row):
    return hashlib.sha256(json.dumps([row['revision_id'],row['start_byte'],row['end_byte'],row['recipe'],row['text']],ensure_ascii=False).encode()).hexdigest()


def index(memory,model,*,scope,limit=100):
    if type(limit) is not int or not 1<=limit<=10000:raise ValueError('Invalid indexing batch limit')
    started=time.perf_counter();ready=0;failed=0;discarded=0;errors=[]
    contract=json.dumps(model.contract,sort_keys=True)
    with memory.transaction(write=True) as db:
        memory._authorize(db,scope,None)
        old=db.execute('SELECT contract,dimensions FROM embedding_spaces WHERE id=?',(model.space_id,)).fetchone()
        if old and (old['contract']!=contract or old['dimensions']!=model.dimensions):raise ValueError('Incompatible vector space identity')
        db.execute('INSERT OR IGNORE INTO embedding_spaces VALUES (?,?,?)',(model.space_id,contract,model.dimensions))
        rows=[dict(r) for r in db.execute('''SELECT c.* FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id
            WHERE e.scope=? AND NOT EXISTS(SELECT 1 FROM chunk_vectors v WHERE v.chunk_id=c.id AND v.space_id=?)
            ORDER BY c.id LIMIT ?''',(scope,model.space_id,limit))]
    for row in rows:
        identity=fingerprint(row)
        try:
            vector=np.asarray(model.embed(row['text'],'passage'),dtype='<f4')
            if vector.shape!=(model.dimensions,) or not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector))-1)>.001:
                raise ValueError('Invalid embedding vector')
        except Exception as exc:
            errors.append({'chunk_id':row['id'],'code':'embedding_failed','error_type':type(exc).__name__})
            failed+=1;continue  # Original/lexical evidence already committed.
        with memory.transaction(write=True) as db:
            current=db.execute('''SELECT c.* FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id
                WHERE c.id=? AND e.scope=?''',(row['id'],scope)).fetchone()
            if not current or fingerprint(current)!=identity:discarded+=1;continue
            db.execute('INSERT OR REPLACE INTO chunk_vectors VALUES (?,?,?,?,?)',
                (model.space_id,row['id'],identity,vector.tobytes(),datetime.now(timezone.utc).isoformat()))
            ready+=1
    return {'space_id':model.space_id,'indexed':ready,'failed':failed,'discarded_changed_or_deleted':discarded,
            'seconds':time.perf_counter()-started,'input_count':len(rows),'errors':errors}


def candidates(db,query_vector,model,scope,*,limit=200,plan=None):
    """Exact cosine scan in bounded blocks; no ANN and no newest-history cutoff."""
    query=np.asarray(query_vector,dtype='<f4')
    if query.shape!=(model.dimensions,) or not np.isfinite(query).all():raise ValueError('Invalid query vector')
    space=db.execute('SELECT dimensions,contract FROM embedding_spaces WHERE id=?',(model.space_id,)).fetchone()
    if not space:return [],{'state':'missing_index','scanned':0}
    if space['dimensions']!=model.dimensions or space['contract']!=json.dumps(model.contract,sort_keys=True):raise ValueError('Vector contract mismatch')
    from . import temporal
    condition,args,_=temporal.predicate(plan) if plan else ('1',[],None)
    # Scope must drive the join; starting with every vector in the model space
    # decrypted unrelated scopes before rejecting them at the revision join.
    cursor=db.execute(f'''SELECT c.id,v.vector FROM eligible_events e CROSS JOIN retrieval_chunks c ON c.revision_id=e.id
        CROSS JOIN chunk_vectors v ON v.chunk_id=c.id WHERE v.space_id=? AND e.scope=? AND {condition}''',(model.space_id,scope,*args))
    best=[];scanned=0
    while batch:=cursor.fetchmany(256):
        vectors=np.stack([np.frombuffer(r['vector'],dtype='<f4') for r in batch])
        if vectors.shape[1]!=model.dimensions or not np.isfinite(vectors).all():raise ValueError('Corrupted stored vector')
        scores=vectors@query
        for row,score in zip(batch,scores):
            entry=(float(score),-row['id'])
            if len(best)<limit:heapq.heappush(best,entry)
            elif entry>best[0]:heapq.heapreplace(best,entry)
        scanned+=len(batch)
    from .retrieval import BASE
    result=[]
    for score,negative_id in sorted(best,reverse=True):
        row=db.execute(BASE+' WHERE c.id=? AND e.scope=?',(-negative_id,scope)).fetchone()
        if row:result.append({**dict(row),'dense_score':score})
    total=db.execute('SELECT count(*) FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id WHERE e.scope=? AND '+condition,(scope,*args)).fetchone()[0]
    return result,{'state':'ready' if scanned==total else 'partial','scanned':scanned,'missing':total-scanned,'space_id':model.space_id}
