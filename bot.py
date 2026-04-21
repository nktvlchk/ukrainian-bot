"""
🇺🇦 Український Словник — Telegram бот
Словник + Підручник української мови (укр ↔ рос)
"""

import os
import re
import json
import logging
import asyncio
import random
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

from deep_translator import GoogleTranslator, MyMemoryTranslator
from gtts import gTTS
import tempfile

from data import TOPICS, BLOCK_CONTENT, CASES_SUMMARY, CONSONANT_CHANGES, DICTIONARY
from big_dictionary import BIG_DICT
from grammar import (
    VERB_GOVERNMENT, decline_noun, conjugate_verb,
    get_gender_from_pos, get_explanation, EXPLANATORY_DICT
)
from wiktionary import lookup_wiktionary, format_wiktionary_entry

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =====================================================================
# CONFIG
# =====================================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Google Translate (free, no API key needed)
translator_ru_uk = GoogleTranslator(source='ru', target='uk')
translator_uk_ru = GoogleTranslator(source='uk', target='ru')
logger.info("✅ Google Translate підключено (безкоштовно, без ключів)")

# Favorites storage (file-based, per user)
FAVORITES_FILE = Path(__file__).parent / "favorites.json"

def load_favorites() -> dict:
    if FAVORITES_FILE.exists():
        return json.loads(FAVORITES_FILE.read_text(encoding="utf-8"))
    return {}

def save_favorites(data: dict):
    FAVORITES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================
# DICTIONARY — merge all sources
# =====================================================================

def _convert_big(entry_tuple):
    """Convert BIG_DICT tuple to DICTIONARY-style dict."""
    return {"pos": entry_tuple[0], "translation": entry_tuple[1], "example": entry_tuple[2]}

# Merge all dictionaries into one lookup (Ukrainian → Russian)
ALL_WORDS = {}
for w, e in BIG_DICT.items():
    ALL_WORDS[w] = _convert_big(e)
for w, e in DICTIONARY.items():
    ALL_WORDS[w] = e  # DICTIONARY entries override BIG_DICT

# Build reverse dictionary (Russian → Ukrainian)
REVERSE_DICT = {}
for ukr_word, entry in ALL_WORDS.items():
    rus_translation = entry.get("translation", "")
    parts = re.split(r'[/,;]', rus_translation)
    for part in parts:
        rus_key = part.strip().lower()
        if rus_key:
            if rus_key not in REVERSE_DICT:
                REVERSE_DICT[rus_key] = []
            REVERSE_DICT[rus_key].append((ukr_word, entry))
            # Also index by first word of multi-word translations
            first_word = rus_key.split()[0] if ' ' in rus_key else None
            if first_word and len(first_word) >= 3:
                if first_word not in REVERSE_DICT:
                    REVERSE_DICT[first_word] = []
                REVERSE_DICT[first_word].append((ukr_word, entry))

logger.info(f"📚 Словник завантажено: {len(ALL_WORDS)} укр слів, {len(REVERSE_DICT)} рос ключів")


# =====================================================================
# LANGUAGE DETECTION
# =====================================================================

UKR_ONLY = set("іїєґ")
RUS_ONLY = set("ыэёъ")

def detect_language(text: str) -> str:
    """Detect if text is Ukrainian or Russian. Returns 'uk', 'ru', or 'unknown'."""
    t = text.lower()
    has_ukr = any(ch in UKR_ONLY for ch in t)
    has_rus = any(ch in RUS_ONLY for ch in t)

    if has_ukr and not has_rus:
        return "uk"
    if has_rus and not has_ukr:
        return "ru"

    # For single words — check dictionaries
    words = t.split()
    if len(words) == 1:
        w = words[0]
        if w in ALL_WORDS:
            return "uk"
        if w in REVERSE_DICT:
            return "ru"

    # Heuristic: common Russian-only patterns
    rus_patterns = ["ого", "ему", "ться", "ешь", "ёт", "щ", "ый", "ий"]
    ukr_patterns = ["ться", "ють", "ємо", "ішь", "ього"]

    rus_score = sum(1 for p in rus_patterns if p in t)
    ukr_score = sum(1 for p in ukr_patterns if p in t)

    if rus_score > ukr_score:
        return "ru"
    if ukr_score > rus_score:
        return "uk"

    return "unknown"


# =====================================================================
# FUZZY LOOKUP
# =====================================================================

