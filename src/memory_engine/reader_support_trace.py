"""Content-free stage witnesses for diagnosing support loss, not proving support."""
from .reader_contracts import identity_hash

RECIPE='support-stage-witnesses-v1'
STATES=frozenset(('PRESENT','MISSING','AMBIGUOUS','REJECTED'))


def stage(name,items,reason,*,state=None,**metadata):
    ids=tuple(dict.fromkeys(items))
    state=state or ('PRESENT' if ids else 'MISSING')
    if state not in STATES:raise ValueError('Invalid support-stage state')
    return dict(stage=name,state=state,reason_code=reason,ids=ids,count=len(ids),**metadata)


def build(plan,units,claims,slots,subject_candidates,relevant,chosen,identity,temporal,edges,allowed,verification,answerability):
    ids=lambda cs:[c.claim_id for c in cs]
    by_state={'RESOLVED':'PRESENT','UNKNOWN':'MISSING','AMBIGUOUS':'AMBIGUOUS'}
    rows=[stage('question',[identity_hash(plan.raw_text)] if slots else [],
                'parsed_slots_present' if slots else 'question_contract_missing',slots=list(slots)),
          stage('candidate_evidence',[u.evidence_id for u in units],'authorized_selected_original_spans'),
          stage('linguistic_extraction',ids(claims),'temporary_propositions_present' if claims else 'no_supported_extraction_frame'),
          stage('normalized_subject',ids(subject_candidates),'subject_candidate_matching' if subject_candidates else 'subject_match_missing',
                state='REJECTED' if claims and not subject_candidates else None),
          stage('normalized_relation',ids(relevant),'requested_relation_matching' if relevant else 'requested_relation_or_target_missing',
                state='REJECTED' if subject_candidates and not relevant else None),
          stage('normalized_value',ids([c for c in relevant if c.value]),'value_projection_present' if relevant else 'no_relevant_value_projection'),
          stage('polarity',ids([c for c in relevant if c.polarity=='positive']),'positive_claims_only_current_question_contract',
                state='REJECTED' if relevant and not any(c.polarity=='positive' for c in relevant) else None),
          stage('certainty',ids([c for c in relevant if not c.uncertain]),'qualified_or_partial_claims_withheld',
                state='REJECTED' if relevant and all(c.uncertain for c in relevant) else None),
          stage('identity',[identity_hash(a) for a in identity.actor_ids] if identity else [],identity.reason if identity else 'identity_resolution_not_requested',
                state=by_state[identity.state] if identity else 'PRESENT'),
          stage('temporal',[r['claim_id'] for r in temporal if r['eligible']],'explicit_temporal_eligibility',
                decisions=[dict(claim_id=r['claim_id'],eligible=r['eligible'],reason=r['reason']) for r in temporal]),
          stage('support_graph',ids(chosen),'graph_and_selection_available',
                conflicts=list(answerability.conflicting_claims),edge_count=len(edges)),
          stage('verifier',ids(allowed),'closed_proposition_integrity_not_semantic_entailment' if allowed else 'no_allowed_claims_to_verify',
                state=('PRESENT' if verification['accepted'] else 'REJECTED') if allowed else 'MISSING',
                independent_semantic_verification='not_invoked'),
          stage('answerability',list(answerability.supporting_claim_ids),answerability.reason_codes[0],
                state={'SUFFICIENT':'PRESENT','INSUFFICIENT':'MISSING','AMBIGUOUS':'AMBIGUOUS','CONFLICTING':'REJECTED'}[answerability.state],
                answerability_state=answerability.state)]
    return dict(recipe=RECIPE,stages=rows,authority='none',raw_content_included=False,
                interpretation='Presence describes pipeline state; it does not certify semantic truth.')
