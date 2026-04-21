"""
Wiktionary API integration for Ukrainian dictionary bot.
Fetches rich word data from RUSSIAN Wiktionary (ru.wiktionary.org)
— definitions in Russian, part of speech, pronunciation.
53,000+ Ukrainian words available. Free, no API key needed.
"""

import re
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# Russian Wiktionary MediaWiki API — definitions in Russian!
WIKT_API = "https://ru.wiktionary.org/w/api.php"

# In-memory cache: word → parsed result (keeps last 2000 lookups)
_cache: dict = {}
_MAX_CACHE = 2000


def _clean_wikitext(text: str) -> str:
    """Clean wikitext markup to plain text."""
    # Remove templates like {{помета|...}}, {{выдел|...}} etc
    # But keep content of simple link templates
    text = re.sub(r'\{\{помета\|([^}]*)\}\}', r'(\1)', text)
    text = re.sub(r'\{\{выдел\|([^}]*)\}\}', r'\1', text)
    text = re.sub(r'\{\{итп\}\}', 'и т. п.', text)
    text = re.sub(r'\{\{итд\}\}', 'и т. д.', text)
    # Remove remaining templates but try to keep first param
    text = re.sub(r'\{\{[^|{}]*\|([^|{}]*?)(?:\|[^}]*)?\}\}', r'\1', text)
    text = re.sub(r'\{\{[^}]*\}\}', '', text)
    # Wiki links: [[word|display]] → display, [[word]] → word
    text = re.sub(r'\[\[[^|\]]*\|([^\]]*)\]\]', r'\1', text)
    text = re.sub(r'\[\[([^\]]*)\]\]', r'\1', text)
    # Remove bold/italic
    text = re.sub(r"'{2,3}", '', text)
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def lookup_wiktionary(word: str) -> Optional[dict]:
    """
    Look up a Ukrainian word on Russian Wiktionary.
    Returns dict with:
        - word: str
        - pos: str (part of speech in Ukrainian notation)
        - definitions: list[str] (definitions in RUSSIAN)
        - all_pos: list[dict] with {pos, definitions}
        - pronunciation: str
        - synonyms: list[str]
        - examples: list[str]
    Returns None if word not found.
    """
    word = word.strip().lower()
    if not word or len(word) > 100:
        return None

    # Check cache
    if word in _cache:
        return _cache[word]

    try:
        resp = requests.get(WIKT_API, params={
            'action': 'parse',
            'page': word,
            'format': 'json',
            'prop': 'wikitext',
            'redirects': '1',
        }, timeout=5, headers={
            'User-Agent': 'UkrainianDictionaryBot/1.0 (Telegram bot)'
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

    # Check for error (page not found)
    if 'error' in data:
        _cache_put(word, None)
        return None

    wikitext = data.get('parse', {}).get('wikitext', {}).get('*', '')
    if not wikitext:
        _cache_put(word, None)
        return None

    # Find Ukrainian section
    result = _parse_ukrainian_section(word, wikitext)
    _cache_put(word, result)
    return result


def _parse_ukrainian_section(word: str, wikitext: str) -> Optional[dict]:
    """Parse the Ukrainian language section from ru.wiktionary wikitext."""

    # Ukrainian section markers on ru.wiktionary:
    # = {{-uk-}} =   or   = Украинский =
    ukr_pattern = r'=\s*\{\{-uk-\}\}\s*=|=\s*Украинский\s*='
    ukr_match = re.search(ukr_pattern, wikitext)

    if not ukr_match:
        return None

    # Get text from Ukrainian section start to next language section or end
    section_start = ukr_match.end()
    # Next language section: = {{-xx-}} = or end of text
    next_lang = re.search(r'\n=\s*\{\{-[a-z]+-\}\}\s*=|\n=\s*[А-Я]', wikitext[section_start:])
    if next_lang:
        ukr_text = wikitext[section_start:section_start + next_lang.start()]
    else:
        ukr_text = wikitext[section_start:]

    # Extract POS (Морфологические и синтаксические свойства section)
    pos = _extract_pos(ukr_text)

    # Extract definitions (Значение section)
    definitions = _extract_definitions(ukr_text)

    # Extract pronunciation
    pronunciation = _extract_pronunciation(ukr_text)

    # Extract synonyms
    synonyms = _extract_list_section(ukr_text, 'Синонимы')

    # Extract examples
    examples = _extract_examples(ukr_text)

    if not definitions:
        return None

    all_pos = [{
        "pos": pos,
        "definitions": definitions[:5],
    }]

    return {
        "word": word,
        "pos": pos,
        "definitions": definitions[:5],
        "all_pos": all_pos,
        "pronunciation": pronunciation,
        "synonyms": synonyms[:5],
        "examples": examples[:3],
    }


def _extract_pos(ukr_text: str) -> str:
    """Extract part of speech from the Ukrainian section."""
    # Look for POS templates like {{сущ-uk}}, {{прил-uk}}, {{гл-uk}} etc.
    pos_patterns = {
        r'\{\{сущ[.-]uk': 'ім.',
        r'\{\{сущ uk': 'ім.',
        r'\{\{гл[.-]uk': 'дієсл.',
        r'\{\{глаг[.-]uk': 'дієсл.',
        r'\{\{прил[.-]uk': 'прикм.',
        r'\{\{прил uk': 'прикм.',
        r'\{\{нар[.-]uk': 'присл.',
        r'\{\{нареч[.-]uk': 'присл.',
        r'\{\{мест[.-]uk': 'займ.',
        r'\{\{числ[.-]uk': 'числ.',
        r'\{\{предл[.-]uk': 'прийм.',
        r'\{\{союз[.-]uk': 'спол.',
        r'\{\{част[.-]uk': 'частка',
        r'\{\{межд[.-]uk': 'вигук',
        r'\{\{собств[.-]uk': 'власна назва',
        # Also match Cyrillic descriptions
        r'существительное': 'ім.',
        r'прилагательное': 'прикм.',
        r'глагол': 'дієсл.',
        r'наречие': 'присл.',
        r'местоимение': 'займ.',
        r'числительное': 'числ.',
        r'предлог': 'прийм.',
        r'союз': 'спол.',
        r'частица': 'частка',
        r'междометие': 'вигук',
    }
    for pattern, pos_label in pos_patterns.items():
        if re.search(pattern, ukr_text, re.IGNORECASE):
            return pos_label
    return ""


def _extract_definitions(ukr_text: str) -> list:
    """Extract definitions from the Значение (Meaning) section."""
    # Find the Значение section
    meaning_match = re.search(
        r'={3,4}\s*Значение\s*={3,4}',
        ukr_text
    )
    if not meaning_match:
        # Try alternative: Семантические свойства → look for # lines after it
        sem_match = re.search(r'Семантические свойства', ukr_text)
        if sem_match:
            text_after = ukr_text[sem_match.end():]
        else:
            return []
    else:
        text_after = ukr_text[meaning_match.end():]

    # Get text until next section (===)
    next_section = re.search(r'={3,4}\s*\S', text_after)
    if next_section:
        text_after = text_after[:next_section.start()]

    # Parse numbered definitions (lines starting with #)
    defs = []
    for line in text_after.split('\n'):
        line = line.strip()
        if line.startswith('#') and not line.startswith('##'):
            # Remove the # prefix
            def_text = line.lstrip('#').strip()
            if def_text and def_text != '-':
                cleaned = _clean_wikitext(def_text)
                if cleaned and len(cleaned) > 1:
                    defs.append(cleaned)

    return defs


def _extract_pronunciation(ukr_text: str) -> str:
    """Extract pronunciation/stress info."""
    # Look for IPA in pronunciation section
    pron_match = re.search(r'={3,4}\s*Произношение\s*={3,4}(.*?)={3,4}',
                           ukr_text, re.DOTALL)
    if pron_match:
        pron_text = pron_match.group(1)
        # Extract IPA
        ipa = re.search(r'\{\{МФА[^}]*\|([^}|]+)', pron_text)
        if ipa:
            return ipa.group(1).strip()
        # Alternative IPA format
        ipa2 = re.search(r'\[([^\]]*[ˈˌ][^\]]*)\]', pron_text)
        if ipa2:
            return ipa2.group(1).strip()
    return ""


def _extract_list_section(ukr_text: str, section_name: str) -> list:
    """Extract a list section (synonyms, antonyms, etc.)."""
    pattern = rf'={3,4}\s*{section_name}\s*={3,4}(.*?)(?:={3,4}|\Z)'
    match = re.search(pattern, ukr_text, re.DOTALL)
    if not match:
        return []

    items = []
    for line in match.group(1).split('\n'):
        line = line.strip()
        if line.startswith('#') and not line.startswith('##'):
            item = _clean_wikitext(line.lstrip('#').strip())
            if item and item != '-' and len(item) > 0:
                items.append(item)
    return items


def _extract_examples(ukr_text: str) -> list:
    """Extract usage examples."""
    examples = []
    # Examples are often in {{пример|...}} templates
    for match in re.finditer(r'\{\{пример\|([^}]*)\}\}', ukr_text):
        ex_text = match.group(1).split('|')[0]  # First param is the example text
        if ex_text:
            cleaned = _clean_wikitext(ex_text)
            if cleaned and len(cleaned) > 3:
                examples.append(cleaned)

    # Also look for ## lines (sub-definitions often contain examples)
    if not examples:
        for line in ukr_text.split('\n'):
            line = line.strip()
            if line.startswith('##') and not line.startswith('###'):
                ex = _clean_wikitext(line.lstrip('#').strip())
                if ex and len(ex) > 5:
                    examples.append(ex)

    return examples[:3]


def _cache_put(word: str, value):
    """Add to cache with size limit."""
    global _cache
    _cache[word] = value
    if len(_cache) > _MAX_CACHE:
        keys = list(_cache.keys())[:_MAX_CACHE // 2]
        for k in keys:
            del _cache[k]


def format_wiktionary_entry(result: dict) -> str:
    """Format Wiktionary result as a nice text block for Telegram."""
    if not result:
        return ""

    lines = []

    # Part of speech + definitions
    for pos_data in result["all_pos"]:
        pos = pos_data["pos"]
        defs = pos_data["definitions"]

        if pos:
            lines.append(f"[ {pos} ]")

        for j, d in enumerate(defs[:3], 1):
            lines.append(f"  {j}. {d}")

    # Pronunciation
    if result.get("pronunciation"):
        lines.append(f"🔉 [{result['pronunciation']}]")

    # Synonyms
    if result.get("synonyms"):
        syns = ", ".join(result["synonyms"][:4])
        lines.append(f"🔹 Синоніми: {syns}")

    # Example
    if result.get("examples"):
        lines.append(f"💬 <i>{result['examples'][0]}</i>")

    return "\n".join(lines)


# Quick test
if __name__ == "__main__":
    test_words = ["сучасний", "волошковий", "брунатний", "смарагдовий",
                  "кіт", "говорити", "борщ", "права"]
    for w in test_words:
        print(f"\n--- {w} ---")
        r = lookup_wiktionary(w)
        if r:
            print(format_wiktionary_entry(r))
        else:
            print("Not found")
