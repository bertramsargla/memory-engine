"""Minimal evidence selection and exact parent reconstruction, separate from ranking."""

RECIPE='sufficiency-support-and-neighbors-v1'


def select(response):
    rows=response['results'];state=response.get('sufficiency')
    if not state:return rows,[]
    support=set(state['supporting_chunk_ids'])
    if state['state'] in ('SUFFICIENT','AMBIGUOUS','CONFLICTING'):
        selected=[r for r in rows if r['chunk_id'] in support]
    else:
        # Related evidence may explain abstention, but it is not a context-filling
        # obligation. Keep up to two examples and preserve the insufficiency state.
        selected=rows[:2]
    chosen={r['chunk_id'] for r in selected}
    return selected,[{'chunk_id':r['chunk_id'],'reason':'not_selected_for_sufficiency_context'} for r in rows if r['chunk_id'] not in chosen]


def expand(db,chunk_id,scope,revision_id,*,max_bytes=2400):
    if type(chunk_id) is not int or type(max_bytes) is not int or not 1200<=max_bytes<=64000:
        raise ValueError('Invalid bounded parent expansion')
    row=db.execute('''SELECT c.*,r.content FROM retrieval_chunks c JOIN eligible_events r ON r.id=c.revision_id
        WHERE c.id=? AND c.revision_id=? AND r.scope=?''',(chunk_id,revision_id,scope)).fetchone()
    if row is None:raise KeyError('Chunk not eligible in scope')
    start,end=row['start_byte'],row['end_byte'];expanded=[];omitted=[]
    for cid in [row['previous_id'],row['next_id']]:
        if cid is None:continue
        neighbor=db.execute('SELECT start_byte,end_byte FROM retrieval_chunks WHERE id=? AND revision_id=?',(cid,row['revision_id'])).fetchone()
        if neighbor is None:continue
        a,b=min(start,neighbor['start_byte']),max(end,neighbor['end_byte'])
        if b-a>max_bytes:omitted.append({'chunk_id':cid,'reason':'expansion_byte_limit'});continue
        start,end=a,b;expanded.append(cid)
    raw=row['content'].encode('utf-8')
    return {'start_byte':start,'end_byte':end,'text':raw[start:end].decode('utf-8'),
            'citation':f"memory-engine://evidence/{row['revision_id']}#{start}:{end}",
            'expanded_chunk_ids':expanded,'expansion_omitted':omitted,'context_recipe':RECIPE}
