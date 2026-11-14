"""Explicit temporal operators. Observation never substitutes for event/known time."""
from datetime import datetime, timezone


def instant(value):
    if value is None:
        return None
    dt=datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('Reader time needs a timezone')
    return dt.astimezone(timezone.utc)


def interval_contains(start,end,point):
    p=instant(point)
    if end is not None and p>=instant(end):
        return False
    if start is None:
        return None
    return instant(start)<=p and (end is None or p<instant(end))


def eligible(claim,plan):
    if plan.temporal_mode=='known_at_time':
        if not plan.temporal_interval:
            return False,'knowledge_cutoff_missing'
        cutoff=instant(plan.temporal_interval[1])
        if claim.known_at is None:
            return False,'known_time_unknown'
        if instant(claim.known_at)>=cutoff or (claim.known_to and instant(claim.known_to)<cutoff):
            return False,'outside_knowledge_interval'
        return True,'known_before_cutoff'
    if claim.known_at and instant(claim.known_at)>instant(plan.as_of):
        return False,'not_yet_known'
    if claim.known_to and instant(claim.known_to)<=instant(plan.as_of):
        return False,'knowledge_superseded'
    if plan.temporal_mode in ('current','latest') and (claim.valid_from or claim.valid_to):
        if interval_contains(claim.valid_from,claim.valid_to,plan.as_of) is False:
            return False,'outside_valid_interval'
    if plan.temporal_mode in ('before','after','during','at_time','around') and plan.temporal_interval:
        # These are event-time operators, not a second observation-time search.
        # The time a message was authored is not when a described meeting happened.
        # Only explicit reviewed validity, or an explicitly requested authoring-time
        # question, can use these axes. Free-text event dates require a separate
        # interpretation contract (including timezone/granularity).
        basis=claim.valid_from or (claim.authored_at if 'authored_at' in plan.requested_provenance else None)
        if basis is None:
            return False,'event_time_unknown'
        start,end=map(instant,plan.temporal_interval);point=instant(basis)
        ok=point<start if plan.temporal_mode=='before' else point>=end if plan.temporal_mode=='after' else start<=point<end
        return ok,'event_interval_match' if ok else 'outside_event_interval'
    return True,'no_unsupported_time_substitution'


def order(left,right,basis='known_at'):
    if basis not in ('known_at','authored_at','valid_from'):
        raise ValueError('Explicit temporal axis required')
    a,b=instant(getattr(left,basis)),instant(getattr(right,basis))
    if a is None or b is None or a==b:
        return 'UNKNOWN_RELATION'
    return 'TEMPORALLY_BEFORE' if a<b else 'TEMPORALLY_AFTER'


def latest_supported(claims,basis='known_at'):
    """Ordering alone is not supersession: callers must establish comparable claims."""
    if not claims or any(getattr(c,basis) is None for c in claims):
        return ()
    latest=max(instant(getattr(c,basis)) for c in claims)
    return tuple(c for c in claims if instant(getattr(c,basis))==latest)
