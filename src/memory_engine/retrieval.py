"""Eligible exact/lexical candidate generation and bounded deterministic ranking.

Scores are ranking signals, never probabilities or statements of factual truth.
"""
import hashlib
import json
import re
import time
from .query import IDENTIFIERS,WORDS
from . import provenance
from . import retrieval_trace
from . import fusion
from . import temporal
from . import candidate_pool

RECIPE='lexical-v2.1'
CAP=200


def migrate(db):
    db.execute('''CREATE VIEW eligible_events AS SELECT * FROM events e WHERE NOT EXISTS(
        SELECT 1 FROM occurrences o WHERE o.revision_id=e.id AND o.sensitivity IN ('private','secret'))''')
    db.execute('''CREATE VIEW eligible_memory_items AS SELECT * FROM memory_items
        WHERE event IN (SELECT id FROM eligible_events)''')
    db.execute('''CREATE VIEW eligible_wiki_pages AS SELECT * FROM wiki_pages p WHERE NOT EXISTS(
        SELECT 1 FROM wiki_evidence w WHERE w.page=p.id AND w.event NOT IN (SELECT id FROM eligible_events))''')
    db.execute('''CREATE VIEW eligible_interpretations AS SELECT * FROM interpretations i WHERE support_state='supported'
        AND NOT EXISTS(SELECT 1 FROM interpretation_evidence s WHERE s.interpretation_id=i.id
            AND s.revision_id NOT IN (SELECT id FROM eligible_events))''')
    db.execute('CREATE VIEW eligible_observations AS SELECT * FROM observations WHERE event IN (SELECT id FROM eligible_events)')
    db.execute('CREATE VIEW eligible_chronicle_entries AS SELECT * FROM chronicle_entries WHERE event IN (SELECT id FROM eligible_events)')
    db.execute('CREATE VIEW eligible_wiki_evidence AS SELECT * FROM wiki_evidence WHERE event IN (SELECT id FROM eligible_events)')
    db.execute("CREATE VIRTUAL TABLE lexical_v2 USING fts5(text,tokenize='porter unicode61 remove_diacritics 2')")
    db.execute("INSERT INTO lexical_v2(lexical_v2,rank) VALUES ('secure-delete',1)")
    db.execute('''CREATE TABLE exact_identifiers(identifier TEXT NOT NULL,chunk_id INTEGER NOT NULL
        REFERENCES retrieval_chunks(id) ON DELETE CASCADE,PRIMARY KEY(identifier,chunk_id))''')
    db.execute('CREATE INDEX identifiers_chunk ON exact_identifiers(chunk_id)')
    db.execute('''CREATE TRIGGER lexical_chunk_delete BEFORE DELETE ON retrieval_chunks BEGIN
        DELETE FROM lexical_v2 WHERE rowid=OLD.id;DELETE FROM exact_identifiers WHERE chunk_id=OLD.id;END''')
    for row in db.execute('SELECT id,text FROM retrieval_chunks'):
        index_chunk(db,row['id'],row['text'])
    db.execute('CREATE INDEX occurrence_author ON occurrences(author_name COLLATE NOCASE,revision_id)')
    db.execute('PRAGMA user_version=7')


def index_chunk(db,cid,text):
    db.execute('INSERT INTO lexical_v2(rowid,text) VALUES (?,?)',(cid,text))
    db.executemany('INSERT OR IGNORE INTO exact_identifiers VALUES (?,?)',
                   [(m.group().casefold(),cid) for m in IDENTIFIERS.finditer(text)])
    db.execute("UPDATE retrieval_chunks SET lexical_version='fts5-unicode61-v1+porter-unicode61-v2' WHERE id=?",(cid,))


def quote(term):return '"'+term.replace('"','""')+'"'


COLUMNS='''e.id,e.title,e.app,e.kind,e.observed,e.scope,e.quality,
    c.id AS chunk_id,c.start_byte,c.end_byte,c.text,c.recipe,c.ordinal,c.previous_id,c.next_id,
    r.source_id,r.content_hash,r.completeness,r.truncated'''
BASE=f'''SELECT {COLUMNS}
    FROM retrieval_chunks c JOIN eligible_events e ON e.id=c.revision_id JOIN revisions r ON r.id=e.id'''


