"""Optional case/role witnesses. A grammatical actor is not a resolved person.

No model is loaded here. All morphological analyses remain in the projection.
An explicit passive agent marker or agreeing object case establishes a role;
neither a dictionary lemma nor a dependency-parser lemma establishes identity.
"""
from dataclasses import dataclass
import hashlib
import re
from .reader_argument_roles import ArgumentFrame, VERBS
from .reader_epistemics import strength

RECIPE='case-constrained-role-bindings-v1'
NP=r'[^\W\d_]+(?:[ -][^\W\d_]+){0,7}'
ACTIVE=re.compile(rf'(?P<left>{NP})\s+(?P<verb>kinnitas|pakkus|kiitis heaks)\s+(?P<right>{NP})\.?',re.I)
PASSIVE=re.compile(rf'(?P<object>{NP})\s+(?P<verb>kinnitati|pakuti)\s+(?P<actor>{NP})\s+poolt\.?',re.I)
BLOCKED=re.compile(r'\b(?:ei|mitte|kui|võimalik|võib|jaoks|ütles|sõnul|kirjutas|ignore|system)\b|["“”„>]',re.I)


@dataclass(frozen=True)
class RoleBinding:
    surface: str
    start_byte: int
    end_byte: int
    role: str
    morphology_candidates: tuple
    normalized_surface_candidates: tuple
    identity_state: str='UNKNOWN'
    identity_authority: str='none'
    recipe: str=RECIPE


@dataclass(frozen=True)
class RoleResolution:
    state: str
    reason: str
    bindings: tuple
    frame: ArgumentFrame | None
    recipe: str=RECIPE


