"""Verification of a closed proposition language, not arbitrary model prose.

Any extra prose is unverified. The generator can select/phrase approved proposition
records only through canonical rendering. Matching citation IDs alone is insufficient.
"""
from dataclasses import asdict


def render(claims,units):
    by_id={u.evidence_id:u for u in units}
    lines=[]
    for c in claims:
        sources=' '.join('['+by_id[e].citation+']' for e in c.support_ids)
        if c.assertion_kind=='observed_mention':
            phrase=f'The selected source contains “{c.value}”.'
        elif c.assertion_kind=='adapter_outcome':
            phrase=f'The adapter recorded {c.subject}: {c.value}.'
        elif c.assertion_kind=='reviewed_interpretation':
            phrase=f'The reviewed interpretation records {c.subject} {c.predicate}: {c.value}.'
        elif c.assertion_kind=='source_attribution':
            phrase=f'The source attributes the {c.predicate} role to the name “{c.value}”; native identity is unknown.'
        elif c.assertion_kind=='observation_metadata':
            phrase=f'The stored {c.predicate} timestamp is {c.value}.'
        elif c.assertion_kind=='source_temporal':
            phrase=f'The source states {c.subject} {c.predicate}: {c.value}; this is a source-described time.'
        else:
            phrase=f'The source states {c.subject} {c.predicate}: {c.value}.'
        lines.append(phrase+' '+sources)
    return '\n'.join(lines)


def verify_proposals(proposals,plan,allowed,units):
    """Only exact plan propositions can be certified. No confidence-based entailment.

    A caller/model cannot strengthen attribution/time or swap support. Unknown fields
    are compared too. Claims must first have been derived by the bounded extractor;
    this function does not turn arbitrary candidates into established truth.
    """
    candidates={c.claim_id:asdict(c) for c in allowed}
    unit_ids={u.evidence_id for u in units}
    results=[]
    for proposal in proposals:
        if not isinstance(proposal,dict):
            results.append(dict(state='UNSUPPORTED',reason='malformed_proposition'));continue
        gold=candidates.get(proposal.get('claim_id'))
        state='UNSUPPORTED';reason='claim_not_allowed'
        if gold is not None and proposal['claim_id'] in plan.claims_allowed:
            # Exact structure: unexpected fields can conceal stronger assertions.
            normalized={k:(list(v) if isinstance(v,tuple) else v) for k,v in gold.items()}
            supplied={k:(list(v) if isinstance(v,tuple) else v) for k,v in proposal.items()}
            if supplied==normalized and set(gold['support_ids'])<=unit_ids:
                state,reason='SUPPORTED','same_bounded_proposition_and_support'
            elif proposal.get('value')!=gold['value'] or proposal.get('speaker')!=gold['speaker'] or proposal.get('authored_at')!=gold['authored_at']:
                state,reason='CONTRADICTED','value_identity_or_time_changed'
            else:
                state,reason='PARTIALLY_SUPPORTED','support_or_claim_strength_changed'
        results.append(dict(claim_id=proposal.get('claim_id'),state=state,reason=reason))
    return results


def verify_generated(answer,proposals,plan,allowed,units):
    checks=verify_proposals(proposals,plan,allowed,units)
    canonical=render(allowed,units)
    valid=all(c['state']=='SUPPORTED' for c in checks) and len(proposals)==len(allowed) and answer==canonical
    return dict(accepted=valid,claims=checks,reason='closed_language_verified' if valid else 'unverified_text_or_proposition',
                arbitrary_prose_entailment='not_supported')
