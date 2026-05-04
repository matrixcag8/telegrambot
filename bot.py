#!/usr/bin/env python3
"""NutriBob — Diario alimentare Telegram bot."""

import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from foods import FOODS, UNIT_GRAMS, PORTION_WORDS, PORTION_OVERRIDES

# ── CONFIG ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set via environment variable, never hardcoded
DB_PATH = Path(__file__).parent / "diary.db"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── DATABASE ──────────────────────────────────────────────────────────────────


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                food          TEXT    NOT NULL,
                calories      INTEGER NOT NULL,
                quantity_desc TEXT,
                created_at    TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                weight_kg  REAL    NOT NULL,
                goal       TEXT    NOT NULL DEFAULT 'main',
                updated_at TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weight_entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                weight_kg  REAL    NOT NULL,
                created_at TEXT    NOT NULL
            )
            """
        )
        # migrate: add goal column if it doesn't exist yet
        try:
            conn.execute("ALTER TABLE users ADD COLUMN goal TEXT NOT NULL DEFAULT 'main'")
        except Exception:
            pass


# kcal multipliers per goal (based on TDEE for sedentary/light activity)
_GOAL_MULTIPLIER = {
    "cut":  28,   # ~20% deficit
    "main": 33,   # maintenance
    "bulk": 38,   # ~15% surplus
}
_GOAL_LABELS = {
    "cut":  "✂️ Cut (dimagrire)",
    "main": "⚖️ Mantenimento",
    "bulk": "💪 Bulk (massa)",
}
_GOAL_DESC = {
    "cut":  "deficit calorico — brucia più di quello che mangi",
    "main": "mangiare quanto consumi — peso stabile",
    "bulk": "surplus calorico — costruisci massa muscolare",
}


def db_set_weight(user_id: int, weight_kg: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, weight_kg, goal, updated_at)
            VALUES (?, ?, 'main', ?)
            ON CONFLICT(user_id) DO UPDATE SET weight_kg=excluded.weight_kg, updated_at=excluded.updated_at
            """,
            (user_id, weight_kg, _now_str()),
        )


def db_add_weight_entry(user_id: int, weight_kg: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO weight_entries (user_id, weight_kg, created_at) VALUES (?,?,?)",
            (user_id, weight_kg, _now_str()),
        )


def db_get_weight_history(user_id: int, limit: int = 14) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT created_at, weight_kg
            FROM weight_entries
            WHERE user_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def db_set_goal(user_id: int, goal: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET goal=?, updated_at=? WHERE user_id=?",
            (goal, _now_str(), user_id),
        )


def db_get_profile(user_id: int) -> Optional[tuple]:
    """Returns (weight_kg, goal) or None."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT weight_kg, goal FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row if row else None


def db_get_weight(user_id: int) -> Optional[float]:
    profile = db_get_profile(user_id)
    return profile[0] if profile else None


def _calorie_target(weight_kg: Optional[float], goal: str = "main") -> int:
    """Daily kcal target based on weight and goal."""
    if weight_kg:
        return round(weight_kg * _GOAL_MULTIPLIER.get(goal, 33))
    return 2000


def _goal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ Cut — perdere peso",     callback_data="goal_cut")],
        [InlineKeyboardButton("⚖️ Mantenimento",          callback_data="goal_main")],
        [InlineKeyboardButton("💪 Bulk — aumentare massa", callback_data="goal_bulk")],
    ])


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_add_entry(user_id: int, food: str, calories: int, quantity_desc: str, entry_date: Optional[date] = None) -> None:
    if entry_date is None:
        ts = _now_str()
    else:
        ts = entry_date.strftime("%Y-%m-%d") + " 12:00:00"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO entries (user_id, food, calories, quantity_desc, created_at) VALUES (?,?,?,?,?)",
            (user_id, food, calories, quantity_desc, ts),
        )


def db_get_day(user_id: int, day: date) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT id, food, calories, quantity_desc, created_at
            FROM entries
            WHERE user_id = ? AND date(created_at) = ?
            ORDER BY created_at
            """,
            (user_id, day.isoformat()),
        ).fetchall()


def db_delete_last(user_id: int) -> Optional[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, food, calories FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM entries WHERE id = ?", (row[0],))
            return row
    return None


def db_delete_day(user_id: int, day: date) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM entries WHERE user_id = ? AND date(created_at) = ?",
            (user_id, day.isoformat()),
        )
        return cur.rowcount


def db_weekly(user_id: int) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """
            SELECT date(created_at) AS day, SUM(calories) AS total, COUNT(*) AS n
            FROM entries
            WHERE user_id = ?
              AND date(created_at) >= date('now', '-6 days')
            GROUP BY day
            ORDER BY day DESC
            """,
            (user_id,),
        ).fetchall()


# ── FOOD PARSING ──────────────────────────────────────────────────────────────

