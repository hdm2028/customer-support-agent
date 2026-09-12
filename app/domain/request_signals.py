"""Conservative local handling of explicit denials in business text.

This extracts asserted mentions, not verified product/payment facts. Only a
short, explicit denial directly preceding the phrase suppresses a match.
Ambiguous or double negation retains the existing conservative signal.
"""
import re

_BOUNDARY = re.compile(r"[，,。；;！？!?\n]|但是|不过|然而")
_DENIAL = re.compile(r"(?:并没有|没有|并非|不是|并不|尚未|不再|无需|不必|不|没)(?:任何|什么|存在|出现|发生|要求|需要|打算|准备|想要|想|会|要|去|进行|提出|有|的)*$")
_NEGATIVE = re.compile(r"不|没|未|非|无")


def asserted_mentions(text: str, phrases) -> list[str]:
    matches = []
    for phrase in phrases:
        for found in re.finditer(re.escape(phrase), text):
            prefix = _BOUNDARY.split(text[:found.start()])[-1]
            denial = _DENIAL.search(prefix)
            if denial and len(denial[0]) <= 12 and not _NEGATIVE.search(prefix[:denial.start()]):
                continue
            matches.append(phrase)
            break
    return matches
