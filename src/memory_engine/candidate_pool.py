"""Preserve retriever heads before a bounded reranker; scores remain unchanged."""
import hashlib

RECIPE='protected-retriever-heads5-v1'


def preserve(rows,groups,limit,*,head=5):
    pool=list(rows[:limit]);decisions=[]
    by_hash={hashlib.sha256(r['text'].encode()).hexdigest():r for r in rows}
    mandatory=[];seen=set()
    for name in ['dense','lexical','entity']:
        taken=0;local=set()
        for candidate in groups.get(name,[]):
            digest=hashlib.sha256(candidate['text'].encode()).hexdigest()
            if digest in local:continue
            local.add(digest);row=by_hash.get(digest)
            if row is None:continue
            taken+=1
            if row['chunk_id'] not in seen:mandatory.append((name,row));seen.add(row['chunk_id'])
            if taken>=head:break
    for name,row in mandatory:
        if any(r['chunk_id']==row['chunk_id'] for r in pool):continue
        victim=next((r for r in reversed(pool) if not r['exact'] and r['chunk_id'] not in seen),None)
        if len(pool)>=limit and victim is None:
            decisions.append({'chunk_id':row['chunk_id'],'reason':'head_reservation_exceeds_exact_budget','retriever':name});continue
        if victim:pool.remove(victim)
        pool.append(row)
        decisions.append({'chunk_id':row['chunk_id'],'reason':'preserve_retriever_head','retriever':name,
                          'replaced_chunk_id':victim['chunk_id'] if victim else None})
    position={r['chunk_id']:i for i,r in enumerate(rows)}
    pool.sort(key=lambda r:position[r['chunk_id']])
    return pool,decisions