_PREFIX_RE = re.compile(
    r"^(ho mangiato|ho bevuto|mangiato|bevuto|mi sono mangiato|mi sono bevuto"
    r"|ho fatto|ho preso|ho mang[ia]+to|sto mangiando|ho appena mangiato)\s+",
    re.IGNORECASE,
)

_NUM_WORDS: dict[str, float] = {
    "un": 1, "uno": 1, "una": 1,
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
    "mezzo": 0.5, "mezza": 0.5,
}

_DAY_NAMES = {
    "Monday": "Lunedì", "Tuesday": "Martedì", "Wednesday": "Mercoledì",
    "Thursday": "Giovedì", "Friday": "Venerdì", "Saturday": "Sabato",
    "Sunday": "Domenica",
}

_FOOD_EMOJI: dict[str, str] = {
    "pasta": "🍝", "pizza": "🍕", "riso": "🍚", "risotto": "🍚",
    "insalata": "🥗", "pollo": "🍗", "carne": "🥩", "pesce": "🐟",
    "salmone": "🐟", "uovo": "🥚", "uova": "🥚", "latte": "🥛",
    "yogurt": "🥛", "frutta": "🍎", "mela": "🍎", "banana": "🍌",
    "arancia": "🍊", "fragole": "🍓", "uva": "🍇", "gelato": "🍨",
    "cioccolato": "🍫", "torta": "🎂", "biscotti": "🍪",
    "cornetto": "🥐", "croissant": "🥐", "pane": "🍞",
    "caffe": "☕", "caffè": "☕", "cappuccino": "☕", "tè": "🍵",
    "birra": "🍺", "vino": "🍷", "acqua": "💧",
    "hamburger": "🍔", "sushi": "🍣", "kebab": "🌯",
}


def _food_emoji(food_name: str) -> str:
    name_lower = food_name.lower()
    for key, emoji in _FOOD_EMOJI.items():
        if key in name_lower:
            return emoji
    return "🍽️"


def _parse_input(text: str) -> tuple[str, float, str, Optional[str]]:
    """Parse user input → (food_name, quantity, unit, unit_word).
    unit ∈ {'g', 'pcs'}; unit_word is the portion descriptor (es. 'fetta', 'pezzo') or None."""
    text = _PREFIX_RE.sub("", text.strip())

    # Weight: "200g pasta", "200 g di pasta", "200gr ..."
    m = re.match(
        r"^(\d+(?:[.,]\d+)?)\s*(?:kg|grammi|gr|g)\s*(?:di\s+)?(.+)$",
        text,
        re.IGNORECASE,
    )
    if m:
        q = float(m.group(1).replace(",", "."))
        unit_str = re.search(r"(kg|grammi|gr|g)", m.group(0), re.IGNORECASE).group(1).lower()
        if unit_str == "kg":
            q *= 1000
        return m.group(2).strip(), q, "g", None

    # Numeric count: "3 mele", "2 pizze", "3 misurini di proteine"
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s+(?:di\s+)?(.+)$", text)
    if m:
        qty = float(m.group(1).replace(",", "."))
        food = m.group(2).strip()

        # Numeric + unit word: "3 misurini di proteine", "2 fette di pane"
        m_unit = re.match(r"^(\w+)\s+(?:di\s+)?(.+)$", food, re.IGNORECASE)
        if m_unit:
            unit_word = m_unit.group(1).lower()
            food_candidate = m_unit.group(2).strip()
            if unit_word in UNIT_GRAMS:
                return food_candidate, qty * UNIT_GRAMS[unit_word], "g", None
            if unit_word in PORTION_WORDS:
                return food_candidate, qty, "pcs", unit_word

        return food, qty, "pcs", None

    # Number words: "una pizza", "due mele", "mezzo pollo"
    for word, val in _NUM_WORDS.items():
        if re.match(rf"^{word}\s+", text, re.IGNORECASE):
            food = re.sub(rf"^{word}\s+", "", text, flags=re.IGNORECASE).strip()
            food = re.sub(r"^di\s+", "", food)

            # Unit words after the number: "una tazza di latte", "due fette di pane"
            m_unit = re.match(
                r"^(\w+)\s+(?:di\s+)?(.+)$", food, re.IGNORECASE
            )
            if m_unit:
                unit_word = m_unit.group(1).lower()
                food_candidate = m_unit.group(2).strip()
                if unit_word in UNIT_GRAMS:
                    return food_candidate, val * UNIT_GRAMS[unit_word], "g", None
                if unit_word in PORTION_WORDS:
                    return food_candidate, val, "pcs", unit_word

            return food, val, "pcs", None

    # Unit words without a leading number: "tazza di latte", "fetta di torta"
    m_unit = re.match(
        r"^(una?|un[oa]?)?\s*(\w+)\s+(?:di\s+)?(.+)$", text, re.IGNORECASE
    )
    if m_unit:
        unit_word = m_unit.group(2).lower()
        food_candidate = m_unit.group(3).strip()
        if unit_word in UNIT_GRAMS:
            return food_candidate, float(UNIT_GRAMS[unit_word]), "g", None
        if unit_word in PORTION_WORDS:
            return food_candidate, 1.0, "pcs", unit_word

    return text, 1.0, "pcs", None