def fuzzy_lookup_ukr(word: str) -> tuple:
    """Try to find a Ukrainian word in dictionary by stripping suffixes."""
    if word in ALL_WORDS:
        return word, ALL_WORDS[word]

    suffixes = [
        "ів", "ям", "ях", "ами", "ями", "ою", "ею", "єю",
        "ові", "еві", "єві", "ом", "ем", "єм",
        "ів", "їв", "ей",
        "ці", "зі", "сі",
        "у", "ю", "і", "ї", "и", "е", "є", "о", "а", "я",
        "ку", "ця",
        "ся",
        "ти", "ть",
        "ую", "юю", "єш", "еш", "ає", "є", "емо", "ємо", "ете", "єте", "ють", "ять",
        "ла", "ло", "ли", "в",
    ]

    for suffix in sorted(suffixes, key=len, reverse=True):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            stem = word[:-len(suffix)]
            for ending in ["", "а", "я", "о", "е", "і", "и", "ь", "й", "ти", "ка", "ок"]:
                candidate = stem + ending
                if candidate in ALL_WORDS:
                    return candidate, ALL_WORDS[candidate]

    # Strict partial match — at least 4 prefix chars and >70% of word
    if len(word) >= 5:
        best_match = None
        best_score = 0
        for dict_word, entry in ALL_WORDS.items():
            if len(dict_word) >= 5 and abs(len(word) - len(dict_word)) <= 2:
                prefix_len = 0
                for a, b in zip(word, dict_word):
                    if a == b:
                        prefix_len += 1
                    else:
                        break
                if prefix_len >= 4 and prefix_len >= len(word) * 0.7:
                    if prefix_len > best_score:
                        best_score = prefix_len
                        best_match = (dict_word, entry)
        if best_match:
            return best_match

    return None, None


def fuzzy_lookup_rus(word: str) -> list:
    """Try to find a Russian word in reverse dictionary."""
    if word in REVERSE_DICT:
        return REVERSE_DICT[word]

    rus_suffixes = [
        "ами", "ями", "ов", "ев", "ей",
        "ом", "ем", "ой", "ей",
        "ам", "ям", "ах", "ях",
        "у", "ю", "а", "я", "е", "и", "ы", "о",
        "ть", "ся",
        "ую", "ешь", "ет", "ем", "ете", "ют", "ят",
        "ал", "ала", "ало", "али", "ил", "ила", "ило", "или",
    ]

    for suffix in sorted(rus_suffixes, key=len, reverse=True):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            stem = word[:-len(suffix)]
            for ending in ["", "а", "я", "о", "е", "и", "ь", "й", "ть", "ок", "ка", "ый", "ий", "ой"]:
                candidate = stem + ending
                if candidate in REVERSE_DICT:
                    return REVERSE_DICT[candidate]

    # Strict partial match
    if len(word) >= 5:
        best_match = None
        best_score = 0
        for rus_key, entries in REVERSE_DICT.items():
            if len(rus_key) >= 5 and abs(len(word) - len(rus_key)) <= 2:
                prefix_len = 0
                for a, b in zip(word, rus_key):
                    if a == b:
                        prefix_len += 1
                    else:
                        break
                if prefix_len >= 4 and prefix_len >= len(word) * 0.7:
                    if prefix_len > best_score:
                        best_score = prefix_len
                        best_match = entries
        if best_match:
            return best_match

    return []


# =====================================================================
# GOOGLE TRANSLATE — fallback for words/phrases not in dictionary
# =====================================================================

def _do_translate(text: str, source: str, target: str) -> str:
    """Synchronous translate helper — tries Google, then MyMemory."""
    # 1) Google Translate
    try:
        result = GoogleTranslator(source=source, target=target).translate(text)
        if result and result.lower().strip() != text.lower().strip():
            return result
    except Exception as e:
        logger.warning(f"Google Translate ({source}→{target}) error: {e}")

    # 2) MyMemory fallback (different translation engine)
    try:
        src_code = source if source != 'auto' else 'uk'
        result = MyMemoryTranslator(source=src_code, target=target).translate(text)
        if result and result.lower().strip() != text.lower().strip():
            return result
    except Exception as e:
        logger.warning(f"MyMemory ({source}→{target}) error: {e}")

    return ""


