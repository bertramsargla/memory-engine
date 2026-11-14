"""Deterministic query planning. Mentions are strings, never resolved identities."""
from dataclasses import asdict,dataclass
from datetime import datetime,timedelta,timezone
import re
from zoneinfo import ZoneInfo

RECIPE='memory-query-v2.1'
WORDS=re.compile(r"[^\W_]+(?:[-_][^\W_]+)*",re.UNICODE)
IDENTIFIERS=re.compile(r'(?<![\w-])[A-Za-z]{2,16}[-_]\d+(?:[-_][A-Za-z0-9]+)*(?![\w-])')
STOP=set('''where did do does i me my we our you your they their what which who whom
is are was were be been being have has had can could would should will shall a an the
and or of to for from with about in on at by it this that these those there here
see saw seen find locate recall remember tell told say said make made using use believe believed know knew known thought
before after during around yesterday today eilne eile täna
please finally currently current latest previously previous actually really ever
kus ma nägin näha seda see mis mida millal mulle minu me meie kas on oli olid
ütles ütle palun leia ettevõtet kohta'''.split())
DAYS={'monday':0,'tuesday':1,'wednesday':2,'thursday':3,'friday':4,'saturday':5,'sunday':6,
      'esmaspäeval':0,'teisipäeval':1,'kolmapäeval':2,'neljapäeval':3,'reedel':4,'laupäeval':5,'pühapäeval':6}
MONTHS={m.lower():i for i,m in enumerate(['January','February','March','April','May','June','July','August','September','October','November','December'],1)}


@dataclass(frozen=True)
class MemoryQuery:
    raw_text:str
    intent:str
    lexical_terms:tuple[str,...]
    phrases:tuple[str,...]
    exact_identifiers:tuple[str,...]
    entity_mentions:tuple[str,...]
    source_hints:tuple[str,...]
    account_hints:tuple[str,...]
    temporal_expression:str|None
    temporal_interval:tuple[str,str]|None
    temporal_mode:str
    requested_provenance:tuple[str,...]
    scope:str
    language:str
    ambiguity:tuple[str,...]
    semantic_query:str
    sequence_anchor:str|None=None
    as_of:str|None=None
    timezone_name:str='UTC'
    recipe:str=RECIPE

    def as_dict(self):return asdict(self)


def parse_query(text,*,scope='project:memory_engine',now=None,tz='UTC'):
    if not isinstance(text,str) or len(text)>2000:
        raise ValueError('Invalid memory query; maximum 2,000 characters.')
    clock=datetime.fromisoformat(now) if isinstance(now,str) else (now or datetime.now(timezone.utc))
    if clock.tzinfo is None:raise ValueError('Query clock must have a timezone.')
    clock=clock.astimezone(ZoneInfo(tz))
    low=text.casefold(); intent='recall_fact'; mode='none'; required=[]; ambiguous=[]
    if re.search(r'\b(where|kus|find|locate)\b',low):intent='locate_source'
    if re.search(r'\b(tell|told|say|said|ütles)\b',low):intent='recall_person_statement'; required.append('speaker')
    if re.search(r'\b(reply|draft|vastus)\b',low):intent='draft_with_history'
    if re.search(r'\b(current|latest|finally|currently|praegu|viimane)\b',low):intent='current_state';mode='current'
    if re.search(r'\b(believe|believed|know|knew|known|thought)\b',low):intent='historical_state';mode='known_at_time'
    if re.search(r'\b(changed|compare|changes|difference)\b',low):intent='compare_updates'
    if re.search(r'\b(did|have|has|was|kas)\b.*\b(send|sent|approve|approved|read|promise|promised)\b',low):
        intent='verify_action'; required.append('action_state')
    if re.search(r'\b(when.*(?:write|wrote|author)|authored)\b',low):required.append('authored_at')
    exact=tuple(dict.fromkeys(m.group() for m in IDENTIFIERS.finditer(text)))
    if exact and intent=='recall_fact':intent='retrieve_exact_identifier'
    phrases=tuple(dict.fromkeys(m.group(1) for m in re.finditer(r'["“]([^"”]{1,256})["”]',text)))
    expression=None; interval=None; day=None; removed=set()
    relative=re.search(r'\b(yesterday|today|eile|täna)\b',low)
    date=re.search(r'\b(20\d\d-\d\d-\d\d)\b',text)
    month=re.search(r'\b('+'|'.join(MONTHS)+r')\s+(\d{1,2})(?:,?\s+(20\d\d))?\b',low)
    weekday=re.search(r'\b(?:on|at|before|after|around)\s+('+'|'.join(DAYS)+r')\b',low)
    if relative:
        expression=relative[0];day=(clock-timedelta(days=1 if relative[0] in ('yesterday','eile') else 0)).date()
    elif date:
        expression=date[0];day=datetime.fromisoformat(date[0]).date()
    elif month:
        expression=month[0];day=datetime(int(month[3] or clock.year),MONTHS[month[1]],int(month[2])).date()
        if not month[3]:ambiguous.append('year_resolved_from_query_clock')
    elif weekday:
        expression=weekday[0];day=(clock-timedelta(days=(clock.weekday()-DAYS[weekday[1]])%7)).date()
        ambiguous.append('weekday_resolved_to_most_recent')
    if day:
        start=datetime(day.year,day.month,day.day,tzinfo=clock.tzinfo);end=start+timedelta(days=1)
        if re.search(r'\bmorning\b',low):end=start+timedelta(hours=12);removed.add('morning')
        interval=(start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat())
        removed.update(w.casefold() for w in WORDS.findall(expression))
        if mode!='known_at_time':
            mode=next((v for k,v in [('before','before'),('after','after'),('around','around'),('during','during')] if re.search(r'\b'+k+r'\b',low)),'during')
    anchor=None
    seq=re.search(r'\b(before|after)\s+(?:i\s+)?(?:opened|opening|viewed)\s+(?:the\s+)?(.+?)[?.!]*$',text,re.I)
    if seq:
        intent='activity_before_after';mode='sequence';anchor=seq[2].rstrip('?.!')
    if mode=='known_at_time' and interval is None:ambiguous.append('historical_time_missing')
    words=WORDS.findall(text)
    terms=tuple(dict.fromkeys(w for w in words if w.casefold() not in STOP|removed))[:32]
    # Question grammatical verbs are not mandatory evidence terms. Negation and
    # semantic content stay in semantic_query even when lexical terms are omitted.
    entities=tuple(dict.fromkeys(w for w in words if w[:1].isupper() and w.casefold() not in STOP and w.casefold() not in MONTHS and w.casefold() not in DAYS))
    sources=tuple(k for k in ('email','thread','document','note','terminal','webpage','conversation') if re.search(r'\b'+k+r'\b',low))
    accounts=tuple(k for k in ('work','personal') if re.search(r'\b'+k+r' account\b',low))
    lang='et' if any(w.casefold() in {'kus','mida','millal','nägin','kas','mulle','minu','ütles'} for w in words) else 'en'
    return MemoryQuery(text,intent,terms,phrases,exact,entities,sources,accounts,expression,interval,mode,
                       tuple(required),scope,lang,tuple(ambiguous),text,anchor,clock.astimezone(timezone.utc).isoformat(),tz)
