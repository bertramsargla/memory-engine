"""Bounded local second-stage ranking; inference never holds a transaction."""
import math
import time

RECIPE='bounded-cross-encoder-exact-first-diverse-v1'


def apply(memory,response,query,*,scope,token,model,depth,limit):
    if type(depth) is not int or depth not in (20,50,100):raise ValueError('Reranker depth must be 20, 50 or 100')
    started=time.perf_counter();rows=response['results'];scores={};state='ready'
    if model is None:state='model_unavailable'
    else:
        for row in rows[:depth]:
            with memory.transaction() as db:
                memory._authorize(db,scope,token)
                exists=db.execute('SELECT 1 FROM eligible_events WHERE id=? AND scope=?',(row['id'],scope)).fetchone()
            if not exists:continue
            try:
                score=float(model.score(query,row['text']))
                if not math.isfinite(score):raise ValueError('Non-finite reranker output')
                scores[row['chunk_id']]=score
            except Exception:
                state='inference_failed';scores={};break
    # A deletion, sensitivity change or revocation during model work wins. Never
    # return a stale copy merely because it was eligible when inference started.
    with memory.transaction() as db:
        memory._authorize(db,scope,token)
        rows=[r for r in rows if db.execute('SELECT 1 FROM eligible_events WHERE id=? AND scope=?',(r['id'],scope)).fetchone()]
    if scores:
        for r in rows:r['reranker_score']=scores.get(r['chunk_id'])
        ordered=sorted(rows,key=lambda r:(not r['exact'],-r.get('occurrence_score',0),r['reranker_score'] is None,-(r['reranker_score'] or 0),r['chunk_id']))
        chosen=[];extra=[];sources=set()
        for r in ordered:
            if r['source_id'] in sources:extra.append(r)
            else:chosen.append(r);sources.add(r['source_id'])
        rows=chosen+extra;rows.sort(key=lambda r:not r['exact'])
    response['results']=rows[:limit]
    detail={'recipe':RECIPE,'state':state,'depth_cap':depth,'scored_count':len(scores),
            'model_space_id':getattr(model,'space_id',None),'milliseconds':(time.perf_counter()-started)*1000,
            'scores':[{'chunk_id':cid,'score':score} for cid,score in scores.items()]}
    response['reranking']=detail
    if 'trace' in response:
        response['trace']['reranking']=detail
        response['trace']['context']['selected_chunk_ids']=[r['chunk_id'] for r in response['results']]
        response['trace']['omitted']=[{'chunk_id':r['chunk_id'],'reason':'reranked_result_limit'} for r in rows[limit:]]
    return response