def candidates(db,plan,timings=None,*,exact_only=False):
    timings={} if timings is None else timings
    started=time.perf_counter()
    groups={'exact':[],'lexical':[],'entity':[]}
    condition,args,_=temporal.predicate(plan)
    for identifier in plan.exact_identifiers:
        rows=db.execute(BASE+f''' JOIN exact_identifiers i ON i.chunk_id=c.id
            WHERE i.identifier=? AND e.scope=? AND {condition} ORDER BY c.id LIMIT ?''',(identifier.casefold(),plan.scope,*args,CAP)).fetchall()
        groups['exact'] += [dict(r) for r in rows]
    timings['exact_ms']=(time.perf_counter()-started)*1000;started=time.perf_counter()
    if exact_only:
        timings.update(lexical_ms=0.0,entity_ms=0.0)
        return groups
    if plan.lexical_terms:
        expr=' OR '.join(quote(t) for t in plan.lexical_terms)
        # Retain the measured FTS-first loop order from Phase 1. Scope and privacy
        # are SQL predicates before the candidate limit, not post-model filters.
        sql=f'''SELECT e.id,e.title,e.app,e.kind,e.observed,e.scope,e.quality,
            c.id AS chunk_id,c.start_byte,c.end_byte,c.text,c.recipe,c.ordinal,c.previous_id,c.next_id,
            r.source_id,r.content_hash,r.completeness,r.truncated,bm25(lexical_v2) AS bm25
            FROM lexical_v2 CROSS JOIN retrieval_chunks c ON c.id=lexical_v2.rowid
            CROSS JOIN eligible_events e ON e.id=c.revision_id CROSS JOIN revisions r ON r.id=e.id
            WHERE lexical_v2 MATCH ? AND e.scope=? AND {condition} ORDER BY bm25,c.id LIMIT ?'''
        groups['lexical']=[dict(r) for r in db.execute(sql,(expr,plan.scope,*args,CAP))]
    timings['lexical_ms']=(time.perf_counter()-started)*1000;started=time.perf_counter()
    occurrence_clause,occurrence_args,_=temporal.occurrence_constraint(plan)
    for entity in [e for e in plan.entity_mentions if e not in plan.exact_identifiers][:8]:
        # The selective author index must drive the join. Starting with every
        # revision in scope made even a nonexistent name scan 100k history rows.
        # Author/account/time predicates refer to the same observation.
        rows=db.execute(f'''SELECT {COLUMNS} FROM (
            SELECT DISTINCT revision_id FROM occurrences o
            WHERE author_name=? COLLATE NOCASE AND {occurrence_clause}) found
            CROSS JOIN eligible_events e ON e.id=found.revision_id
            CROSS JOIN revisions r ON r.id=e.id
            CROSS JOIN retrieval_chunks c ON c.revision_id=e.id
            WHERE e.scope=? ORDER BY c.id LIMIT ?''',(entity,*occurrence_args,plan.scope,50)).fetchall()
        groups['entity'] += [dict(r) for r in rows]
    timings['entity_ms']=(time.perf_counter()-started)*1000
    return groups


def rank(db,plan,groups,*,limit,strategy='lexical',reserve_heads=False):
    union={};decisions=[]
    fused=fusion.scores(groups,'rrf' if strategy=='hybrid_rrf' else 'normalized') if strategy.startswith('hybrid_') else {}
    for retriever,rows in groups.items():
        for position,row in enumerate(rows):
            item=union.setdefault(row['chunk_id'],{**row,'retrievers':{},'exact':False})
            item['retrievers'][retriever]=position+1
            if retriever=='dense':item['dense_score']=row['dense_score']
            if retriever=='exact':item['exact']=True
    generic={'project','company','person','source','email','thread','document','case','reference','url'}
    anchors=[x.casefold() for x in plan.entity_mentions if x.casefold() not in generic and x not in plan.exact_identifiers]
    for item in union.values():
        item['provenance']=provenance.summary(db,item['id'])
        item['selected_occurrence'],item['temporal_basis']=temporal.select_occurrence(db,plan,item['id'])
        origin=item['selected_occurrence'] or {}
        item['speaker_candidate_ids']=origin.get('speaker_candidate_ids',[])
        item['temporal_constraint_verified']=bool(origin.get(item['temporal_basis']))
        if origin:
            item['observed']=origin['observed_at'];item['title']=origin.get('title',item['title']);item['app']=origin.get('app',item['app'])
        text=item['text'].casefold()
        metadata=' '.join(str(origin.get(k) or '') for k in ('author_name','account_id','thread_id'))
        searchable=text+' '+metadata.casefold()
        terms=[t.casefold() for t in plan.lexical_terms]
        coverage=sum(t in searchable for t in terms)/max(1,len(terms))
        anchor_hits=sum(bool(re.search(r'(?<!\w)'+re.escape(a)+r'(?!\w)',searchable)) for a in anchors)
        phrase_hits=sum(p.casefold() in text for p in plan.phrases)
        reviewed=db.execute('''SELECT count(*) FROM interpretation_evidence s JOIN eligible_interpretations i
            ON i.id=s.interpretation_id WHERE s.revision_id=? AND i.review_state='confirmed'
            AND (i.known_to IS NULL)''',(item['id'],)).fetchone()[0]>0
        item['content_score']=coverage+(.6*anchor_hits/max(1,len(anchors)))+phrase_hits+.25*reviewed
        item['occurrence_score']=0.0
        item['score']=item['content_score']+sum(1/(60+r) for r in item['retrievers'].values())
        if strategy=='dense':item['score']=item.get('dense_score',0)
        if strategy.startswith('hybrid_'):item['score']=fused.get(item['chunk_id'],0)
        if plan.temporal_mode in ('current','latest','known_at_time'):
            item['interpretations']=temporal.interpretation_state(db,plan,item['id'],plan.as_of)
            relevant=[i for i in item['interpretations'] if i.get('predicate') and i['predicate'].casefold() in terms
                      and (not anchors or (i.get('subject_id') or '').casefold() in anchors)]
            active=[i for i in relevant if i['active_in_knowledge_interval'] and
                    (plan.temporal_mode=='known_at_time' or i['valid_at_query_time'] is not False)]
            item['occurrence_score']=1.0 if active else -.5 if relevant else 0.0
            item['score']+=item['occurrence_score']
        item['reviewed_support']=reviewed
        item['citation']=f"memory-engine://evidence/{item['id']}#{item['start_byte']}:{item['end_byte']}"
    ordered=sorted(union.values(),key=lambda r:(not r['exact'],-r['score'],r['chunk_id']))
    # Distinct source first pass, then additional chunks. Repeat observations never
    # create candidates. Exact duplicate content has explicit alternative origins.
    selected=[];deferred=[];sources=set();hashes={}
    for item in ordered:
        digest=hashlib.sha256(item['text'].encode()).hexdigest()
        if digest in hashes:
            primary=hashes[digest]
            primary.setdefault('alternative_origins',[]).append({'id':item['id'],'source_id':item['source_id'],'citation':item['citation']})
            decisions.append({'chunk_id':item['chunk_id'],'reason':'identical_span','kept':primary['chunk_id']})
            continue
        hashes[digest]=item
        if item['source_id'] in sources:
            deferred.append(item);decisions.append({'chunk_id':item['chunk_id'],'reason':'source_diversity_deferred'})
        else:
            selected.append(item);sources.add(item['source_id'])
    selected+=deferred
    # Exact IDs retain priority even if one source has several matching spans.
    selected.sort(key=lambda r:not r['exact'])
    pool,reservations=candidate_pool.preserve(selected,groups,limit) if reserve_heads else (selected[:limit],[])
    kept={r['chunk_id'] for r in pool}
    return pool,{'candidate_count':len(union),'deduplication':decisions,
        'candidate_pool':{'recipe':candidate_pool.RECIPE,'protected':reserve_heads,'decisions':reservations},
        'omitted': [{'chunk_id':r['chunk_id'],'reason':'result_limit'} for r in selected if r['chunk_id'] not in kept],
        'ranked':ordered}