async def smart_translate(text: str, lang: str) -> tuple:
    """Smart translate: auto-detects direction, tries multiple engines.
    Returns (translation, tts_text) where tts_text is always Ukrainian."""
    loop = asyncio.get_event_loop()
    word = text.lower().strip()

    if lang == "ru":
        # Russian → Ukrainian
        # Try: auto→uk, then ru→uk
        result = await loop.run_in_executor(None, _do_translate, text, 'auto', 'uk')
        if not result:
            result = await loop.run_in_executor(None, _do_translate, text, 'ru', 'uk')
        return result, result or ""  # TTS = Ukrainian translation
    else:
        # Ukrainian (or unknown) → Russian
        # Try: auto→ru, then uk→ru
        result = await loop.run_in_executor(None, _do_translate, text, 'auto', 'ru')
        if not result:
            result = await loop.run_in_executor(None, _do_translate, text, 'uk', 'ru')

        if result:
            return result, text  # TTS = original Ukrainian text

        # Still nothing? Maybe it's actually Russian → try auto→uk
        result = await loop.run_in_executor(None, _do_translate, text, 'auto', 'uk')
        if result:
            return result, result  # TTS = Ukrainian translation

        return "", ""


async def google_translate(text: str, direction: str) -> str:
    """Legacy translate wrapper for /voice and other commands."""
    try:
        if direction == "ru_uk":
            source, target = 'ru', 'uk'
        elif direction == "uk_ru":
            source, target = 'uk', 'ru'
        elif direction == "auto_ru":
            source, target = 'auto', 'ru'
        elif direction == "auto_uk":
            source, target = 'auto', 'uk'
        else:
            source, target = 'auto', 'ru'

        # Google Translate limit ~5000 chars. Split long texts.
        if len(text) <= 4500:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _do_translate, text, source, target
            )
            return result or ""
        else:
            parts = []
            current = ""
            for line in text.replace('. ', '.\n').split('\n'):
                if len(current) + len(line) < 4500:
                    current += line + '\n'
                else:
                    if current:
                        parts.append(current.strip())
                    current = line + '\n'
            if current.strip():
                parts.append(current.strip())

            results = []
            for part in parts:
                r = await asyncio.get_event_loop().run_in_executor(
                    None, _do_translate, part, source, target
                )
                if r:
                    results.append(r)
            return '\n'.join(results)
    except Exception as e:
        logger.error(f"Translate error: {e}")
        return ""


# =====================================================================
# /start
# =====================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🇺🇦 <b>Український Словник</b>\n\n"
        "Вітаю! Я бот для вивчення української мови.\n\n"
        "<b>Що я вмію:</b>\n\n"
        "📖 <b>Словник</b> — надішли слово або фразу, я перекладу\n"
        "📚 /textbook — підручник (14 тем граматики)\n"
        "📋 /cases — таблиця відмінків\n"
        "📝 /decline <i>слово</i> — відмінювання по 7 відмінках\n"
        "🔄 /conjugate <i>дієслово</i> — дієвідмінювання\n"
        "📗 /explain <i>слово</i> — тлумачний словник (укр-укр)\n"
        "🗣 /voice <i>текст</i> — озвучення українською\n"
        "⭐ /favorites — збережені слова\n"
        "Надішліть будь-яке слово або фразу! 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# /help
# =====================================================================

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Як користуватися ботом</b>\n\n"
        "1️⃣ Надішліть <b>слово</b> — переклад (укр↔рос) + граматика\n"
        "2️⃣ Надішліть <b>фразу / речення</b> — автоматичний переклад\n"
        "3️⃣ /textbook — підручник (14 тем)\n"
        "4️⃣ /cases — таблиця відмінків\n"
        "5️⃣ /decline <i>слово</i> — відмінювання іменника\n"
        "6️⃣ /conjugate <i>дієслово</i> — дієвідмінювання\n"
        "7️⃣ /explain <i>слово</i> — тлумачний словник\n"
        "8️⃣ /voice <i>текст</i> — озвучення 🔊\n"
        "9️⃣ /favorites — збережені слова\n\n"
        "🔤 Бот автоматично визначає мову.\n"
        f"📚 {len(ALL_WORDS)} слів в базі + Google Translate + Wiktionary (53,000+ слів)."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# /textbook
# =====================================================================