class RoleResolver:
    def __init__(self,backend,*,dependency=None):
        self.backend=backend
        self.dependency=dependency
        self.contract=dict(recipe=RECIPE,morphology=backend.contract,identity_authority='none',
            dependency=dependency.contract if dependency else None)

    def analyses(self,surface):
        tokens=list(re.finditer(r'[^\W\d_]+',surface))
        return tuple(dict(surface=t[0],start_byte=len(surface[:t.start()].encode()),
            end_byte=len(surface[:t.end()].encode()),analyses=tuple(self.backend.analyses(t[0]))) for t in tokens)

    @staticmethod
    def common_forms(tokens):
        if not tokens:return set()
        forms=None
        for i,t in enumerate(tokens):
            # Casing does not distinguish common nouns from people. A proper-name
            # candidate prevents using that token as unambiguous object evidence.
            if any(a['pos']=='H' for a in t['analyses']):return set()
            pos={'S'} if i==len(tokens)-1 else {'A','O'}
            found={a['form'] for a in t['analyses'] if a['pos'] in pos}
            forms=found if forms is None else forms & found
        return forms or set()

    @staticmethod
    def person_forms(tokens):
        if not tokens or any(not t['surface'][0].isupper() for t in tokens):return set()
        forms=None
        for t in tokens:
            found={a['form'] for a in t['analyses'] if a['pos']=='H'}
            forms=found if forms is None else forms & found
        return forms or set()

    def resolve(self,text,*,source_start=0):
        if not isinstance(text,str) or len(text.encode())>4096 or source_start<0:
            return RoleResolution('UNKNOWN','role_input_budget_or_span',(),None)
        if BLOCKED.search(text) or strength(text)!='CERTAIN':
            return RoleResolution('UNKNOWN','unsupported_scope_polarity_or_modality',(),None)
        body=text.strip();base=len(text)-len(text.lstrip())
        match=PASSIVE.fullmatch(body);options=[];weak=[]
        try:
            if match:
                objects=self.analyses(match['object']);actors=self.analyses(match['actor'])
                if 'sg n' in self.common_forms(objects) and 'sg g' in self.person_forms(actors):
                    options.append(('agent_last_passive','actor','object',actors,objects))
                elif 'sg n' in self.common_forms(objects) and match['actor'][0].isupper():
                    weak.append(('agent_last_passive','actor','object',actors,objects))
            else:
                match=ACTIVE.fullmatch(body)
                if match:
                    left=self.analyses(match['left']);right=self.analyses(match['right'])
                    for voice,actor,obj,at,ot in [('active_et','left','right',left,right),('object_first_et','right','left',right,left)]:
                        if 'sg n' in self.person_forms(at) and self.common_forms(ot)&{'sg g','sg p'}:
                            options.append((voice,actor,obj,at,ot))
                        elif match[actor][0].isupper() and self.common_forms(ot)&{'sg g','sg p'}:
                            weak.append((voice,actor,obj,at,ot))
        except (ValueError,RuntimeError,OSError):
            return RoleResolution('UNKNOWN','morphology_unavailable',(),None)
        if self.dependency is not None and match:
            try:
                signal=self.dependency.roles(text)
                if signal is not None:
                    if signal.get('input_sha256')!=hashlib.sha256(text.encode()).hexdigest():
                        return RoleResolution('UNKNOWN','dependency_input_mismatch',(),None)
                    def agrees(option):
                        for group,role in [(option[1],'actor'),(option[2],'object')]:
                            a,b=match.span(group)
                            lo=len(text[:base+a].encode());hi=len(text[:base+b].encode())
                            span=signal.get(role)
                            if not isinstance(span,(list,tuple)) or len(span)!=2 or any(type(v) is not int for v in span) or not lo<=span[0]<span[1]<=hi:return False
                        return True
                    matched=[o for o in options+weak if agrees(o)]
                    if options and not any(agrees(o) for o in options):
                        return RoleResolution('AMBIGUOUS','dependency_and_case_disagree',(),None)
                    options=matched
            except (ValueError,RuntimeError,OSError):
                return RoleResolution('UNKNOWN','dependency_unavailable',(),None)
        if len(options)!=1:
            return RoleResolution('AMBIGUOUS' if options else 'UNKNOWN','role_candidates_disagree' if options else 'role_case_not_established',(),None)
        voice,ag,og,at,ot=options[0];bindings=[];roles=[]
        for group,role,tokens in [(ag,'actor',at),('verb','verb',()),(og,'object',ot)]:
            a,b=match.span(group);a+=base;b+=base
            start=source_start+len(text[:a].encode());end=source_start+len(text[:b].encode())
            surface=text[a:b]
            # Candidate lists are diagnostic linguistic hypotheses. No Cartesian
            # combination or preferred lemma creates a person identity.
            candidates=tuple(sorted({a['lemma'] for t in tokens for a in t['analyses'] if a['pos']==('H' if role=='actor' else 'S')}))
            bindings.append(RoleBinding(surface,start,end,role,tokens,candidates))
            roles.append((role,start,end,surface))
        frame=ArgumentFrame(match[ag],VERBS[match['verb'].casefold()],match[og],
            'positive','CERTAIN',None,voice,tuple(roles),recipe=RECIPE)
        return RoleResolution('RESOLVED','case_dependency_consensus' if self.dependency is not None and signal is not None else 'explicit_agent_marker_or_case_agreement',tuple(bindings),frame)

    def frames(self,text,*,source_start=0):
        # Do not detach reported/conditional text into new certain sentences.
        result=self.resolve(text,source_start=source_start)
        if result.frame is not None:yield result.frame


def resolve_name(binding,participants,*,bound_native_key=None):
    """Context may narrow candidates; only an adapter-bound key resolves identity.

    Participants are caller-authorized records with strong key, display_name and
    explicit historical aliases. Raw nearby text and the query are not aliases.
    """
    from .reader_identity import actor_key
    candidates=[]
    names={binding.surface.casefold(),*(s.casefold() for s in binding.normalized_surface_candidates)}
    for person,aliases in participants:
        key=actor_key(person)
        if key and names & {s.casefold() for s in (person.display_name,*aliases) if s}:
            candidates.append(key)
    keys=tuple(sorted(set(candidates)))
    if bound_native_key is not None:
        return dict(state='RESOLVED' if bound_native_key in keys else 'UNKNOWN',
            key=bound_native_key if bound_native_key in keys else None,candidates=keys,
            reason='adapter_bound_role' if bound_native_key in keys else 'adapter_key_not_a_name_candidate')
    return dict(state='AMBIGUOUS' if len(keys)>1 else 'UNKNOWN',key=None,candidates=keys,
        reason='multiple_contextual_candidates' if len(keys)>1 else 'textual_role_not_adapter_bound')