def _italian_variants(name: str) -> list:
    """Generate common Italian singular variants from a potential plural."""
    variants = []
    if name.endswith("i"):
        variants.append(name[:-1] + "o")   # cornetti → cornetto
        variants.append(name[:-1] + "e")   # bicchieri → bicchiere
    if name.endswith("e"):
        variants.append(name[:-1] + "a")   # mele → mela, fragole → fragola
        variants.append(name[:-1] + "o")   # torte → torto (fallback)
    return variants


def _lookup(food_name: str) -> Optional[tuple]:
    """Returns (kcal_per_100g, std_portion_g, portion_label, matched_key) or None."""
    name = food_name.lower().strip()
    variants = [name] + _italian_variants(name)

    # 1. Exact match (name + Italian variants)
    for candidate in variants:
        if candidate in FOODS:
            return (*FOODS[candidate], candidate)

    # 2. Key is a multi-word phrase contained in the user's text (longest first)
    sorted_keys = sorted(FOODS.keys(), key=len, reverse=True)
    for key in sorted_keys:
        for v in variants:
            if key in v or key == v:
                return (*FOODS[key], key)

    # 3. User text appears as a whole word inside a key  (avoid mele→torta di mele)
    for key in sorted_keys:
        for v in variants:
            # require word-boundary style match: v must be a token in key
            if re.search(r"\b" + re.escape(v) + r"\b", key):
                return (*FOODS[key], key)

    # 4. Fuzzy
    candidates = list(FOODS.keys())
    for v in variants:
        matches = get_close_matches(v, candidates, n=1, cutoff=0.60)
        if matches:
            key = matches[0]
            return (*FOODS[key], key)

    return None


