"""Which language a message is in, roughly, and the rewrite request when a reply is in the wrong one.

The first two real runs showed why this is code and not only a prompt rule: told in the system
prompt to answer in the user's language, gpt-oss-120b still answered 3 of 10 English questions
in Swedish (the market is SE and the city is Gothenburg). So the agent checks each reply and, on
a mismatch, asks the model once to rewrite it. Only English and Swedish are recognised; any other
language is left alone.
"""

import re

from crewgate.plugins.booking.confirmation import plain

_WORDS = {
    "en": re.compile(
        r"\b(the|and|not|is|are|for|you|your|can|has|to|of|with|on|no|it|a|an|what|who|from|me|my|at|please)\b",
        re.IGNORECASE,
    ),
    "sv": re.compile(
        r"\b(och|inte|är|för|jag|det|har|kan|du|din|vill|ingen|inga|den|med|på|av|en|ett|vad|vem|från|mig)\b",
        re.IGNORECASE,
    ),
}
NAMES = {"en": "English", "sv": "Swedish"}


def detect(text: str, min_hits: int = 1) -> str | None:
    """ "en", "sv", or None when the text is too short or mixed to tell."""
    text = plain(text)
    hits = {lang: len(rx.findall(text)) for lang, rx in _WORDS.items()}
    best = max(hits, key=lambda k: hits[k])
    others = [v for k, v in hits.items() if k != best]
    if hits[best] < min_hits or any(v >= hits[best] for v in others):
        return None
    return best


def mismatch(user_text: str, reply: str) -> str | None:
    """The language the reply should have been in, if it clearly isn't."""
    want = detect(user_text)
    got = detect(reply, min_hits=3)
    return want if want and got and want != got else None


def rewrite_request(reply: str, language: str) -> str:
    return (
        f"Rewrite the reply below in {NAMES[language]}. Keep every fact, number, id, name and link "
        "exactly as it is. Answer with the rewritten reply only.\n\n" + reply
    )
