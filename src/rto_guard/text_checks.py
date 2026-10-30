"""Name and address quality checks: the only part of the risk decision that uses an LLM.

Why here and nowhere else:
  * Deciding whether "Station chowk se left, Hanuman Mandir ke saamne" is a
    findable address, or whether "Kavya Iyer" is real and "qwerty" isn't, is a
    language problem. Rules can't keep up with every Hinglish spelling.
  * Everything numeric (risk score, ₹ maths, thresholds, pincode history) is
    plain code: it is cheaper, gives the same answer every time, and can be
    explained line by line.

The LLM (Gemini) only fills in two labels: address quality and fake name.
The scoring model decides what those labels are worth. If there is no API key,
or the call fails or times out, the rule-based checks below are used instead,
so an order is never blocked waiting for the LLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass

from . import ARTIFACTS

ADDRESS_LEVELS = ("complete", "landmark", "vague", "unusable")

JUNK_ADDRESSES = {
    "", ".", "..", "-", "xyz", "abc", "home", "house", "call me", "call", "na", "n/a",
    "test", "address", "same", "no", "nil", "ghar",
}
FAKE_NAME_TOKENS = {
    "test", "asdf", "qwerty", "xyz", "abc", "aaa", "na", "n/a", "none", "null",
    "sample", "dummy", "fake", "order", "customer", "user", "asd", "zzz",
}
KEYBOARD_RUNS = ("qwert", "asdf", "zxcv", "hjkl", "yuiop")
HOUSE_NUMBER = re.compile(
    r"(\b(house|h\.?\s?no|flat|plot|room|door|block|wing|shop|ghar)\b.*\d)|(\b[a-z]?-?\d+[/-]\d+\b)|(\b[a-z]-\d+\b)|(^\d+)",
    re.I,
)
LANDMARK = re.compile(
    r"\b(near|opp\.?|opposite|behind|ke\s+(paas|saamne|samne|peeche|piche|andar)|wala\s+ghar|chowk|gali|"
    r"mandir|masjid|school|bank|station|tank|petrol|crossing|bazaar|market|hospital)\b",
    re.I,
)
HINGLISH = re.compile(r"\b(ke|ka|ki|wala|wali|paas|saamne|peeche|andar|krke|karke|aana|ghar|gali)\b", re.I)


@dataclass
class TextVerdict:
    address_quality: str      # complete | landmark | vague | unusable
    address_note: str
    name_looks_fake: bool
    name_note: str
    hinglish: bool
    source: str               # "gemini" or "rules"


# ----------------------------------------------------------------- rules ---

def rule_check(name: str, address: str, city: str = "") -> TextVerdict:
    addr = (address or "").strip()
    a = addr.lower()
    hinglish = bool(HINGLISH.search(a))

    if a in JUNK_ADDRESSES or len(re.sub(r"[^a-z0-9]", "", a)) < 4:
        quality, note = "unusable", f'Address is just "{addr}" — courier cannot find it.'
    elif city and a == city.strip().lower():
        quality, note = "unusable", "Address is only the city name."
    elif HOUSE_NUMBER.search(a) and len(a.split()) >= 3:
        quality, note = "complete", "Has a house/flat number and locality."
    elif LANDMARK.search(a) and len(a.split()) >= 4:
        quality, note = "landmark", "No house number, but a clear landmark the courier can ask for."
    else:
        quality, note = "vague", f'Address "{addr}" is too vague to find without a call.'

    n = (name or "").strip().lower()
    tokens = re.findall(r"[a-z]+", n)
    fake, why = False, "Looks like a real name."
    if not tokens or len(n) < 3:
        fake, why = True, "Name is missing or too short."
    elif any(t in FAKE_NAME_TOKENS for t in tokens):
        fake, why = True, f'Name "{name}" looks like a placeholder.'
    elif any(run in n.replace(" ", "") for run in KEYBOARD_RUNS):
        fake, why = True, f'Name "{name}" looks like keyboard mashing.'
    elif len(tokens) >= 2 and len(set(tokens)) == 1:
        fake, why = True, f'Name "{name}" repeats the same word.'
    elif re.search(r"(.)\1\1", n):
        fake, why = True, f'Name "{name}" repeats the same letter.'

    return TextVerdict(quality, note, fake, why, hinglish, "rules")


# ---------------------------------------------------------------- gemini ---

PROMPT = """You check Cash-on-Delivery orders for an Indian D2C haircare brand.
Many addresses are in Hinglish (Hindi written in English letters), e.g.
"gali no 4 ke andar, Petrol Pump ke paas" = "inside lane 4, near the petrol pump".

Classify the delivery address:
- "complete": has a house/flat/plot number AND a street/locality/society.
- "landmark": no house number, but describes a specific findable place
  (e.g. "Hanuman Mandir ke peeche wala ghar", "near SBI Bank, ghar ka naam Om niwas").
- "vague": a generic hint a courier cannot act on (e.g. "near station").
- "unusable": junk or empty ("xyz", ".", "home", "call me", only the city name).

Also say if the customer name looks fake (placeholder, keyboard mashing,
"test", "asdf", repeated words). Common Indian names, nicknames and single
names are NOT fake.

Order:
name: {name}
address: {address}
city: {city}

Reply with JSON only:
{{"address_quality": "...", "address_note": "<max 15 words, plain English>",
  "name_looks_fake": true/false, "name_note": "<max 12 words>", "hinglish": true/false}}"""

_CACHE_FILE = ARTIFACTS / "llm_cache.json"
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    _CACHE_FILE.parent.mkdir(exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False), encoding="utf-8")


def gemini_available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def call_gemini(prompt: str, timeout: float = 8.0) -> dict:
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    text = payload["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def gemini_check(name: str, address: str, city: str = "") -> TextVerdict:
    key = hashlib.sha1(f"{name}|{address}|{city}".encode()).hexdigest()
    cache = _load_cache()
    if key not in cache:
        cache[key] = call_gemini(PROMPT.format(name=name, address=address, city=city))
        _save_cache()
    r = cache[key]
    if r.get("address_quality") not in ADDRESS_LEVELS:
        raise ValueError(f"Unexpected LLM label: {r.get('address_quality')}")
    return TextVerdict(
        r["address_quality"], r.get("address_note", ""), bool(r.get("name_looks_fake")),
        r.get("name_note", ""), bool(r.get("hinglish")), "gemini",
    )


def check(name: str, address: str, city: str = "", use_llm: bool = True) -> TextVerdict:
    """Gemini when available, rules otherwise. Never raises."""
    rules = rule_check(name, address, city)
    if not (use_llm and gemini_available()):
        return rules
    try:
        llm = gemini_check(name, address, city)
    except Exception:
        return rules
    # Hard junk ("xyz", ".") is caught by rules with certainty; don't let the LLM
    # talk us out of it.
    if rules.address_quality == "unusable":
        llm.address_quality, llm.address_note = rules.address_quality, rules.address_note
    return llm


def as_dict(v: TextVerdict) -> dict:
    return asdict(v)