def _split_multi(text: str) -> list[str]:
    """Split a user message into individual food items (comma, semicolon, newline)."""
    parts = re.split(r"[,;\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def estimate_calories(raw_text: str) -> Optional[dict]:
    """Main entry point: parse text → calorie info dict or None."""
    food_name, quantity, unit, unit_word = _parse_input(raw_text)
    result = _lookup(food_name)
    if result is None:
        return None

    kcal_100g, std_g, portion_label, matched_key = result

    if unit == "g":
        calories = max(1, round(kcal_100g * quantity / 100))
        qty_desc = f"{int(quantity)}g"
    else:
        # Se esiste un override per questa unità descrittiva (es. "fetta di pizza"),
        # usa quel peso invece della porzione standard dell'alimento.
        override_g = PORTION_OVERRIDES.get(matched_key, {}).get(unit_word) if unit_word else None
        portion_g = override_g if override_g is not None else std_g
        calories = max(1, round(kcal_100g * portion_g / 100 * quantity))
        if quantity == 1.0:
            qty_desc = f"1 {unit_word} (~{portion_g}g)" if override_g is not None else portion_label
        elif quantity == 0.5:
            qty_desc = f"metà {unit_word} (~{round(portion_g * 0.5)}g)" if override_g is not None else f"metà {portion_label}"
        else:
            n = int(quantity) if quantity == int(quantity) else quantity
            qty_desc = f"{n} × {unit_word} (~{portion_g}g cad.)" if override_g is not None else f"{n} × {portion_label}"

    return {
        "food": food_name.strip().title(),
        "matched": matched_key,
        "calories": calories,
        "qty_desc": qty_desc,
    }


# ── FORMATTING ────────────────────────────────────────────────────────────────


def _calories_bar(total: int, target: int) -> str:
    """Visual progress bar toward the user's daily calorie target."""
    pct = total / target
    filled = round(min(pct, 1.0) * 10)

    if pct > 1.1:
        color = "🔴"
    elif pct >= 0.85:
        color = "🟡"
    else:
        color = "🟢"

    bar = color * filled + "⚪️" * (10 - filled)
    return f"{bar}  *{total}/{target} kcal* ({round(pct * 100)}%)"


def _sparkline(values: list[float]) -> str:
    chars = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        return chars[0] * len(values)
    scale = len(chars) - 1
    return "".join(chars[round((v - vmin) / (vmax - vmin) * scale)] for v in values)


def _parse_weight_kg(text: str) -> Optional[float]:
    cleaned = text.strip().lower().replace("kg", "").strip()
    try:
        weight = float(cleaned.replace(",", "."))
    except ValueError:
        return None
    if not (20 <= weight <= 300):
        return None
    return weight


def format_weight_history(rows: list[tuple]) -> str:
    if not rows:
        return (
            "📭 Nessuna misurazione del peso ancora salvata.\n"
            "Usa `/peso 75.4` per aggiungerne una."
        )

    ordered = list(reversed(rows))
    weights = [float(w) for _, w in ordered]
    latest = weights[-1]
    first = weights[0]
    delta = latest - first
    trend = "↘️" if delta < 0 else "↗️" if delta > 0 else "➡️"
    delta_txt = f"{delta:+.1f}".replace(".", ",")
    latest_txt = f"{latest:.1f}".replace(".", ",")
    min_txt = f"{min(weights):.1f}".replace(".", ",")
    max_txt = f"{max(weights):.1f}".replace(".", ",")
    graph = _sparkline(weights)
    points = min(len(weights), 14)

    return (
        f"⚖️ *Peso attuale:* {latest_txt} kg\n"
        f"{trend} Variazione ({points} misurazioni): *{delta_txt} kg*\n"
        f"📉 Min/Max: *{min_txt}* · *{max_txt}* kg\n\n"
        f"`{graph}`\n"
        "_più vecchio → più recente_"
    )


def format_weight_table(rows: list[tuple], max_rows: int = 7) -> str:
    if not rows:
        return "📭 Nessuna misurazione disponibile."

    ordered = list(reversed(rows))  # oldest -> newest
    visible = ordered[-max_rows:]
    lines = [
        f"📋 *Storico peso (ultime {len(visible)})*",
        "`Data   | Peso`",
        "`-------|------`",
    ]
    for created_at, weight in visible:
        day = datetime.fromisoformat(created_at).strftime("%d/%m")
        weight_txt = f"{float(weight):.1f}".replace(".", ",")
        lines.append(f"`{day}  | {weight_txt}kg`")

    graph = _sparkline([float(w) for _, w in ordered])
    lines.append(f"\n📈 Andamento: `{graph}`")
    lines.append("_più vecchio → più recente_")
    return "\n".join(lines)


def format_diary(rows: list, label: str, target: int) -> str:
    if not rows:
        return f"📭 Nessuna voce per {label}."

    lines = [f"📖 *Diario {label}*\n"]
    total = 0
    for i, (_, food, cal, qty, ts) in enumerate(rows, 1):
        time_str = ts[11:16] if len(ts) > 10 else ""
        emoji = _food_emoji(food)
        lines.append(f"{i}. {emoji} *{food}* — {cal} kcal  _{qty}_  `{time_str}`")
        total += cal

    lines.append(f"\n🔥 *{total} kcal* su *{target} kcal* giornaliere")
    lines.append(_calories_bar(total, target))

    if total < round(target * 0.55):
        lines.append("⚠️ _Sotto il fabbisogno minimo — ricorda di mangiare abbastanza!_")
    elif total > round(target * 1.20):
        lines.append("⚠️ _Sopra l'obiettivo giornaliero_")
    else:
        lines.append("✅ _Nel range consigliato_")

    return "\n".join(lines)


# ── IN-MEMORY STATE ──────────────────────────────────────────────────────────

_pending: dict[int, dict] = {}
_awaiting_weight: dict[int, bool] = {}  # user_id -> ask_goal_after_weight
_awaiting_sgarro: dict[int, bool] = {}  # user_id waiting for kcal + note input

# ── HANDLERS ──────────────────────────────────────────────────────────────────


def _welcome_text(weight_kg: Optional[float], goal: str = "main") -> str:
    target = _calorie_target(weight_kg, goal)
    if weight_kg:
        goal_label = _GOAL_LABELS.get(goal, goal)
        profile_line = f"⚖️ *{weight_kg} kg* · {goal_label} · 🎯 *{target} kcal/giorno*"
    else:
        profile_line = ""
    return (
        "👋 Ciao! Sono *NutriBob*, il tuo diario alimentare personale.\n"
        + (profile_line + "\n" if profile_line else "")
        + "\n*Dimmi cosa hai mangiato e stimo le calorie, ad esempio:*\n"
        "🍝 • pasta al pomodoro\n"
        "🍗 • 200g di petto di pollo\n"
        "🍎 • due mele\n"
        "☕ • un cappuccino\n"
        "🛒 • pasta, pollo, una mela — più alimenti insieme\n\n"
        "📋 *Comandi:*\n"
        "/oggi — diario di oggi\n"
        "/ieri — diario di ieri\n"
        "/settimana — riepilogo ultimi 7 giorni\n"
        "/profilo — mostra o aggiorna peso e obiettivo\n"
        "/peso [kg] — registra peso e mostra grafico storico\n"
        "/cancella — elimina l'ultima voce\n"
        "/reset — svuota il diario di oggi\n"
        "/sgarro — registra kcal libere (es. `/sgarro 450 pizza e gelato`)\n"
        "/tabella — guida alle unità di misura\n"
        "/consigli — alimenti consigliati\n"
        "/help — mostra questo messaggio"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = db_get_profile(user_id)
    if profile is None:
        _awaiting_weight[user_id] = True
        await update.message.reply_text(
            "👋 Ciao! Sono *NutriBob*, il tuo diario alimentare personale.\n\n"
            "Prima di iniziare, dimmi il tuo *peso corporeo in kg* "
            "(es. `75` oppure `68.5`) così personalizzo il tuo obiettivo calorico:",
            parse_mode="Markdown",
        )
    else:
        weight, goal = profile
        await update.message.reply_text(_welcome_text(weight, goal), parse_mode="Markdown")
        await update.message.reply_text(
            "🎯 Se vuoi cambiare obiettivo (cut / mantenimento / bulk), scegli qui:",
            reply_markup=_goal_keyboard(),
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = db_get_profile(user_id)
    if profile is None:
        await update.message.reply_text(_welcome_text(None, "main"), parse_mode="Markdown")
    else:
        weight, goal = profile
        await update.message.reply_text(_welcome_text(weight, goal), parse_mode="Markdown")


async def cmd_profilo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = db_get_profile(user_id)
    if profile is None:
        _awaiting_weight[user_id] = True
        await update.message.reply_text(
            "⚖️ Non ho ancora il tuo peso. Mandami il peso in kg (es. `75`):",
            parse_mode="Markdown",
        )
    else:
        weight, goal = profile
        target = _calorie_target(weight, goal)
        goal_label = _GOAL_LABELS.get(goal, goal)
        goal_desc = _GOAL_DESC.get(goal, "")
        history_rows = db_get_weight_history(user_id, limit=14)
        await update.message.reply_text(
            f"⚖️ *Profilo attuale*\n"
            f"Peso: *{weight} kg*\n"
            f"Obiettivo: {goal_label} — _{goal_desc}_\n"
            f"Kcal/giorno: *{target} kcal*\n\n"
            + format_weight_table(history_rows)
            + "\n\n"
            "💡 Per aggiornare il peso usa `/peso 75.4`",
            parse_mode="Markdown",
        )


async def cmd_peso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw = " ".join(context.args).strip() if context.args else ""

    if raw:
        weight = _parse_weight_kg(raw)
        if weight is None:
            await update.message.reply_text(
                "⚠️ Formato non valido. Usa ad esempio `/peso 74.8`",
                parse_mode="Markdown",
            )
            return
        db_set_weight(user_id, weight)
        db_add_weight_entry(user_id, weight)

    rows = db_get_weight_history(user_id, limit=14)
    if not rows:
        await update.message.reply_text(
            "📭 Nessuna misurazione del peso.\nUsa `/peso 75.4` per salvare il primo valore.",
            parse_mode="Markdown",
        )
        return

    if raw:
        await update.message.reply_text(
            f"✅ Peso salvato: *{str(weight).replace('.', ',')} kg*\n\n"
            + format_weight_history(rows),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            format_weight_history(rows) + "\n\n💡 Aggiungi una misura con `/peso 75.4`",
            parse_mode="Markdown",
        )


def _user_target(user_id: int) -> int:
    profile = db_get_profile(user_id)
    if profile:
        return _calorie_target(profile[0], profile[1])
    return 2000


async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    rows = db_get_day(uid, date.today())
    today_label = "oggi (" + date.today().strftime("%d/%m/%Y") + ")"
    await update.message.reply_text(format_diary(rows, today_label, _user_target(uid)), parse_mode="Markdown")


async def cmd_ieri(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    yesterday = date.today() - timedelta(days=1)
    rows = db_get_day(uid, yesterday)
    label = "ieri (" + yesterday.strftime("%d/%m/%Y") + ")"
    await update.message.reply_text(format_diary(rows, label, _user_target(uid)), parse_mode="Markdown")


async def cmd_settimana(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_weekly(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📭 Nessun dato degli ultimi 7 giorni.")
        return

    lines = ["📊 *Riepilogo settimanale*\n"]
    week_total = 0
    for day_str, total, n in rows:
        d = date.fromisoformat(day_str)
        day_name = _DAY_NAMES.get(d.strftime("%A"), d.strftime("%A"))
        lines.append(f"*{day_name} {d.strftime('%d/%m')}* — {total} kcal  _{n} voci_")
        week_total += total

    avg = round(week_total / len(rows))
    lines.append(f"\n📈 *Media giornaliera: {avg} kcal*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancella(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = db_delete_last(update.effective_user.id)
    if row:
        _, food, cal = row
        await update.message.reply_text(
            f"🗑️ Rimossa: *{food}* ({cal} kcal)", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("📭 Nessuna voce da eliminare.")


async def cmd_consigli(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🥗 *Consigli alimentari*\n\n"
        "🥩 *Carne e Proteine Animali*\n"
        "• Manzo: tagli magri come scamone o filetto\n"
        "• Bisonte, agnello, cervo, capra\n"
        "• Carne macinata magra\n"
        "• Pesce grasso: salmone, trota, sardine e acciughe _(almeno 2× settimana)_\n"
        "• Uova intere o tuorli _(colina e vitamina K2)_\n"
        "• Pollame: pollo e tacchino\n\n"
        "🍚 *Carboidrati e Cereali*\n"
        "• Riso bianco: basmati, jasmine o chicco lungo\n"
        "• Tuberi: patate bianche e patate dolci _(potassio)_\n"
        "• Pane a lievitazione naturale — sourdough/pasta madre, senza bromuro\n\n"
        "🥦 *Verdure*\n"
        "• Carote crude quotidianamente\n"
        "• Spinaci, peperoni, zucchine, cetrioli, pomodori, melanzane, zucca, sedano\n\n"
        "🍊 *Frutta*\n"
        "• Agrumi: arance, mandarini, limoni e lime\n"
        "• Frutti di bosco: mirtilli, fragole e lamponi\n"
        "• Frutta succosa: melone, cantalupo, kiwi e ananas\n\n"
        "🧀 *Latticini e Grassi*\n"
        "• Yogurt greco intero o 2%, formaggi stagionati _(cheddar, parmigiano, svizzero)_, latte intero\n"
        "• Grassi da cucina: burro grass-fed, ghee, sevo di bue\n"
        "• Oli: avocado e cocco\n"
        "• Frutta a guscio: mandorle _(ammollate/germogliate)_ e noci di macadamia\n\n"
        "💧 *Liquidi e Condimenti*\n"
        "• Brodo di ossa o brodo di pollo ricco di collagene\n"
        "• Sale iodato _(funzione tiroidea)_, sale rosa dell'Himalaya o sale marino integrale\n"
        "• Succo di mirtillo rosso puro _(senza zuccheri aggiunti)_ e succo d'arancia senza polpa",
        parse_mode="Markdown",
    )


async def cmd_tabella(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📏 *Guida alle unità di misura*\n\n"
        "✅ *Raccomandato: usa i grammi*\n"
        "La misurazione più precisa — scrivi il numero seguito da `g` o `gr`:\n"
        "`200g pasta` · `150gr petto di pollo` · `30g parmigiano`\n\n"
        "─────────────────────────\n"
        "🥄 *Unità con peso fisso* — le conosco direttamente:\n\n"
        "`tazza` / `tazze` → 240 g\n"
        "`bicchiere` / `bicchieri` → 250 g\n"
        "`lattina` / `lattine` → 330 g\n"
        "`bottiglia` / `bottiglie` → 500 g\n"
        "`cucchiaio` / `cucchiai` → 15 g\n"
        "`cucchiaino` / `cucchiaini` → 5 g\n"
        "`ml` → 1 g (per le bevande)\n\n"
        "_Esempi: `una tazza di latte`, `due cucchiai di olio`, `250ml succo_\n\n"
        "─────────────────────────\n"
        "🍽️ *Unità descrittive* — uso la porzione standard del cibo:\n\n"
        "`fetta` — es. `una fetta di torta` → 100 g\n"
        "`piatto` — es. `un piatto di pasta al pomodoro` → 280 g\n"
        "`porzione` — es. `una porzione di pollo` → 150 g\n"
        "`pezzo` — es. `un pezzo di pizza` → 300 g\n"
        "`ciotola` — es. `una ciotola di fragole` → 150 g\n"
        "`vasetto` — es. `un vasetto di yogurt` → 125 g\n"
        "`trancio` — es. `un trancio di salmone` → 150 g\n"
        "`filetto` — es. `un filetto di merluzzo` → 150 g\n\n"
        "_Il peso effettivo dipende dal cibo — varia da alimento ad alimento._\n\n"
        "─────────────────────────\n"
        "💡 *Consiglio*: per la massima precisione usa sempre i grammi.\n"
        "Le unità descrittive sono stime basate su porzioni medie italiane.",
        parse_mode="Markdown",
    )


async def cmd_sgarro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw = " ".join(context.args).strip() if context.args else ""

    if not raw:
        _awaiting_sgarro[user_id] = True
        await update.message.reply_text(
            "🍕 *Sgarro!*\nMandami le kcal e (opzionale) una nota, ad esempio:\n"
            "`450 pizza e gelato`\n`300`",
            parse_mode="Markdown",
        )
        return

    # Parse inline: first token must be a number, rest is the note
    parts = raw.split(None, 1)
    try:
        kcal = int(float(parts[0].replace(",", ".")))
        if kcal <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Scrivi prima le kcal come numero, poi (opzionale) la nota.\n"
            "Es: `/sgarro 450 pizza e gelato`",
            parse_mode="Markdown",
        )
        return

    note = parts[1].strip() if len(parts) > 1 else "sgarro"
    db_add_entry(user_id, note, kcal, f"{kcal} kcal")
    total_today = sum(c for _, _, c, _, _ in db_get_day(user_id, date.today()))
    target = _user_target(user_id)
    await update.message.reply_text(
        f"🍕 *Sgarro registrato!*\n"
        f"📝 _{note}_ — *{kcal} kcal*\n\n"
        f"🔥 Totale oggi: *{total_today} kcal* su *{target} kcal*\n"
        f"{_calories_bar(total_today, target)}",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Sì, svuota oggi", callback_data="reset_yes"),
            InlineKeyboardButton("❌ Annulla", callback_data="reset_no"),
        ]]
    )
    await update.message.reply_text(
        "⚠️ Sicuro di voler eliminare *tutte* le voci di oggi?",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ── sgarro flow ──────────────────────────────────────────────────────────
    if user_id in _awaiting_sgarro:
        _awaiting_sgarro.pop(user_id)
        if text.lstrip("/").lower() == "annulla":
            await update.message.reply_text("OK, sgarro annullato.")
            return
        parts = text.split(None, 1)
        try:
            kcal = int(float(parts[0].replace(",", ".")))
            if kcal <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "⚠️ Scrivi prima le kcal come numero, poi (opzionale) la nota.\n"
                "Es: `450 pizza e gelato`",
                parse_mode="Markdown",
            )
            return
        note = parts[1].strip() if len(parts) > 1 else "sgarro"
        db_add_entry(user_id, note, kcal, f"{kcal} kcal")
        total_today = sum(c for _, _, c, _, _ in db_get_day(user_id, date.today()))
        target = _user_target(user_id)
        await update.message.reply_text(
            f"🍕 *Sgarro registrato!*\n"
            f"📝 _{note}_ — *{kcal} kcal*\n\n"
            f"🔥 Totale oggi: *{total_today} kcal* su *{target} kcal*\n"
            f"{_calories_bar(total_today, target)}",
            parse_mode="Markdown",
        )
        return
    # ────────────────────────────────────────────────────────────────────────

    # ── weight setup flow ────────────────────────────────────────────────────
    if user_id in _awaiting_weight:
        ask_goal = _awaiting_weight.get(user_id, False)
        # allow /annulla to exit
        if text.lstrip("/").lower() == "annulla":
            _awaiting_weight.pop(user_id, None)
            await update.message.reply_text("OK, nessuna modifica al peso.")
            return
        weight = _parse_weight_kg(text)
        if weight is None:
            await update.message.reply_text(
                "⚠️ Inserisci un numero valido in kg (es. `75` oppure `68.5`):",
                parse_mode="Markdown",
            )
            return
        db_set_weight(user_id, weight)
        db_add_weight_entry(user_id, weight)
        _awaiting_weight.pop(user_id, None)
        if ask_goal:
            await update.message.reply_text(
                f"✅ Peso salvato: *{weight} kg*\n\n"
                "🎯 Qual è il tuo obiettivo?",
                parse_mode="Markdown",
                reply_markup=_goal_keyboard(),
            )
        else:
            profile = db_get_profile(user_id)
            goal = profile[1] if profile else "main"
            target = _calorie_target(weight, goal)
            await update.message.reply_text(
                f"✅ Peso aggiornato: *{weight} kg*\n"
                f"🎯 Obiettivo invariato: {_GOAL_LABELS.get(goal, goal)}\n"
                f"🔥 Target giornaliero: *{target} kcal*",
                parse_mode="Markdown",
            )
        return
    # ────────────────────────────────────────────────────────────────────────

    parts = _split_multi(text)

    if len(parts) > 1:
        # ── multi-food path ──────────────────────────────────────────────────
        recognized = []
        failed = []
        for part in parts:
            r = estimate_calories(part)
            if r:
                recognized.append(r)
            else:
                failed.append(part)

        if not recognized:
            await update.message.reply_text(
                "🤔 Non ho riconosciuto nessuno degli alimenti nel mio database.\n\n"
                "Prova a essere più specifico, per esempio:\n"
                "• `pasta al pomodoro, 200g pollo, una mela`",
                parse_mode="Markdown",
            )
            return

        _pending[user_id] = recognized
        total_kcal = sum(r["calories"] for r in recognized)

        lines = ["──────────────", f"🛒 *{len(recognized)} alimenti riconosciuti:*\n"]
        for r in recognized:
            emoji = _food_emoji(r["food"])
            lines.append(f"• {emoji} *{r['food']}* — {r['calories']} kcal  _{r['qty_desc']}_")
        if failed:
            lines.append(f"\n⚠️ Non riconosciuti: {', '.join(failed)}")
        lines.append(f"\n🔥 *Totale: {total_kcal} kcal*")
        lines.append("──────────────")

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Aggiungi tutti al diario", callback_data="entry_yes"),
                    InlineKeyboardButton("❌ Annulla", callback_data="entry_no"),
                ],
                [
                    InlineKeyboardButton("📅 Aggiungi a ieri", callback_data="entry_ieri"),
                ],
            ]
        )
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return
    # ── single-food path ─────────────────────────────────────────────────────

    result = estimate_calories(text)

    if result is None:
        await update.message.reply_text(
            f"🤔 Non ho riconosciuto *{text}* nel mio database.\n\n"
            "Prova a essere più specifico, per esempio:\n"
            "• `200g pollo`\n"
            "• `una mozzarella`\n"
            "• `pasta al pomodoro`",
            parse_mode="Markdown",
        )
        return

    _pending[user_id] = result

    emoji = _food_emoji(result["food"])
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Aggiungi al diario", callback_data="entry_yes"),
                InlineKeyboardButton("❌ Annulla", callback_data="entry_no"),
            ],
            [
                InlineKeyboardButton("📅 Aggiungi a ieri", callback_data="entry_ieri"),
            ],
        ]
    )
    await update.message.reply_text(
        f"──────────────\n"
        f"{emoji} *{result['food']}*\n"
        f"⚖️ _{result['qty_desc']}_\n"
        f"🔥 *{result['calories']} kcal*\n"
        f"──────────────",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "entry_yes":
        entry = _pending.pop(user_id, None)
        if not entry:
            await query.edit_message_text("⚠️ Sessione scaduta, inviami di nuovo il messaggio.")
            return
        entries = entry if isinstance(entry, list) else [entry]
        for e in entries:
            db_add_entry(user_id, e["food"], e["calories"], e["qty_desc"])
        # Show running total for today
        rows = db_get_day(user_id, date.today())
        total = sum(r[2] for r in rows)
        target = _user_target(user_id)
        if len(entries) == 1:
            e = entries[0]
            added_summary = f"✅ *{e['food']}* aggiunto!\n🔥 {e['calories']} kcal — {e['qty_desc']}\n\n"
        else:
            total_added = sum(e["calories"] for e in entries)
            added_summary = f"✅ *{len(entries)} alimenti* aggiunti! (+{total_added} kcal)\n\n"
        await query.edit_message_text(
            added_summary
            + f"{_calories_bar(total, target)}\n\n"
            + "📖 usa /oggi per vedere il diario completo",
            parse_mode="Markdown",
        )

    elif data == "entry_no":
        _pending.pop(user_id, None)
        await query.edit_message_text("❌ Voce non aggiunta.")

    elif data == "entry_ieri":
        entry = _pending.pop(user_id, None)
        if not entry:
            await query.edit_message_text("⚠️ Sessione scaduta, inviami di nuovo il messaggio.")
            return
        yesterday = date.today() - timedelta(days=1)
        entries = entry if isinstance(entry, list) else [entry]
        for e in entries:
            db_add_entry(user_id, e["food"], e["calories"], e["qty_desc"], entry_date=yesterday)
        rows = db_get_day(user_id, yesterday)
        total = sum(r[2] for r in rows)
        target = _user_target(user_id)
        yesterday_label = yesterday.strftime("%d/%m/%Y")
        if len(entries) == 1:
            e = entries[0]
            added_summary = f"📅 *{e['food']}* aggiunto a ieri ({yesterday_label})!\n🔥 {e['calories']} kcal — {e['qty_desc']}\n\n"
        else:
            total_added = sum(e["calories"] for e in entries)
            added_summary = f"📅 *{len(entries)} alimenti* aggiunti a ieri ({yesterday_label})! (+{total_added} kcal)\n\n"
        await query.edit_message_text(
            added_summary
            + f"Totale ieri: *{total} kcal* su *{target} kcal*\n"
            + f"{_calories_bar(total, target)}\n\n"
            + "📖 usa /ieri per vedere il diario di ieri",
            parse_mode="Markdown",
        )

    elif data == "reset_yes":
        n = db_delete_day(user_id, date.today())
        await query.edit_message_text(
            f"🗑️ Diario di oggi svuotato ({n} voci eliminate)."
        )

    elif data == "reset_no":
        await query.edit_message_text("OK, nessuna modifica.")

    elif data.startswith("goal_"):
        goal = data[5:]  # cut / main / bulk
        if goal not in _GOAL_MULTIPLIER:
            return
        db_set_goal(user_id, goal)
        profile = db_get_profile(user_id)
        weight = profile[0] if profile else None
        target = _calorie_target(weight, goal)
        goal_label = _GOAL_LABELS[goal]
        goal_desc = _GOAL_DESC[goal]
        await query.edit_message_text(
            f"{goal_label}\n"
            f"_{goal_desc}_\n\n"
            f"🎯 Il tuo obiettivo giornaliero: *{target} kcal*\n\n"
            + _welcome_text(weight, goal),
            parse_mode="Markdown",
        )


# ── MAIN ──────────────────────────────────────────────────────────────────────


def main() -> None:
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profilo", cmd_profilo))
    app.add_handler(CommandHandler("peso", cmd_peso))
    app.add_handler(CommandHandler("oggi", cmd_oggi))
    app.add_handler(CommandHandler("ieri", cmd_ieri))
    app.add_handler(CommandHandler("settimana", cmd_settimana))
    app.add_handler(CommandHandler("cancella", cmd_cancella))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("sgarro", cmd_sgarro))
    app.add_handler(CommandHandler("tabella", cmd_tabella))
    app.add_handler(CommandHandler("consigli", cmd_consigli))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("NutriBob avviato — in ascolto...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
