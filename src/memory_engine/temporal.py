"""Occurrence eligibility and explicit knowledge-time selection, without inference."""
from datetime import datetime,timedelta
import re
from . import provenance

RECIPE='explicit-occurrence-time-v1'


def occurrence_constraint(plan,alias='o'):
    clauses=[];args=[];basis=None
    if plan.account_hints:
        clauses.append(alias+'.account_id IN ('+','.join('?' for _ in plan.account_hints)+')');args.extend(plan.account_hints)
    if plan.temporal_interval:
        start,end=plan.temporal_interval
        basis='known_at' if plan.temporal_mode=='known_at_time' else 'authored_at' if 'authored_at' in plan.requested_provenance or plan.intent=='compare_updates' else 'observed_at'
        field=alias+'.'+basis
        if plan.temporal_mode in ('before','known_at_time'):
            time_clause=field+'<?';args.append(end if plan.temporal_mode=='known_at_time' else start)
        elif plan.temporal_mode=='after':time_clause=field+'>=?';args.append(end)
        else:
            if plan.temporal_mode=='around':
                start=(datetime.fromisoformat(start)-timedelta(days=1)).isoformat()
                end=(datetime.fromisoformat(end)+timedelta(days=1)).isoformat()
            time_clause=field+'>=? AND '+field+'<?';args.extend([start,end])
        # An update described in captured prose may have a different event time.
        # Keep unknown event-time candidates explicitly uncertain, never substitute
        # their observation timestamp for when the described event happened.
        if plan.intent=='compare_updates':time_clause='('+time_clause+' OR '+field+' IS NULL)'
        clauses.append(time_clause)
    return ' AND '.join(clauses) or '1',args,basis


def predicate(plan,revision_alias='e'):
    clause,args,basis=occurrence_constraint(plan)
    if clause=='1':return '1',[],basis
    return f'EXISTS(SELECT 1 FROM occurrences o WHERE o.revision_id={revision_alias}.id AND {clause})',args,basis


def select_occurrence(db,plan,revision):
    clause,args,basis=occurrence_constraint(plan)
    # Source location defaults to the first observation. Latest/current and
    # knowledge-as-of requests select their own occurrence instead of inheriting it.
    descending=plan.temporal_mode in ('current','latest','known_at_time','before')
    field=basis or 'observed_at'
    order='DESC' if descending else 'ASC'
    if 'speaker' in plan.requested_provenance and plan.entity_mentions:
        marks=','.join('?' for _ in plan.entity_mentions)
        predicate=clause+f' AND author_name COLLATE NOCASE IN ({marks})'
        speaker=db.execute(f'''SELECT * FROM occurrences o WHERE revision_id=? AND {predicate}
            ORDER BY ({field} IS NULL),{field} {order},id {order} LIMIT 1''',(revision,*args,*plan.entity_mentions)).fetchone()
        if speaker:
            chosen=provenance.decode(speaker)
            chosen['speaker_candidate_ids']=[r[0] for r in db.execute(f'''SELECT DISTINCT author_id FROM occurrences o
                WHERE revision_id=? AND {predicate} AND author_id IS NOT NULL LIMIT 3''',(revision,*args,*plan.entity_mentions))]
            return chosen,basis or 'observed_at'
    if plan.intent=='verify_action':
        desired=next((state for pattern,state in [(r'\b(send|sent)\b','sent'),(r'\bread\b','read'),(r'\b(approve|approved)\b','approved')] if re.search(pattern,plan.raw_text,re.I)),None)
        verified=db.execute(f'''SELECT * FROM occurrences o WHERE revision_id=? AND {clause}
            AND action_state=? AND action_basis IS NOT NULL ORDER BY observed_at,id LIMIT 1''',(revision,*args,desired)).fetchone()
        if verified:return provenance.decode(verified),basis or 'observed_at'
    row=db.execute(f'''SELECT * FROM occurrences o WHERE revision_id=? AND {clause}
        ORDER BY ({field} IS NULL),{field} {order},id {order} LIMIT 1''',(revision,*args)).fetchone()
    return provenance.decode(row) if row else None,basis or 'observed_at'


def interpretation_state(db,plan,revision,as_of):
    rows=db.execute('''SELECT i.* FROM eligible_interpretations i JOIN interpretation_evidence s ON s.interpretation_id=i.id
        WHERE s.revision_id=? AND i.scope=? AND i.review_state IN ('confirmed','superseded')''',(revision,plan.scope)).fetchall()
    cutoff=plan.temporal_interval[1] if plan.temporal_mode=='known_at_time' and plan.temporal_interval else as_of
    result=[]
    for row in rows:
        r=dict(row)
        historical=plan.temporal_mode=='known_at_time'
        r['active_in_knowledge_interval']=bool(r['known_from'] and
            (r['known_from']<cutoff if historical else r['known_from']<=cutoff) and
            (r['known_to'] is None or (r['known_to']>=cutoff if historical else r['known_to']>cutoff)))
        if r['known_from'] and (r['known_from']>=cutoff if historical else r['known_from']>cutoff):continue
        # Validity is not silently assumed from knowledge time. Unknown valid time
        # remains visible; explicit intervals are independently evaluated.
        r['valid_at_query_time']=None if r['valid_from'] is None else r['valid_from']<=as_of and (r['valid_to'] is None or as_of<r['valid_to'])
        result.append(r)
    return result
