"""Memory boundary for bounded evidence reading; no database work belongs in Qt."""
from .query import parse_query
from .reader_contracts import normalize,ReaderBudgetExceeded,budget_result
from .evidence_reader import read,question_slots
from .reader_identity_catalog import witnesses


def read_evidence(memory,query,references,*,scope='project:memory_engine',token=None,now=None,timezone=None,debug=False,support_model=None,semantic_normalizer=None):
    if not isinstance(query,str) or not query.strip() or len(query)>16000:
        raise ValueError('Reader question must contain 1 to 16000 characters')
    if type(debug) is not bool:
        raise ValueError('Explicit developer diagnostic flag required')
    if timezone is None:
        from tzlocal import get_localzone_name
        timezone=get_localzone_name()
    plan=parse_query(query,scope=scope,now=now,tz=timezone)
    person=question_slots(plan)[1]
    try:
        with memory.transaction() as db:
            memory._authorize(db,scope,token)
            units=normalize(db,references,scope)
            identity_witnesses=witnesses(db,plan,person) if units else ()
        result=read(plan,units,debug=debug,identity_witnesses=identity_witnesses,support_model=support_model,
                    semantic_normalizer=semantic_normalizer)
    except ReaderBudgetExceeded as error:
        with memory.transaction() as db:
            memory._authorize(db,scope,token)
        return budget_result(error)
    # Short-lived RAM projections do not outlive a concurrent deletion/revocation.
    # Recheck all support under one final authorization snapshot before release.
    with memory.transaction() as db:
        memory._authorize(db,scope,token)
        current=normalize(db,references,scope)
        if current!=units:
            raise RuntimeError('Evidence metadata changed during reading; retry query')
        if (witnesses(db,plan,person) if units else ())!=identity_witnesses:
            raise RuntimeError('Identity evidence changed during reading; retry query')
    result['trace']['eligibility']=dict(scope_policy='exact_scope_and_eligible_events',
        authorization='checked_before_normalization_and_before_release',content_authority='none')
    return result
