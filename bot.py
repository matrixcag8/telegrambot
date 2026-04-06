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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from foods import FOODS

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


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_add_entry(user_id: int, food: str, calories: int, quantity_desc: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO entries (user_id, food, calories, quantity_desc, created_at) VALUES (?,?,?,?,?)",
            (user_id, food, calories, quantity_desc, _now_str()),
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


def _parse_input(text: str) -> tuple[str, float, str]:
    """Parse user input → (food_name, quantity, unit).  unit ∈ {'g', 'pcs'}"""
    text = _PREFIX_RE.sub("", text.strip())

    # Weight: "200g pasta", "200 g di pasta", "200gr ..."
    m = re.match(
        r"^(\d+(?:[.,]\d+)?)\s*(?:kg|g|gr|grammi)\s*(?:di\s+)?(.+)$",
        text,
        re.IGNORECASE,
    )
    if m:
        q = float(m.group(1).replace(",", "."))
        unit_str = re.search(r"(kg|g|gr|grammi)", m.group(0), re.IGNORECASE).group(1).lower()
        if unit_str == "kg":
            q *= 1000
        return m.group(2).strip(), q, "g"

    # Numeric count: "3 mele", "2 pizze"
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s+(?:di\s+)?(.+)$", text)
    if m:
        return m.group(2).strip(), float(m.group(1).replace(",", ".")), "pcs"

    # Number words: "una pizza", "due mele", "mezzo pollo"
    for word, val in _NUM_WORDS.items():
        if re.match(rf"^{word}\s+", text, re.IGNORECASE):
            food = re.sub(rf"^{word}\s+", "", text, flags=re.IGNORECASE).strip()
            food = re.sub(r"^di\s+", "", food)
            return food, val, "pcs"

    return text, 1.0, "pcs"


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


def estimate_calories(raw_text: str) -> Optional[dict]:
    """Main entry point: parse text → calorie info dict or None."""
    food_name, quantity, unit = _parse_input(raw_text)
    result = _lookup(food_name)
    if result is None:
        return None

    kcal_100g, std_g, portion_label, matched_key = result

    if unit == "g":
        calories = max(1, round(kcal_100g * quantity / 100))
        qty_desc = f"{int(quantity)}g"
    else:
        calories = max(1, round(kcal_100g * std_g / 100 * quantity))
        if quantity == 1.0:
            qty_desc = portion_label
        elif quantity == 0.5:
            qty_desc = f"metà {portion_label}"
        else:
            n = int(quantity) if quantity == int(quantity) else quantity
            qty_desc = f"{n} × {portion_label}"

    return {
        "food": food_name.strip().title(),
        "matched": matched_key,
        "calories": calories,
        "qty_desc": qty_desc,
    }


# ── FORMATTING ────────────────────────────────────────────────────────────────


def _calories_bar(total: int) -> str:
    """Visual progress bar toward ~2000 kcal target."""
    pct = min(total / 2000, 1.0)
    filled = round(pct * 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"`[{bar}]` {round(pct * 100)}%"


def format_diary(rows: list, label: str) -> str:
    if not rows:
        return f"📭 Nessuna voce per {label}."

    lines = [f"📖 *Diario {label}*\n"]
    total = 0
    for i, (_, food, cal, qty, ts) in enumerate(rows, 1):
        time_str = ts[11:16] if len(ts) > 10 else ""
        lines.append(f"{i}. *{food}* — {cal} kcal  _{qty}_  `{time_str}`")
        total += cal

    lines.append(f"\n🔥 *Totale: {total} kcal*")
    lines.append(_calories_bar(total))

    if total < 1200:
        lines.append("⚠️ _Sotto il fabbisogno minimo — ricorda di mangiare abbastanza!_")
    elif total > 2500:
        lines.append("⚠️ _Sopra il fabbisogno tipico_")
    else:
        lines.append("✅ _Nel range consigliato_")

    return "\n".join(lines)


# ── PENDING ENTRIES (in-memory per user) ─────────────────────────────────────

_pending: dict[int, dict] = {}

# ── HANDLERS ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Ciao! Sono *NutriBob*, il tuo diario alimentare personale.\n\n"
        "Dimmi cosa hai mangiato e stimo le calorie, ad esempio:\n"
        "• `pasta al pomodoro`\n"
        "• `200g di petto di pollo`\n"
        "• `due mele`\n"
        "• `un cappuccino`\n\n"
        "📋 *Comandi:*\n"
        "/oggi — diario di oggi\n"
        "/ieri — diario di ieri\n"
        "/settimana — riepilogo ultimi 7 giorni\n"
        "/cancella — elimina l'ultima voce\n"
        "/reset — svuota il diario di oggi\n"
        "/help — mostra questo messaggio"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_oggi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_get_day(update.effective_user.id, date.today())
    today_label = "oggi (" + date.today().strftime("%d/%m/%Y") + ")"
    await update.message.reply_text(format_diary(rows, today_label), parse_mode="Markdown")


async def cmd_ieri(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    yesterday = date.today() - timedelta(days=1)
    rows = db_get_day(update.effective_user.id, yesterday)
    label = "ieri (" + yesterday.strftime("%d/%m/%Y") + ")"
    await update.message.reply_text(format_diary(rows, label), parse_mode="Markdown")


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
        [[
            InlineKeyboardButton("✅ Aggiungi al diario", callback_data="entry_yes"),
            InlineKeyboardButton("❌ Annulla", callback_data="entry_no"),
        ]]
    )
    await update.message.reply_text(
        f"{emoji} *{result['food']}*\n"
        f"📏 {result['qty_desc']}\n"
        f"🔥 *{result['calories']} kcal* (stima)",
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
        db_add_entry(user_id, entry["food"], entry["calories"], entry["qty_desc"])
        # Show running total for today
        rows = db_get_day(user_id, date.today())
        total = sum(r[2] for r in rows)
        await query.edit_message_text(
            f"✅ *{entry['food']}* aggiunto!\n"
            f"🔥 {entry['calories']} kcal — {entry['qty_desc']}\n\n"
            f"📊 Totale di oggi: *{total} kcal*\n"
            f"Usa /oggi per vedere il diario completo.",
            parse_mode="Markdown",
        )

    elif data == "entry_no":
        _pending.pop(user_id, None)
        await query.edit_message_text("❌ Voce non aggiunta.")

    elif data == "reset_yes":
        n = db_delete_day(user_id, date.today())
        await query.edit_message_text(
            f"🗑️ Diario di oggi svuotato ({n} voci eliminate)."
        )

    elif data == "reset_no":
        await query.edit_message_text("OK, nessuna modifica.")


# ── MAIN ──────────────────────────────────────────────────────────────────────


def main() -> None:
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("oggi", cmd_oggi))
    app.add_handler(CommandHandler("ieri", cmd_ieri))
    app.add_handler(CommandHandler("settimana", cmd_settimana))
    app.add_handler(CommandHandler("cancella", cmd_cancella))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("NutriBob avviato — in ascolto...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