async def cmd_textbook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for tid, topic in sorted(TOPICS.items()):
        keyboard.append([
            InlineKeyboardButton(topic["title"], callback_data=f"topic_{tid}")
        ])
    keyboard.append([
        InlineKeyboardButton("📋 Зведена таблиця відмінків", callback_data="summary_cases")
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 Чергування приголосних", callback_data="summary_consonants")
    ])
    await update.message.reply_text(
        "📚 <b>Підручник української мови</b>\n\nОберіть тему:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================================================================
# /cases
# =====================================================================

async def cmd_cases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📌 Знахідний (кого? що?)", callback_data="block_9_accusative")],
        [InlineKeyboardButton("📢 Кличний (звертання)", callback_data="block_9_vocative")],
        [InlineKeyboardButton("📍 Місцевий (на/у кому?)", callback_data="block_10_locative")],
        [InlineKeyboardButton("🔧 Орудний (ким? чим?)", callback_data="block_11_instrumental")],
        [InlineKeyboardButton("📐 Родовий (кого? чого?)", callback_data="block_12_genitive")],
        [InlineKeyboardButton("🎁 Давальний (кому? чому?)", callback_data="block_13_dative")],
        [InlineKeyboardButton("🔄 Чергування приголосних", callback_data="summary_consonants")],
    ]
    await update.message.reply_text(
        CASES_SUMMARY,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================================================================
# /favorites
# =====================================================================

async def cmd_favorites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    favs = load_favorites().get(uid, [])
    if not favs:
        await update.message.reply_text(
            "⭐ У вас поки немає збережених слів.\n"
            "Щоб додати — надішліть слово, потім натисніть «⭐ Додати в обране»."
        )
        return

    text = "⭐ <b>Ваші збережені слова:</b>\n\n"
    for i, word in enumerate(favs[-30:], 1):
        text += f"{i}. {word}\n"

    keyboard = [[InlineKeyboardButton("🗑 Очистити обране", callback_data="fav_clear")]]
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================================================================
# /decline — відмінювач іменників
# =====================================================================

async def cmd_decline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "📝 <b>Відмінювач іменників</b>\n\n"
            "Використання: /decline <i>слово</i>\n"
            "Приклад: /decline студент",
            parse_mode=ParseMode.HTML
        )
        return

    word = args[1].strip().lower()

    # Try to find gender from dictionary
    gender = ""
    if word in ALL_WORDS:
        gender = get_gender_from_pos(ALL_WORDS[word]["pos"])
    if not gender:
        # Guess from word ending
        if word.endswith(("а", "я")) and not word.endswith(("ння", "ття")):
            gender = "ж"
        elif word.endswith(("о", "е")) or word.endswith(("ння", "ття")):
            gender = "с"
        else:
            gender = "ч"

    cases = decline_noun(word, gender)
    gender_name = {"ч": "чоловічий", "ж": "жіночий", "с": "середній"}.get(gender, "?")

    text = f"📝 <b>Відмінювання: {word}</b>\n"
    text += f"Рід: {gender_name}\n\n"
    for case_name, form in cases.items():
        text += f"<b>{case_name}:</b> {form}\n"

    text += "\n<i>⚠️ Деякі форми можуть бути наближеними</i>"

    keyboard = [[InlineKeyboardButton("🔊 Озвучити", callback_data=f"tts_{word}")]]
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================================================================
# /conjugate — дієвідмінювач
# =====================================================================

async def cmd_conjugate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "🔄 <b>Дієвідмінювач</b>\n\n"
            "Використання: /conjugate <i>дієслово</i>\n"
            "Приклад: /conjugate читати",
            parse_mode=ParseMode.HTML
        )
        return

    word = args[1].strip().lower()
    result = conjugate_verb(word)

    if not result:
        await update.message.reply_text(
            f"⚠️ <b>{word}</b> — не знайдено інфінітив.\n"
            "Введіть дієслово в формі інфінітива (-ти): /conjugate читати",
            parse_mode=ParseMode.HTML
        )
        return

    text = f"🔄 <b>Дієвідмінювання: {word}</b>\n\n"

    # Present tense
    text += "<b>Теперішній час:</b>\n"
    for person, form in result["present"].items():
        text += f"  {person} — <b>{form}</b>\n"

    # Past tense
    text += "\n<b>Минулий час:</b>\n"
    for person, form in result["past"].items():
        text += f"  {person} — <b>{form}</b>\n"

    # Imperative
    text += "\n<b>Наказовий спосіб:</b>\n"
    for person, form in result["imperative"].items():
        text += f"  {person} — <b>{form}</b>\n"

    # Verb government
    gov = VERB_GOVERNMENT.get(word)
    if gov:
        text += f"\n📌 <b>Керування:</b> {gov['case']}\n"
        text += f"Питання: {gov['question']}\n"
        text += f"Приклад: {gov['example']}\n"
        if gov.get("note"):
            text += f"💡 {gov['note']}\n"

    text += "\n<i>⚠️ Деякі форми можуть бути наближеними</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# /explain — тлумачний словник
