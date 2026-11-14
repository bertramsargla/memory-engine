"""Small morphology contract; original spans remain authoritative.

The built-in lexicon covers explicit weekday paradigms only. It never strips a
proper-name suffix. A future external analyzer may return multiple candidate forms;
only the lemma projection is normalized, never the citation or global identity.
"""
from dataclasses import dataclass
import re

RECIPE='bounded-weekday-morphology-v1'
WEEKDAYS={'esmaspäev':'Monday','teisipäev':'Tuesday','kolmapäev':'Wednesday',
          'neljapäev':'Thursday','reede':'Friday','laupäev':'Saturday','pühapäev':'Sunday'}
FORMS={}
for lemma,english in WEEKDAYS.items():
    stem=lemma if lemma=='reede' else lemma+'a'
    for surface,case in ((lemma,'nominative'),(stem,'genitive'),(stem+'l','adessive'),
                         (stem+'ks','translative'),(stem+'le','allative'),(stem+'lt','ablative'),(stem+'st','elative')):
        FORMS.setdefault(surface,[]).append((lemma,english,case))


@dataclass(frozen=True)
class TokenAnalysis:
    surface: str
    start_byte: int
    end_byte: int
    lemma_candidates: tuple[str,...]
    grammatical_cases: tuple[str,...]
    state: str
    recipe: str = RECIPE


def tokens(text,*,source_start=0):
    for match in re.finditer(r'[^\W\d_]+(?:-[^\W\d_]+)*',text,re.UNICODE):
        forms=FORMS.get(match[0].casefold(),[])
        yield TokenAnalysis(match[0],source_start+len(text[:match.start()].encode()),
            source_start+len(text[:match.end()].encode()),tuple(sorted({f[0] for f in forms})),
            tuple(sorted({f[2] for f in forms})),'KNOWN_PARADIGM' if forms else 'UNKNOWN')


def weekday(value):
    forms=FORMS.get(value.casefold(),[])
    # Here only cases establishing a named day or deadline endpoint are projected.
    eligible=[f for f in forms if f[2] in ('nominative','genitive','adessive','translative','allative')]
    values={f[1] for f in eligible}
    return next(iter(values)) if len(values)==1 else None
