"""
Wiktionary API integration for Ukrainian dictionary bot.
Fetches rich word data (part of speech, definitions, pronunciation, etc.)
from English Wiktionary REST API — 53,000+ Ukrainian words available.
Free, no API key needed.
"""

import re
import logging
import requests
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

WIKT_API = "https://en.wiktionary.org/api/rest_v1/page/definition"

# In-memory cache: word → parsed result (keeps last 2000 lookups)
_cache: dict = {}
_MAX_CACHE = 2000


def _clean_html(html_text: str) -> str:
    """Remove HTML tags but keep text content."""
    text = re.sub(r'<[^>]+>', '', html_text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#39;', "'").replace('&quot;', '"')
    return text.strip()


def _extract_pronunciation(html_text: str) -> str:
    """Try to extract IPA pronunciation from definition HTML."""
    # IPA patterns commonly found in Wiktionary
    ipa_match = re.search(r'IPA[^:]*:\s*/?([^/<\]]+)/?', html_text)
    if ipa_match:
        return ipa_match.group(1).strip()
    return ""


def lookup_wiktionary(word: str) -> Optional[dict]:
    """
    Look up a Ukrainian word on English Wiktionary.
    Returns dict with:
        - word: str
        - pos: str (part of speech)
        - definitions: list[str] (English definitions)
        - all_pos: list[dict] with {pos, definitions}
        - pronunciation: str (IPA if available)
    Returns None if word not found or not in Ukrainian section.
    """
    word = word.strip().lower()
    if not word or len(word) > 100:
        return None

    # Check cache
    if word in _cache:
        return _cache[word]

    try:
        url = f"{WIKT_API}/{requests.utils.quote(word)}"
        resp = requests.get(url, timeout=5, headers={
            'User-Agent': 'UkrainianDictionaryBot/1.0 (Telegram bot for learning Ukrainian)'
        })

        if resp.status_code == 404:
            _cache_put(word, None)
            return None

        resp.raise_for_status()
        data = resp.json()

    except requests.RequestException as e:
        logger.warning(f"Wiktionary API error for '{word}': {e}")
        return None
    except ValueError:
        logger.warning(f"Wiktionary API: invalid JSON for '{word}'")
        return None

    # Find Ukrainian section
    ukrainian_section = None
    for lang_section in data.get("en", []) if isinstance(data, dict) else []:
        pass  # "en" key is for English Wiktionary format

    # The REST API returns a dict with language keys
    # Try to find Ukrainian data
    ukr_data = None
    if isinstance(data, dict):
        for key, sections in data.items():
            if isinstance(sections, list):
                for section in sections:
                    if isinstance(section, dict) and section.get("language", "").lower() == "ukrainian":
                        ukr_data = sections if not ukr_data else ukr_data
                        break
                if ukr_data:
                    break

    if not ukr_data:
        _cache_put(word, None)
        return None

    # Parse Ukrainian entries
    all_pos = []
    pronunciation = ""

    for section in ukr_data:
        if not isinstance(section, dict):
            continue
        if section.get("language", "").lower() != "ukrainian":
            continue

        pos = section.get("partOfSpeech", "unknown")
        definitions_raw = section.get("definitions", [])

        defs = []
        for d in definitions_raw:
            if isinstance(d, dict):
                def_text = _clean_html(d.get("definition", ""))
                if def_text and not def_text.startswith("("):
                    defs.append(def_text)
                elif def_text:
                    defs.append(def_text)

                # Check for pronunciation info
                if not pronunciation:
                    parsed_pron = d.get("parsedExamples", [])
                    full_html = d.get("definition", "")
                    pron = _extract_pronunciation(full_html)
                    if pron:
                        pronunciation = pron

        if defs:
            all_pos.append({
                "pos": _normalize_pos(pos),
                "definitions": defs[:5]  # Max 5 definitions per POS
            })

    if not all_pos:
        _cache_put(word, None)
        return None

    result = {
        "word": word,
        "pos": all_pos[0]["pos"],
        "definitions": all_pos[0]["definitions"],
        "all_pos": all_pos,
        "pronunciation": pronunciation,
    }

    _cache_put(word, result)
    return result


def _cache_put(word: str, value):
    """Add to cache with size limit."""
    global _cache
    _cache[word] = value
    if len(_cache) > _MAX_CACHE:
        # Remove oldest half
        keys = list(_cache.keys())[:_MAX_CACHE // 2]
        for k in keys:
            del _cache[k]


def _normalize_pos(pos: str) -> str:
    """Normalize English POS labels to Ukrainian abbreviations."""
    pos_lower = pos.lower().strip()
    mapping = {
        "noun": "ім.",
        "verb": "дієсл.",
        "adjective": "прикм.",
        "adverb": "присл.",
        "pronoun": "займ.",
        "preposition": "прийм.",
        "conjunction": "спол.",
        "interjection": "вигук",
        "particle": "частка",
        "numeral": "числ.",
        "determiner": "означ.",
        "proper noun": "власна назва",
        "phrase": "фраза",
        "prefix": "префікс",
        "suffix": "суфікс",
    }
    return mapping.get(pos_lower, pos)


def format_wiktionary_entry(result: dict) -> str:
    """Format Wiktionary result as a nice text block for Telegram."""
    if not result:
        return ""

    lines = []

    # All POS entries
    for i, pos_data in enumerate(result["all_pos"]):
        pos = pos_data["pos"]
        defs = pos_data["definitions"]

        if len(result["all_pos"]) > 1:
            lines.append(f"<b>{pos}</b>")
        else:
            lines.append(f"[ {pos} ]")

        for j, d in enumerate(defs[:3], 1):  # Max 3 defs shown
            lines.append(f"  {j}. {d}")

    if result.get("pronunciation"):
        lines.append(f"🔉 [{result['pronunciation']}]")

    return "\n".join(lines)


# Quick test
if __name__ == "__main__":
    test_words = ["сучасний", "волошковий", "брунатний", "смарагдовий", "кіт", "говорити"]
    for w in test_words:
        print(f"\n--- {w} ---")
        r = lookup_wiktionary(w)
        if r:
            print(format_wiktionary_entry(r))
        else:
            print("Not found")