# =====================================================================

async def cmd_explain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "📗 <b>Тлумачний словник</b>\n\n"
            "Використання: /explain <i>слово</i>\n"
            "Приклад: /explain борщ",
            parse_mode=ParseMode.HTML
        )
        return

    word = args[1].strip().lower()
    explanation = get_explanation(word)

    if explanation:
        text = f"📗 <b>{word}</b>\n\n"
        text += f"📝 {explanation}\n"

        # Also show translation if available
        if word in ALL_WORDS:
            entry = ALL_WORDS[word]
            text += f"\n🔹 Рос.: {entry['translation']}"

        keyboard = [
            [InlineKeyboardButton("🔊 Озвучити", callback_data=f"tts_{word}")],
            [InlineKeyboardButton("⭐ Додати в обране", callback_data=f"fav_add_{word}")],
        ]
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Try Google Translate as explanatory fallback (uk→uk doesn't exist, so show what we have)
        text = f"📗 Слово <b>{word}</b> поки немає в тлумачному словнику.\n\n"
        if word in ALL_WORDS:
            entry = ALL_WORDS[word]
            text += f"📖 {word} [ {entry['pos']} ] — {entry['translation']}\n"
            if entry.get("example"):
                text += f"💬 <i>{entry['example']}</i>\n"
        text += f"\n📚 У тлумачному словнику {len(EXPLANATORY_DICT)} слів."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# /voice — озвучення (text-to-speech)
# =====================================================================

async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "🔊 <b>Озвучення</b>\n\n"
            "Використання: /voice <i>текст українською</i>\n"
            "Приклад: /voice Доброго ранку, як справи?",
            parse_mode=ParseMode.HTML
        )
        return

    text_to_speak = args[1].strip()
    await send_tts(update, text_to_speak)


async def send_tts(update_or_query, text: str, lang: str = "uk"):
    """Generate and send TTS audio message."""
    try:
        # Determine if this is an Update or CallbackQuery
        if hasattr(update_or_query, 'message'):
            message = update_or_query.message
        elif hasattr(update_or_query, 'callback_query'):
            message = update_or_query.callback_query.message
        else:
            message = update_or_query

        tts = gTTS(text=text, lang=lang, slow=False)
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            tts.save(f.name)
            f.seek(0)
            await message.reply_voice(
                voice=open(f.name, 'rb'),
                caption=f"🔊 {text[:100]}"
            )
        os.unlink(f.name)
    except Exception as e:
        logger.error(f"TTS error: {e}")
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(
                f"⚠️ Помилка озвучення: {str(e)[:200]}",
                parse_mode=ParseMode.HTML
            )


# =====================================================================
# /stats — аналітика бота
# =====================================================================

STATS_FILE = Path(__file__).parent / "stats.json"

def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    return {"users": {}, "words": {}, "commands": {}, "total_messages": 0}

