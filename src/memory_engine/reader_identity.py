"""Bounded actor resolution. Names identify candidates; they do not merge actors."""
from dataclasses import dataclass
import re
from .reader_contracts import identity_hash


@dataclass(frozen=True)
class IdentityResolution:
    state: str
    actor_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason: str


def actor_key(identity):
    if identity.confidence is not None and identity.confidence<1:
        return None
    value=identity.native_id
    if not value or value.casefold() in ('unknown','unavailable'):
        return None
    # Email local parts are not universally case-insensitive. Domain is.
    if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
        local,domain=value.rsplit('@',1)
        return 'email:'+local+'@'+domain.casefold()
    if not identity.platform or not (identity.workspace or identity.account):
        return None
    return 'native:'+identity_hash([identity.platform,identity.workspace,identity.account,value])


def resolve(name, units):
    direct=[u for u in units if (u.author_identity.display_name or '').casefold()==name.casefold()
            or (u.author_identity.native_id or '')==name]
    if not direct:
        return IdentityResolution('UNKNOWN',(),(),'no_matching_author')
    keys={actor_key(u.author_identity) for u in direct}
    known=keys-{None}
    if len(known)>1 or (None in keys and (known or len({u.source_id for u in direct})>1)):
        return IdentityResolution('AMBIGUOUS',tuple(sorted(known)),tuple(u.evidence_id for u in direct),'multiple_unmerged_actors')
    if not known:
        return IdentityResolution('UNKNOWN',(),tuple(u.evidence_id for u in direct),'display_name_without_strong_identity')
    # This also carries historical display-name changes sharing a strong identifier.
    eligible=[u for u in units if actor_key(u.author_identity) in known]
    return IdentityResolution('RESOLVED',tuple(sorted(known)),tuple(u.evidence_id for u in eligible),'strong_identifier_match')
