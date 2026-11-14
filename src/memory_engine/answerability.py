"""Conservative deterministic evidence sufficiency, distinct from relevance.

This component does not generate facts. SUFFICIENT means the supported operation
below can be performed using quoted evidence/explicit adapter metadata, not that
arbitrary statements in an observed document are true in the outside world.
"""
import re
from .query import WORDS,IDENTIFIERS

RECIPE='evidence-sufficiency-v1'


def result(state,reason,rows,*,missing=(),details=None):
    return {'state':state,'reason_codes':[reason],'recipe':RECIPE,
            'supporting_chunk_ids':[r['chunk_id'] for r in rows],
            'missing_provenance':list(missing),'details':details or {},
            'authority':'quoted_evidence_only'}


def assess(plan,rows,coverage):
    if not rows:
        reason='no_eligible_evidence' if coverage.get('eligible_revision_count')==0 else 'retrieval_found_no_support'
        return result('INSUFFICIENT',reason,[],details={'capture_absence_proven':False,
            'index_state':coverage.get('semantic'),'capture_coverage':'unmeasured'})
    if 'historical_time_missing' in plan.ambiguity:
        return result('AMBIGUOUS','historical_time_needs_clarification',rows)
    terms=[x.casefold() for x in plan.lexical_terms]
    def origin(r):return r.get('selected_occurrence') or (r.get('provenance') or {}).get('first') or {}
    # Exact identifier existence is a deterministic source-location invariant.
    if plan.exact_identifiers and plan.intent in ('locate_source','retrieve_exact_identifier'):
        exact=[r for r in rows if r.get('exact')]
        found={identifier.casefold() for r in exact for identifier in IDENTIFIERS.findall(r['text'])}
        if {x.casefold() for x in plan.exact_identifiers}<=found:return result('SUFFICIENT','exact_identifier_observed',exact)
        return result('INSUFFICIENT','exact_identifier_not_observed',[])
    topics=[t for t in terms if t not in {'send','sent','read','approve','approved','authored','write','wrote','account','when'}]
    related=[r for r in rows if all(t in (r['text']+' '+str(origin(r).get('author_name') or '')+' '+str(origin(r).get('account_id') or '')).casefold() for t in topics)]
    if topics and not related:return result('INSUFFICIENT','requested_relation_not_established',rows)
    if related:rows=related
    if plan.intent=='compare_updates' and plan.temporal_interval and any(not r.get('temporal_constraint_verified') for r in rows):
        return result('INSUFFICIENT','underlying_event_time_unknown',rows,missing=('authored_or_valid_time',))
    if plan.intent=='verify_action':
        low=plan.raw_text.casefold()
        desired='sent' if re.search(r'\b(send|sent)\b',low) else 'read' if re.search(r'\bread\b',low) else 'approved' if re.search(r'\b(approve|approved)\b',low) else None
        objects=[t for t in terms if t not in {'send','sent','read','approve','approved','promise','promised'}]
        verified=[r for r in rows if desired and origin(r).get('action_state')==desired and origin(r).get('outcome_basis')
                  and objects and all(t in r['text'].casefold() for t in objects)]
        if verified:return result('SUFFICIENT','adapter_verified_action',verified)
        return result('INSUFFICIENT','action_outcome_unverified',rows,missing=('action_outcome',))
    if 'speaker' in plan.requested_provenance:
        mentions={x.casefold() for x in plan.entity_mentions}
        attributed=[r for r in rows if origin(r).get('author_name') and origin(r)['author_name'].casefold() in mentions]
        if not attributed:return result('INSUFFICIENT','speaker_not_established',rows,missing=('author',))
        identities={origin(r).get('author_id') or ('unresolved:'+r['source_id']) for r in attributed}
        identities.update(identity for r in attributed for identity in r.get('speaker_candidate_ids',[]))
        if len(identities)>1:return result('AMBIGUOUS','same_name_multiple_identities',attributed)
        rows=attributed
    if 'authored_at' in plan.requested_provenance:
        authored=[r for r in rows if origin(r).get('authored_at')]
        if not authored:return result('INSUFFICIENT','authored_time_unknown',rows,missing=('authored_at',))
        return result('SUFFICIENT','adapter_authored_time',authored)
    if plan.temporal_mode=='sequence':
        return result('INSUFFICIENT','sequence_anchor_not_established',rows,missing=('anchor_occurrence',))
    # Structured, reviewed interpretation support is stronger than text similarity.
    interpretations=[(r,i) for r in rows for i in r.get('interpretations',[])
                     if i.get('active_in_knowledge_interval') and i.get('review_state')=='confirmed'
                     and (i.get('predicate') or '').casefold() in terms
                     and (not plan.entity_mentions or (i.get('subject_id') or '').casefold() in {x.casefold() for x in plan.entity_mentions})
                     and (plan.temporal_mode=='known_at_time' or i.get('valid_at_query_time') is not False)]
    by_key={}
    for r,i in interpretations:
        if i.get('subject_id') and i.get('predicate'):
            by_key.setdefault((i['subject_id'],i['predicate']),[]).append((r,i))
    for pairs in by_key.values():
        if len({i['value'] for _,i in pairs})>1:
            return result('CONFLICTING','reviewed_interpretations_disagree',[r for r,_ in pairs])
    if by_key and plan.intent in ('current_state','historical_state'):
        return result('SUFFICIENT','reviewed_temporal_interpretation',[r for pairs in by_key.values() for r,_ in pairs])
    # An intentionally small, versioned reading grammar detects disagreement; it
    # never writes a claim or converts a source's assertion into a verified event.
    slots={}
    for row in rows:
        for m in re.finditer(r'\b(deadline|budget|launch)\s*(?::|is|will be)\s*([^.!?\n,]+)',row['text'],re.I):
            if m[1].casefold() in terms:slots.setdefault(m[1].casefold(),[]).append((m[2].strip().casefold(),row))
    for values in slots.values():
        if len({value for value,_ in values})>1:return result('CONFLICTING','observed_values_disagree',[r for _,r in values])
    content=' '.join(r['text'] for r in rows).casefold()
    # Unsupported relation requests must not pass merely because a company/person
    # name occurs. This rule is intentionally conservative for paraphrase/languages.
    relation_terms=[t for t in terms if t not in {x.casefold() for x in plan.entity_mentions}]
    relation_terms=[t for t in relation_terms if t not in {'person','company','source','project','account','work','personal'}]
    missing=[t for t in relation_terms if t not in content]
    if missing:
        return result('INSUFFICIENT','requested_relation_not_established',rows,details={'missing_term_count':len(missing)})
    anchors=[x.casefold() for x in plan.entity_mentions]
    if anchors and not all(a in content or any(origin(r).get('author_name','') and a==origin(r)['author_name'].casefold() for r in rows) for a in anchors):
        return result('INSUFFICIENT','requested_entity_not_established',rows)
    if not terms or (not any(t in content for t in terms) and 'speaker' not in plan.requested_provenance):
        return result('INSUFFICIENT','semantic_relatedness_is_not_support',rows)
    if plan.intent!='locate_source' and any(r.get('completeness') in ('partial','truncated') for r in rows):
        return result('INSUFFICIENT','capture_incomplete',rows,missing=('complete_evidence',))
    if re.search(r'\b(unknown|not known|not established|unverified)\b',content):
        return result('INSUFFICIENT','source_explicitly_preserves_uncertainty',rows)
    if plan.intent=='locate_source' or slots or 'speaker' in plan.requested_provenance:
        return result('SUFFICIENT','quoted_evidence_matches_request',rows)
    return result('INSUFFICIENT','reading_contract_not_supported',rows)