def save_stats(data: dict):
    STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def track_event(user_id: int, username: str, event_type: str, value: str = ""):
    """Track user activity for analytics."""
    stats = load_stats()
    uid = str(user_id)

    # Track user
    if uid not in stats["users"]:
        stats["users"][uid] = {"name": username, "count": 0, "first_seen": ""}
    stats["users"][uid]["count"] += 1
    stats["users"][uid]["name"] = username or uid

    # Track words
    if event_type == "word" and value:
        w = value.lower()
        stats["words"][w] = stats["words"].get(w, 0) + 1

    # Track commands
    if event_type == "command" and value:
        stats["commands"][value] = stats["commands"].get(value, 0) + 1

    stats["total_messages"] = stats.get("total_messages", 0) + 1
    save_stats(stats)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = load_stats()

    total_users = len(stats.get("users", {}))
    total_messages = stats.get("total_messages", 0)
    words = stats.get("words", {})
    commands = stats.get("commands", {})

    text = "📊 <b>Аналітика бота</b>\n\n"
    text += f"👥 Користувачів: <b>{total_users}</b>\n"
    text += f"💬 Повідомлень: <b>{total_messages}</b>\n\n"

    # Top users
    users = stats.get("users", {})
    if users:
        sorted_users = sorted(users.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
        text += "<b>👤 Топ користувачів:</b>\n"
        for i, (uid, udata) in enumerate(sorted_users, 1):
            text += f"  {i}. {udata.get('name', uid)} — {udata['count']} повідомлень\n"
        text += "\n"

    # Top words
    if words:
        sorted_words = sorted(words.items(), key=lambda x: x[1], reverse=True)[:15]
        text += "<b>🔤 Топ запитів:</b>\n"
        for i, (w, cnt) in enumerate(sorted_words, 1):
            text += f"  {i}. {w} — {cnt}×\n"
        text += "\n"

    # Command usage
    if commands:
        text += "<b>⚡ Команди:</b>\n"
        for cmd, cnt in sorted(commands.items(), key=lambda x: x[1], reverse=True):
            text += f"  /{cmd} — {cnt}×\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# =====================================================================
# CALLBACK HANDLER
# =====================================================================

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Topic list ---
    if data.startswith("topic_"):
        tid = int(data.split("_")[1])
        topic = TOPICS[tid]
        keyboard = []
        for block_key, block_title in topic["blocks"].items():
            keyboard.append([
                InlineKeyboardButton(block_title, callback_data=f"block_{tid}_{block_key}")
            ])
        keyboard.append([InlineKeyboardButton("⬅️ Назад до тем", callback_data="back_topics")])
        await query.edit_message_text(
            f"{topic['title']}\n\nОберіть розділ:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Block content ---
    elif data.startswith("block_"):
        parts = data.split("_", 2)
        tid = int(parts[1])
        block_key = parts[2]
        content = BLOCK_CONTENT.get((tid, block_key), "❌ Контент не знайдено.")
        if len(content) > 4000:
            content = content[:3990] + "\n\n<i>(текст скорочено)</i>"
        keyboard = [
            [InlineKeyboardButton(f"⬅️ Назад до {TOPICS[tid]['short']}", callback_data=f"topic_{tid}")],
            [InlineKeyboardButton("📚 Всі теми", callback_data="back_topics")],
        ]
        try:
            await query.edit_message_text(
                content, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.message.reply_text(
                content, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    elif data == "summary_cases":
        keyboard = [[InlineKeyboardButton("📚 Всі теми", callback_data="back_topics")]]
        await query.edit_message_text(
            CASES_SUMMARY, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "summary_consonants":
        keyboard = [[InlineKeyboardButton("📚 Всі теми", callback_data="back_topics")]]
        await query.edit_message_text(
            CONSONANT_CHANGES, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "back_topics":
        keyboard = []
        for tid, topic in sorted(TOPICS.items()):
            keyboard.append([
                InlineKeyboardButton(topic["title"], callback_data=f"topic_{tid}")
            ])
        keyboard.append([
            InlineKeyboardButton("📋 Зведена таблиця відмінків", callback_data="summary_cases")
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 Чергування приголосних", callback_data="summary_consonants")
        ])
        await query.edit_message_text(
            "📚 <b>Підручник української мови</b>\n\nОберіть тему:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # --- Favorites ---
    elif data.startswith("fav_") and data != "fav_clear":
        word = data[4:]
        uid = str(update.effective_user.id)
        favs = load_favorites()
        user_favs = favs.get(uid, [])
        if word not in user_favs:
            user_favs.append(word)
            favs[uid] = user_favs
            save_favorites(favs)
            await query.answer("⭐ Додано в обране!", show_alert=True)
        else:
            await query.answer("Вже в обраному!", show_alert=True)

    elif data == "fav_clear":
        uid = str(update.effective_user.id)
        favs = load_favorites()
        favs[uid] = []
        save_favorites(favs)
        await query.edit_message_text("🗑 Обране очищено.")

    # --- TTS callback ---
    elif data.startswith("tts_"):
        tts_key = data[4:]
        tts_text = TTS_CACHE.get(tts_key, tts_key)  # fallback to key itself if not in cache
        try:
            tts_obj = gTTS(text=tts_text, lang='uk', slow=False)
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                tts_obj.save(f.name)
                await query.message.reply_voice(
                    voice=open(f.name, 'rb'),
                    caption=tts_text[:100]
                )
            os.unlink(f.name)
        except Exception as e:
            await query.message.reply_text(f"Помилка озвучення: {str(e)[:200]}")

    # --- Verb government callback ---
    elif data.startswith("gov_"):
        verb = data[4:]
        gov = VERB_GOVERNMENT.get(verb)
        if gov:
            text = f"📌 <b>Керування дієслова «{verb}»</b>\n\n"
            text += f"Відмінок: <b>{gov['case']}</b>\n"
            text += f"Питання: {gov['question']}\n"
            text += f"Приклад: {gov['example']}\n"
            if gov.get("note"):
                text += f"\n💡 {gov['note']}"
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await query.message.reply_text(f"Інформація про керування «{verb}» не знайдена.")

    # --- Conjugation callback ---
    elif data.startswith("conj_"):
        verb = data[5:]
        result = conjugate_verb(verb)
        if result:
            text = f"🔄 <b>{verb}</b>\n\n<b>Теперішній:</b>\n"
            for p, f in result["present"].items():
                text += f"  {p} — <b>{f}</b>\n"
            text += "\n<b>Минулий:</b>\n"
            for p, f in result["past"].items():
                text += f"  {p} — <b>{f}</b>\n"
            text += "\n<b>Наказовий:</b>\n"
            for p, f in result["imperative"].items():
                text += f"  {p} — <b>{f}</b>\n"
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await query.message.reply_text(f"⚠️ Не вдалося дієвідмінити «{verb}».")

    # --- Declension callback ---
    elif data.startswith("decl_"):
        word = data[5:]
        gender = ""
        if word in ALL_WORDS:
            gender = get_gender_from_pos(ALL_WORDS[word]["pos"])
        if not gender:
            if word.endswith(("а", "я")) and not word.endswith(("ння", "ття")):
                gender = "ж"
            elif word.endswith(("о", "е")) or word.endswith(("ння", "ття")):
                gender = "с"
            else:
                gender = "ч"
        cases = decline_noun(word, gender)
        gender_name = {"ч": "чоловічий", "ж": "жіночий", "с": "середній"}.get(gender, "?")
        text = f"📝 <b>Відмінювання: {word}</b> ({gender_name})\n\n"
        for case_name, form in cases.items():
            text += f"<b>{case_name}:</b> {form}\n"
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)



# =====================================================================
# WORD / PHRASE HANDLER
# =====================================================================

# Store TTS texts to avoid callback_data overflow (64 byte limit)
TTS_CACHE = {}
_tts_counter = 0

def tts_store(text: str) -> str:
    """Store text for TTS and return short callback key."""
    global _tts_counter
    _tts_counter += 1
    key = f"t{_tts_counter}"
    TTS_CACHE[key] = text
    # Keep cache from growing forever
    if len(TTS_CACHE) > 500:
        oldest = list(TTS_CACHE.keys())[:250]
        for k in oldest:
            del TTS_CACHE[k]
    return key


def build_word_buttons(word: str, entry: dict = None) -> list:
    """Build inline keyboard buttons for a word result."""
    buttons = []
    tts_key = tts_store(word)
    row1 = [
        InlineKeyboardButton("Озвучити", callback_data=f"tts_{tts_key}"),
        InlineKeyboardButton("В обране", callback_data=f"fav_{word[:50]}"),
    ]
    buttons.append(row1)

    row2 = []
    if entry:
        pos = entry.get("pos", "").lower()
        if "ім." in pos:
            row2.append(InlineKeyboardButton("Відмінювання", callback_data=f"decl_{word[:25]}"))
        if "дієсл" in pos:
            row2.append(InlineKeyboardButton("Дієвідмінювання", callback_data=f"conj_{word[:25]}"))
        if word in VERB_GOVERNMENT:
            row2.append(InlineKeyboardButton("Керування", callback_data=f"gov_{word[:25]}"))
    if row2:
        buttons.append(row2)

    return buttons


async def handle_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()
    word = raw_text.lower()

    if not word or len(word) > 4000:
        return

    # Track analytics
    user = update.effective_user
    username = user.first_name or user.username or str(user.id)
    track_event(user.id, username, "word", word[:50])

    # ── Detect language ──
    lang = detect_language(raw_text)

    # ── Translate (Google Translate + MyMemory fallback) ──
    entry = None
    found_word = None
    translation, tts_text = await smart_translate(raw_text, lang)

    # ── Wiktionary enrichment (async, for single words) ──
    wikt_data = None
    wikt_text = ""
    if ' ' not in word and len(word) >= 2:
        try:
            # Look up word in Wiktionary (53,000+ Ukrainian words)
            wikt_data = await asyncio.get_event_loop().run_in_executor(
                None, lookup_wiktionary, word
            )
            # If not found, try with translation (for Russian words → Ukrainian)
            if not wikt_data and translation:
                trans_word = translation.lower().strip().split()[0] if translation else ""
                if trans_word and trans_word != word:
                    wikt_data = await asyncio.get_event_loop().run_in_executor(
                        None, lookup_wiktionary, trans_word
                    )
            if wikt_data:
                wikt_text = format_wiktionary_entry(wikt_data)
        except Exception as e:
            logger.warning(f"Wiktionary lookup error: {e}")

    # ── Build response ──
    if translation and translation.lower().strip() != word:
        text = f"<b>{raw_text}</b>\n\n{translation}\n"

        # Bonus: local dictionary grammar info
        if ' ' not in word:
            found_word, entry = fuzzy_lookup_ukr(word)
            if not entry:
                trans_lower = translation.lower().strip()
                found_word, entry = fuzzy_lookup_ukr(trans_lower)
            if entry:
                # Only show local dict POS if Wiktionary didn't provide it
                if not wikt_text:
                    text += f"\n[ {entry['pos']} ]"
                if entry.get("example"):
                    text += f"\n<i>{entry['example']}</i>"
                expl = get_explanation(found_word)
                if expl:
                    text += f"\n<i>{expl}</i>"
                text += "\n"

        # Add Wiktionary data (rich definitions from 53k+ word database)
        if wikt_text:
            text += f"\n📖 <b>Wiktionary:</b>\n{wikt_text}\n"

        keyboard = []
        if tts_text:
            tts_key = tts_store(tts_text)
            keyboard.append([InlineKeyboardButton("Озвучити", callback_data=f"tts_{tts_key}")])
        keyboard.append([InlineKeyboardButton("В обране", callback_data=f"fav_{word[:50]}")])

        # Grammar buttons — use Wiktionary POS if local dict doesn't have it
        effective_pos = ""
        effective_word = word
        if ' ' not in word:
            if entry:
                effective_pos = entry.get("pos", "").lower()
                effective_word = found_word or word
            elif wikt_data:
                effective_pos = wikt_data.get("pos", "").lower()

        if effective_pos:
            row = []
            if "ім." in effective_pos or effective_pos in ("noun", "ім."):
                row.append(InlineKeyboardButton("Відмінювання", callback_data=f"decl_{effective_word[:25]}"))
            if "дієсл" in effective_pos or effective_pos in ("verb", "дієсл."):
                row.append(InlineKeyboardButton("Дієвідмінювання", callback_data=f"conj_{effective_word[:25]}"))
            if effective_word in VERB_GOVERNMENT:
                row.append(InlineKeyboardButton("Керування", callback_data=f"gov_{effective_word[:25]}"))
            if row:
                keyboard.append(row)

        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Translation failed — still try Wiktionary
        if wikt_text:
            text = f"<b>{raw_text}</b>\n\n📖 <b>Wiktionary:</b>\n{wikt_text}\n"
            keyboard = []
            tts_key = tts_store(word)
            keyboard.append([InlineKeyboardButton("Озвучити", callback_data=f"tts_{tts_key}")])
            keyboard.append([InlineKeyboardButton("В обране", callback_data=f"fav_{word[:50]}")])
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                f"Не вдалося перекласти <b>{raw_text}</b>.\n"
                "Спробуйте інше написання або додайте контекст.",
                parse_mode=ParseMode.HTML
            )


# =====================================================================
# MAIN
# =====================================================================

def main():
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN not set!")
        print("Create .env file with TELEGRAM_BOT_TOKEN=your_token")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("textbook", cmd_textbook))
    app.add_handler(CommandHandler("cases", cmd_cases))
    app.add_handler(CommandHandler("decline", cmd_decline))
    app.add_handler(CommandHandler("conjugate", cmd_conjugate))
    app.add_handler(CommandHandler("explain", cmd_explain))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_word))

    print("🇺🇦 Український Словник бот запущено!")
    print(f"📚 Словник: {len(ALL_WORDS)} слів")
    print(f"📗 Тлумачний словник: {len(EXPLANATORY_DICT)} слів")
    print(f"📌 Керування дієслів: {len(VERB_GOVERNMENT)} дієслів")
    print("🔄 Google Translate для фраз і невідомих слів")
    print("📖 Wiktionary API: 53,000+ укр слів з граматикою")
    print("🔊 Озвучення (gTTS)")
    print("📊 Аналітика: /stats")
    print("Команди: /start /help /textbook /cases /decline /conjugate /explain /voice /favorites /stats")
    app.run_polling()


if __name__ == "__main__":
    main()
