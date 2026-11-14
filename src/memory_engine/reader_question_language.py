"""Explicit question argument spans. No capitalization-based subject guessing."""
import re

NP=r'[\w][\w -]{0,100}?'
FUNCTIONAL=re.compile(rf'Mis\s+on\s+(?P<subject>{NP})\s+(?P<relation>eelarve|tähtaeg)\?',re.I)
MEETING=re.compile(rf'Millal\s+(?:toimus|peeti)\s+(?P<subject>{NP})\s+koosolek\?',re.I)
PERSON=re.compile(rf'Mida\s+(?P<person>{NP})\s+(?:ütles|kirjutas|rääkis)(?:\s+[^?]+)?\?',re.I)
OWNERS=[re.compile(rf'Who\s+owns\s+(?:the\s+)?(?P<subject>{NP})\?',re.I),
        re.compile(rf'Kes\s+vastutab\s+(?P<subject>{NP})\s+eest\?',re.I)]


def functional(text):
    for pattern in OWNERS:
        match=pattern.fullmatch(text.strip())
        if match:return (('owner',),match['subject'])
    match=FUNCTIONAL.fullmatch(text.strip())
    if match:return (('budget' if match['relation'].casefold()=='eelarve' else 'deadline',),match['subject'])
    match=MEETING.fullmatch(text.strip())
    if match:return (('meeting_time',),match['subject'])
    return None


def person(text):
    match=PERSON.fullmatch(text.strip())
    return match['person'] if match else None