def search(db,plan,limit,*,trace=False,debug=False,strategy='lexical',dense=None,semantic_state='not_requested',reserve_heads=False):
    safe_plan=retrieval_trace.query_plan(plan,raw=debug) if trace else None
    timings={}
    # Dense-only previously paid for BM25/author candidates and discarded them.
    # Missing dense evidence still takes the normal lexical fallback path.
    groups=candidates(db,plan,timings,exact_only=True) if strategy=='dense' and dense else candidates(db,plan,timings)
    effective=strategy
    if strategy=='dense' and dense:
        groups={'exact':groups['exact'],'dense':dense}
    elif strategy=='dense':effective='lexical'
    elif strategy.startswith('hybrid_') and dense:groups['dense']=dense
    elif strategy.startswith('hybrid_'):effective='lexical'
    started=time.perf_counter()
    rows,details=rank(db,plan,groups,limit=limit,strategy=effective,reserve_heads=reserve_heads)
    timings['ranking_ms']=(time.perf_counter()-started)*1000
    result={'results':rows,'mode':effective,'ranking_recipe':fusion.RECIPE if effective.startswith('hybrid_') else RECIPE,'query_plan':plan.as_dict(),
            'coverage':{'candidate_limit_per_retriever':CAP,'candidate_count':details['candidate_count'],
                'semantic':semantic_state,'sensitivity_policy':'exclude_any_private_or_secret_occurrence',
                'bounded':any(len(v)>=CAP for v in groups.values())}}
    result['candidate_pool']=details['candidate_pool']
    if trace:
        result['trace']={'recipe':'retrieval-trace-v1','query_plan':safe_plan,
            'eligibility':{'scope_hash':retrieval_trace.digest(plan.scope),'grant_checked':True,
                'privacy_rule':'exclude_any_private_or_secret_occurrence','trust':'evidence_is_untrusted',
                'excluded_revision_ids':[r[0] for r in db.execute('SELECT id FROM events WHERE scope=? AND id NOT IN (SELECT id FROM eligible_events) LIMIT 200',(plan.scope,))]},
            'candidates':retrieval_trace.candidates(groups),'ranking':retrieval_trace.ranking(details['ranked']),
            'candidate_pool':details['candidate_pool'],
            'deduplication':details['deduplication'],'omitted':details['omitted'],
            'temporal':{'mode':plan.temporal_mode,'applied':bool(plan.temporal_interval or plan.account_hints or plan.temporal_mode in ('current','latest')),
                'recipe':temporal.RECIPE,'basis':temporal.occurrence_constraint(plan)[2],
                'selected_occurrence_ids':[r['selected_occurrence']['id'] for r in rows if r.get('selected_occurrence')]},'timings':timings,
            'context':{'state':'not_assembled','selected_chunk_ids':[r['chunk_id'] for r in rows]},
            'sufficiency':{'evaluated':False,'state':None,'reason_codes':['assessment_not_implemented']}}
        if debug:result['trace']['raw_candidate_text']=[r['text'] for r in details['ranked']]
    return result
