"""Bounded volatile cache of decrypted exact-search projections, never evidence.

An epoch includes connection-local writes, cross-connection commits and projection/
privacy/revocation generations. Eligibility and original row hydration still run in
the caller's authorized read transaction. No plaintext cache is written to disk.
"""
import json
import time
import threading
import numpy as np
from . import vector_index, temporal


class VectorCache:
    def __init__(self,max_bytes=64*1024*1024):
        if type(max_bytes) is not int or not 1024<=max_bytes<=64*1024*1024:
            raise ValueError('Vector RAM budget must be 1KB..64MB')
        self.max_bytes=max_bytes;self.key=None;self.ids=None;self.vectors=None
        self.lock=threading.RLock()

    def clear(self):
        with self.lock:self._clear()

    def _clear(self):
        if self.vectors is not None:self.vectors.fill(0)
        if self.ids is not None:self.ids.fill(0)
        self.key=self.ids=self.vectors=None

    def candidates(self,db,query_vector,model,scope,*,limit=200,plan=None):
        with self.lock:
            return self._candidates(db,query_vector,model,scope,limit=limit,plan=plan)

    def _candidates(self,db,query_vector,model,scope,*,limit=200,plan=None):
        started=time.perf_counter()
        query=np.asarray(query_vector,dtype='<f4')
        if query.shape!=(model.dimensions,) or not np.isfinite(query).all():
            raise ValueError('Invalid query vector')
        space=db.execute('SELECT dimensions,contract FROM embedding_spaces WHERE id=?',(model.space_id,)).fetchone()
        if not space:return [],{'state':'missing_index','scanned':0,'cache':'missing_space'}
        if space['dimensions']!=model.dimensions or space['contract']!=json.dumps(model.contract,sort_keys=True):
            raise ValueError('Vector contract mismatch')
        generation=db.execute('SELECT generation FROM projection_clock WHERE id=1').fetchone()[0]
        version=db.execute('PRAGMA data_version').fetchone()[0]
        key=(db,model.space_id,space['contract'],scope,generation,version,db.total_changes)
        hit=key==self.key
        if not hit:
            self.clear()
            count=db.execute('''SELECT count(*) FROM eligible_events e CROSS JOIN retrieval_chunks c ON c.revision_id=e.id
                CROSS JOIN chunk_vectors v ON v.chunk_id=c.id WHERE e.scope=? AND v.space_id=?''',(scope,model.space_id)).fetchone()[0]
            if count*(model.dimensions*4+8)>self.max_bytes:
                rows,state=vector_index.candidates(db,query,model,scope,limit=limit,plan=plan)
                return rows,{**state,'cache':'budget_bypass','cache_budget_bytes':self.max_bytes}
            ids=np.empty(count,dtype=np.int64);vectors=np.empty((count,model.dimensions),dtype='<f4')
            cursor=db.execute('''SELECT c.id,v.vector FROM eligible_events e CROSS JOIN retrieval_chunks c ON c.revision_id=e.id
                CROSS JOIN chunk_vectors v ON v.chunk_id=c.id WHERE e.scope=? AND v.space_id=? ORDER BY c.id''',(scope,model.space_id))
            for i,row in enumerate(cursor):
                vector=np.frombuffer(row['vector'],dtype='<f4')
                if vector.shape!=(model.dimensions,) or not np.isfinite(vector).all():
                    vectors.fill(0);raise ValueError('Corrupted stored vector')
                ids[i]=row['id'];vectors[i]=vector
            self.ids,self.vectors,self.key=ids,vectors,key
        loaded=time.perf_counter()
        condition,args,_=temporal.predicate(plan) if plan else ('1',[],None)
        # Temporal eligibility remains a SQL operation over current occurrence state.
        if condition=='1':
            indices=np.arange(len(self.ids))
        else:
            eligible=np.fromiter((r[0] for r in db.execute('''SELECT c.id FROM retrieval_chunks c
                JOIN eligible_events e ON e.id=c.revision_id WHERE e.scope=? AND '''+condition,(scope,*args))),dtype=np.int64)
            indices=np.flatnonzero(np.isin(self.ids,eligible))
        # einsum without optimization avoids an unbounded BLAS thread pool.
        # Scan all stored vectors; apply the temporal mask only to scores/IDs.
        all_scores=np.einsum('ij,j->i',self.vectors,query,optimize=False)
        scores=all_scores[indices]
        selected=indices[np.lexsort((self.ids[indices],-scores))[:limit]]
        scored=time.perf_counter()
        from .retrieval import BASE
        result=[]
        for i in selected:
            row=db.execute(BASE+' WHERE c.id=? AND e.scope=?',(int(self.ids[i]),scope)).fetchone()
            if row:result.append({**dict(row),'dense_score':float(all_scores[i])})
        total=db.execute('SELECT count(*) FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id WHERE e.scope=? AND '+condition,(scope,*args)).fetchone()[0]
        finished=time.perf_counter()
        return result,dict(state='ready' if len(indices)==total else 'partial',scanned=len(indices),missing=total-len(indices),
            space_id=model.space_id,cache='hit' if hit else 'rebuilt',cache_bytes=self.ids.nbytes+self.vectors.nbytes,
            cache_budget_bytes=self.max_bytes,generation=generation,materialize_ms=(loaded-started)*1000,
            filter_and_cosine_ms=(scored-loaded)*1000,hydrate_ms=(finished-scored)*1000)
