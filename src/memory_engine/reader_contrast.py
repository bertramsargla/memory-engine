"""Explicit not-X-but-Y value contrast. No correction inferred from recency."""
import re

PATTERN=re.compile(r'(?:not|mitte)\s+(?P<negative>[^,;.!?]+?),?\s+(?:but|vaid)\s+(?P<positive>[^,;.!?]+)',re.I)


def values(surface):
    match=PATTERN.fullmatch(surface.strip())
    if not match:return None
    if re.search(r'\b(?:if|unless|maybe|or|kui|või|ehk)\b',surface,re.I):return None
    return match['positive'].strip(),match['negative'].strip()
