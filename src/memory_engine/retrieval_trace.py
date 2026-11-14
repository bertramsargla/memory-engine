"""In-memory query diagnostics. No file writes and no raw personal text by default."""
import hashlib
import os


def digest(value):return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def query_plan(plan,*,raw=False):
    if raw and os.environ.get('MEMORY_ENGINE_DEVELOPER_DIAGNOSTICS')!='1':
        raise PermissionError('Raw diagnostics require explicit developer mode.')
    data=plan.as_dict()
    if raw:return data
    for key in ['raw_text','semantic_query','scope','temporal_expression','sequence_anchor']:
        data[key+'_hash']=digest(data.pop(key))
    for key in ['lexical_terms','phrases','exact_identifiers','entity_mentions','account_hints']:
        data[key+'_hashes']=[digest(v) for v in data.pop(key)]
    return data


def candidates(groups):
    return {name:[{'chunk_id':r['chunk_id'],'revision_id':r['id'],'source_id':r['source_id'],
                   'rank':i+1,'bm25':r.get('bm25'),'dense_score':r.get('dense_score')}
                  for i,r in enumerate(rows)] for name,rows in groups.items()}


def ranking(rows):
    return [{'chunk_id':r['chunk_id'],'score':r['score'],'content_score':r['content_score'],
             'occurrence_score':r['occurrence_score'],'exact_identifier':r['exact'],
             'reviewed_support':r.get('reviewed_support',False)} for r in rows]
