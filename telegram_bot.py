#!/usr/bin/env python3
"""
Telegram SMS Parser & Formatter Bot
-------------------------------------
Receives SMS forwarding messages (bKash, NAGAD, Rocket/Nexus Pay),
parses transaction details, and sends formatted templates to a private channel.

Hosted as a Render "Web Service" - a lightweight Flask health-check
endpoint runs in a background thread to prevent sleep cycles.

>>> CONFIGURATION <<<
Edit BOT_TOKEN, SOURCE_CHAT_ID, DESTINATION_CHANNEL_ID below.
"""

from __future__ import annotations

import os
import sys
import re
import sqlite3
import asyncio
import logging
import threading
from pathlib import Path
from typing import Final
from datetime import datetime, timedelta, timezone

from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# 1. CONFIGURATION  --  EDIT THESE VALUES or set environment variables
# ---------------------------------------------------------------------------
#
# The bot reads from environment variables first (Render secrets), falling
# back to the hardcoded values below for local development.
#
# Render secrets (set in Dashboard):
#   BOT_TOKEN, SOURCE_CHAT_ID, DESTINATION_CHANNEL_ID
#
# Local testing: edit the values below OR set the same env vars in your shell.

BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "8742421744:AAG82T3SaWv0kR68bf0BeOyczs87tc46pGQ")
SOURCE_CHAT_ID: Final[str] = os.getenv("SOURCE_CHAT_ID", "1898023864")
DESTINATION_CHANNEL_ID: Final[str] = os.getenv("DESTINATION_CHANNEL_ID", "-1003914671463")

try:
    SOURCE_CHAT_ID_INT: Final[int] = int(SOURCE_CHAT_ID)
    DESTINATION_CHANNEL_ID_INT: Final[int] = int(DESTINATION_CHANNEL_ID)
except ValueError:
    print("FATAL: SOURCE_CHAT_ID and DESTINATION_CHANNEL_ID must be numeric")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 2. Logging Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 3. SMS PARSER ENGINE
# ---------------------------------------------------------------------------

class ParsedSMS:
    """Structured fields extracted from an SMS message."""
    def __init__(self):
        self.service: str = ""           # BKASH / NAGAD / ROCKET
        self.time: str = ""
        self.date: str = ""
        self.trx_id: str = ""
        self.amount: str = ""
        self.last_number: str = ""       # Last 4+ digits / masked account
        self.original_body: str = ""     # Raw message after metadata
        self.transaction_type: str = ""  # cash_out / cash_in / unknown



def _format_time(t: str) -> str:
    """Convert 24h time to 12h with AM/PM.
    Input: 22:33 / 22:33:37 / 05:49:19 pm -> Output: 10:33:37 PM
    """
    t = t.strip().lower()
    has_ampm = "am" in t or "pm" in t
    # Remove am/pm for parsing
    t_clean = re.sub(r'[ap]m', '', t).strip()
    parts = t_clean.split(':')
    if len(parts) < 2:
        return t
    hour = int(parts[0])
    minute = parts[1]
    second = parts[2] if len(parts) > 2 else "00"
    if has_ampm:
        # Already 12h format, just capitalize AM/PM
        ampm = "AM" if "am" in t else "PM"
        return f"{hour}:{minute}:{second} {ampm}"
    # Convert 24h to 12h
    ampm = "AM" if hour < 12 else "PM"
    hour12 = hour if hour == 12 else hour % 12
    hour12 = 12 if hour12 == 0 else hour12
    return f"{hour12}:{minute}:{second} {ampm}"


def _format_date(d: str) -> str:
    """Normalize date to DD/MM/YYYY."""
    # Already in DD/MM/YYYY
    if re.match(r'\d{2}/\d{2}/\d{4}', d):
        return d
    # Convert DD-MON-YY -> DD/MM/YYYY
    months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
              "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
    m = re.match(r'(\d{2})-([A-Z]{3})-(\d{2})', d, re.I)
    if m and m.group(2).upper() in months:
        return f"{m.group(1)}/{months[m.group(2).upper()]}/20{m.group(3)}"
    return d


def parse_bkash(text: str)  -> ParsedSMS | None:
    """Parse bKash messages (Cash Out or Money Received)."""
    result = ParsedSMS()
    result.service = "BKASH"

    lines = text.strip().split('\n')
    full_text = text

    # Find the From: line
    from_match = re.search(r"From:\s*'bKash'", full_text, re.I)
    if not from_match:
        return None

    # Find date/time from "at DD/MM/YYYY HH:MM" pattern or "When:" line
    when_match = re.search(r'When:\s*(.*)', full_text)
    if when_match:
        dt_str = when_match.group(1).strip()
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})', dt_str)
        if m:
            date_parts = m.group(1).split('-')
            result.date = f"{date_parts[2]}/{date_parts[1]}/{date_parts[0]}"
            result.time = _format_time(m.group(2))

    # Get original body after the **** separator
    body_match = re.search(r'\*{3,}\s*\n(.*?)(?:\n\s*Reply:|$)', full_text, re.DOTALL)
    if body_match:
        result.original_body = body_match.group(1).strip()

    # Cash Out pattern
    cash_out = re.search(r'Cash Out\s+Tk\s+([\d,.]+)\s+from\s+(\S+)', full_text, re.I)
    # Money Received pattern
    received = re.search(r'received\s+Tk\s+([\d,.]+)\s+from\s+(\S+)', full_text, re.I)

    if cash_out:
        result.transaction_type = "cash_out"
        result.amount = cash_out.group(1)
        result.last_number = cash_out.group(2)
    elif received:
        result.transaction_type = "cash_in"
        result.amount = received.group(1)
        result.last_number = received.group(2)

    # TrxID
    trx = re.search(r'TrxID\s+(\S+)', full_text, re.I)
    if trx:
        result.trx_id = trx.group(1)

    return result


def parse_nagad(text: str)  -> ParsedSMS | None:
    """Parse NAGAD messages (Cash Out or Money Received)."""
    result = ParsedSMS()
    result.service = "NAGAD"

    full_text = text
    from_match = re.search(r"From:\s*'NAGAD'", full_text, re.I)
    if not from_match:
        return None

    # Date/time from When: line
    when_match = re.search(r'When:\s*(.*)', full_text)
    if when_match:
        dt_str = when_match.group(1).strip()
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})', dt_str)
        if m:
            date_parts = m.group(1).split('-')
            result.date = f"{date_parts[2]}/{date_parts[1]}/{date_parts[0]}"
            result.time = _format_time(m.group(2))

    # Get original body after ****
    body_match = re.search(r'\*{3,}\s*\n(.*?)(?:\n\s*Reply:|$)', full_text, re.DOTALL)
    if body_match:
        result.original_body = body_match.group(1).strip()

    # Cash Out Received
    cash_out = re.search(r'Cash\s+Out\s+Received', full_text, re.I)
    money_recv = re.search(r'Money\s+Received', full_text, re.I)

    # Amount
    amt = re.search(r'Amount:\s*Tk\s*([\d,.]+)', full_text, re.I)
    if amt:
        result.amount = amt.group(1)

    # Customer / Sender (last number)
    cust = re.search(r'Customer:\s*(\S+)', full_text, re.I)
    sender = re.search(r'Sender:\s*(\S+)', full_text, re.I)
    if cust:
        result.last_number = cust.group(1)
    elif sender:
        result.last_number = sender.group(1)

    # TxnID
    txn = re.search(r'TxnID:\s*(\S+)', full_text, re.I)
    if txn:
        result.trx_id = txn.group(1)

    result.transaction_type = "cash_out" if cash_out else ("cash_in" if money_recv else "unknown")
    return result


def parse_rocket(text: str)  -> ParsedSMS | None:
    """Parse Rocket / Nexus Pay direct SMS messages (type 5)."""
    result = ParsedSMS()
    result.service = "ROCKET"

    full_text = text

    # Detect: TkXX received from A/C:***XXX
    # These don't have the sms-fw.com format
    is_rocket = re.search(r'received\s+from\s+A/C', full_text, re.I)
    if not is_rocket:
        return None

    result.original_body = full_text.strip()

    # Amount: Tk60.00 or Tk 60.00
    amt = re.search(r'Tk\s*([\d,.]+)', full_text, re.I)
    if amt:
        result.amount = amt.group(1)

    # A/C:***439
    ac = re.search(r'A/C\s*:\s*\*{0,3}(\S+)', full_text, re.I)
    if ac:
        result.last_number = ac.group(1)

    # TxnId
    txn = re.search(r'TxnId\s*:\s*(\S+)', full_text, re.I)
    if txn:
        result.trx_id = txn.group(1)

    # Date: 29-MAY-26 05:49:19 pm
    dt = re.search(r'Date\s*:\s*(\d{2}-[A-Z]{3}-\d{2})\s+(\d{2}:\d{2}:\d{2}\s*[ap]m?)', full_text, re.I)
    if dt:
        result.date = _format_date(dt.group(1))
        result.time = _format_time(dt.group(2))

    result.transaction_type = "cash_in"
    return result


def detect_and_parse(text: str)  -> ParsedSMS | None:
    """Detect SMS type and parse accordingly."""
    # Try bKash first (most specific)
    result = parse_bkash(text)
    if result:
        return result
    # Try NAGAD
    result = parse_nagad(text)
    if result:
        return result
    # Try Rocket
    result = parse_rocket(text)
    if result:
        return result
    return None


# ---------------------------------------------------------------------------
# 4. TEMPLATE FORMATTERS
# ---------------------------------------------------------------------------


def _mask_balance(text: str) -> str:
    """Mask balance amounts in SMS text (Balance Tk X,XXX.XX -> Balance Tk ****)."""
    return re.sub(r'(Balance\s+Tk\s)[\d,]+(?:\.\d+)?', r'****', text, flags=re.I)


def format_bkash_template(parsed: ParsedSMS) -> str:
    """Format parsed bKash data into the user's template."""
    body = _mask_balance(parsed.original_body)
    amt = (parsed.amount or '').replace('.00', '')
    return f"""From: BKASH
Time: {parsed.time or 'N/A'}
Date: {parsed.date or 'N/A'}
Trx ID: {parsed.trx_id or 'N/A'}
Amount: {amt or 'N/A'} TK
Last Number: {parsed.last_number or 'N/A'}

****
{body}"""


def format_nagad_template(parsed: ParsedSMS) -> str:
    """Format parsed NAGAD data into the user's template."""
    amt = (parsed.amount or '').replace('.00', '')
    return f"""From: NAGAD
Time: {parsed.time or 'N/A'}
Date: {parsed.date or 'N/A'}
Trx ID: {parsed.trx_id or 'N/A'}
Amount: {amt or 'N/A'} TK
Last Number: {parsed.last_number or 'N/A'}

****
{parsed.original_body}"""


def format_rocket_template(parsed: ParsedSMS) -> str:
    """Format parsed Rocket data into the user's template."""
    amt = (parsed.amount or '').replace('.00', '')
    last = parsed.last_number or 'N/A'
    if last.startswith('***'):
        last = last[3:]
    return f"""From: ROCKET
Time: {parsed.time or 'N/A'}
Date: {parsed.date or 'N/A'}
Trx ID: {parsed.trx_id or 'N/A'}
Amount: {amt or 'N/A'} TK
A/C :   ***{last}

****
{parsed.original_body}"""


FORMATTERS = {
    "BKASH": format_bkash_template,
    "NAGAD": format_nagad_template,
    "ROCKET": format_rocket_template,
}


# ---------------------------------------------------------------------------
# 5. TRANSACTION DATABASE  —  SQLite storage for summaries & search
# ---------------------------------------------------------------------------

DB_PATH: Final[Path] = Path(__file__).parent / "transactions.db"
BANGLADESH_TZ: Final[timezone] = timezone(timedelta(hours=6))
_last_daily_summary_date: str = ""
_last_weekly_summary_week: str = ""
_bot_instance = None  # Set during startup, used by scheduled summary


def _init_db() -> None:
    """Create the transactions table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                service      TEXT    NOT NULL,
                txn_type     TEXT    NOT NULL,
                amount       REAL,
                amount_raw   TEXT,
                trx_id       TEXT,
                last_number  TEXT,
                date         TEXT,
                time         TEXT,
                original_body TEXT,
                raw_message   TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def _parse_amount(amount_raw: str | None) -> float | None:
    """Parse an amount string like '11,600.48' to float."""
    if not amount_raw:
        return None
    try:
        return float(amount_raw.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _fmt_currency(amount: float | None) -> str:
    """Format a float as ৳X,XXX.XX."""
    if amount is None:
        return "৳0"
    return f"৳{amount:,.2f}"


def _save_transaction(parsed, raw_text: str) -> None:
    """Insert a parsed transaction into the database."""
    amount_float = _parse_amount(parsed.amount)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO transactions
               (service, txn_type, amount, amount_raw, trx_id, last_number, date, time, original_body, raw_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                parsed.service,
                parsed.transaction_type,
                amount_float,
                parsed.amount or "",
                parsed.trx_id or "",
                parsed.last_number or "",
                parsed.date or "",
                parsed.time or "",
                parsed.original_body or "",
                raw_text,
            ),
        )
        conn.commit()


# ── Summary queries ──────────────────────────────────────────────────────────


def _query_summary(start_date: str, end_date: str) -> list[dict]:
    """Return aggregated summary grouped by service and txn_type."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT service, txn_type, COUNT(*) as count, COALESCE(SUM(amount), 0) as total
               FROM transactions
               WHERE date >= ? AND date <= ?
               GROUP BY service, txn_type
               ORDER BY service, txn_type""",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def _search_transactions(trx_id: str | None = None, amount: str | None = None) -> list[dict]:
    """Search by Trx ID (case-insensitive) or amount (partial match)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if trx_id:
            rows = conn.execute(
                """SELECT id, service, txn_type, amount_raw, trx_id, last_number, date, time
                   FROM transactions
                   WHERE LOWER(trx_id) LIKE ?
                   ORDER BY date DESC, time DESC LIMIT 20""",
                (f"%{trx_id.lower()}%",),
            ).fetchall()
        elif amount:
            rows = conn.execute(
                """SELECT id, service, txn_type, amount_raw, trx_id, last_number, date, time
                   FROM transactions
                   WHERE amount_raw LIKE ? OR CAST(amount AS TEXT) LIKE ?
                   ORDER BY date DESC, time DESC LIMIT 20""",
                (f"%{amount}%", f"%{amount}%"),
            ).fetchall()
        else:
            return []
        return [dict(r) for r in rows]


def _get_transaction_by_id(txn_id: int) -> dict | None:
    """Get full details of a single transaction by DB id."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)
        ).fetchone()
        return dict(row) if row else None


# ── In-memory search context (for compact → detail flow) ─────────────────────

_last_search_results: dict[int, list[dict]] = {}


# ── Summary & search formatting ──────────────────────────────────────────────


def _format_summary(data: list[dict], period_label: str) -> str:
    """Format aggregated summary into a Telegram message."""
    if not data:
        return f"📭 No transactions found for {period_label}."

    grand_total = 0.0
    grand_count = 0
    lines: list[str] = [f"📊 {period_label}\n"]

    services: dict[str, list[dict]] = {}
    for row in data:
        services.setdefault(row["service"], []).append(row)

    for svc in ["BKASH", "NAGAD", "ROCKET"]:
        if svc not in services:
            continue
        lines.append(f"<b>{svc}</b>")
        for row in services[svc]:
            txn_type = row["txn_type"].replace("_", " ").title()
            count = row["count"]
            total = row["total"]
            grand_total += total
            grand_count += count
            lines.append(f"  {txn_type}: {count} txn — {_fmt_currency(total)}")
        lines.append("")

    lines.append(f"<b>Total: {grand_count} transactions — {_fmt_currency(grand_total)}</b>")
    return "\n".join(lines)


def _format_search_compact(results: list[dict]) -> str:
    """Format search results as a compact numbered list."""
    if not results:
        return "🔍 No matching transactions found."

    lines = [f"🔍 Found {len(results)} result(s):\n"]
    for i, r in enumerate(results, 1):
        amt = r["amount_raw"] or "?"
        svc = r["service"]
        txn_type = r["txn_type"].replace("_", " ").title()
        trx = r["trx_id"] or "-"
        last = r["last_number"] or "-"
        dt = r["date"] or ""
        lines.append(f"{i}. <b>{svc}</b> | {txn_type} | {amt} TK | {last} | {trx} | {dt}")
    lines.append("\nReply with the number to see the full template.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Flask Health-Check Server
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/health")
def health_check():
    return {"status": "ok"}, 200


@flask_app.route("/")
def index():
    return {"service": "Telegram SMS Parser Bot", "status": "running"}, 200


def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# ---------------------------------------------------------------------------
# 7. SECURITY FILTER  —  Block sensitive messages (OTP, passwords, PINs, etc.)
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: Final[list] = [
    re.compile(r'\bOTP\b', re.I),
    re.compile(r'\bone[- ]?time\s+password\b', re.I),
    re.compile(r'\bverification\s+code\b', re.I),
    re.compile(r'\blogin\s+code\b', re.I),
    re.compile(r'\bsecurity\s+code\b', re.I),
    re.compile(r'\bPIN\b', re.I),
    re.compile(r'\bCVV\b', re.I),
    re.compile(r'\bCVC\b', re.I),
    re.compile(r'\btwo[- ]?factor\b', re.I),
    re.compile(r'\b2FA\b', re.I),
    re.compile(r'\brecovery\s+code\b', re.I),
    re.compile(r'\bbackup\s+code\b', re.I),
    re.compile(r'\btemporary\s+password\b', re.I),
    re.compile(r'\bpassword\s+reset\b', re.I),
    re.compile(r'\bsign[- ]?in\s+code\b', re.I),
    re.compile(r'\bDo not share\b', re.I),
]


def _is_sensitive(text: str) -> bool:
    """Return True if the message contains security-sensitive content (OTP, password, PIN, etc.)."""
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            logger.info("Blocked sensitive message matching: %s", pattern.pattern)
            return True
    return False


# ---------------------------------------------------------------------------
# 8. COMMAND HANDLERS  —  /daily, /weekly, /search
# ---------------------------------------------------------------------------


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send today's transaction summary."""
    if not update.effective_chat or update.effective_chat.id != SOURCE_CHAT_ID_INT:
        return
    today = datetime.now(BANGLADESH_TZ).strftime("%d/%m/%Y")
    data = await asyncio.to_thread(_query_summary, today, today)
    msg = _format_summary(data, f"Daily Summary — {today}")
    await update.effective_message.reply_text(msg, parse_mode="HTML")


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the weekly transaction summary (last 7 days)."""
    if not update.effective_chat or update.effective_chat.id != SOURCE_CHAT_ID_INT:
        return
    now = datetime.now(BANGLADESH_TZ)
    week_ago = now - timedelta(days=7)
    start = week_ago.strftime("%d/%m/%Y")
    end = now.strftime("%d/%m/%Y")
    data = await asyncio.to_thread(_query_summary, start, end)
    msg = _format_summary(data, f"Weekly Summary — {start} to {end}")
    await update.effective_message.reply_text(msg, parse_mode="HTML")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search transactions by Trx ID or amount.
    Usage:
      /search DFB6898VEG      — search by Trx ID
      /search amount 60       — search by amount
    """
    if not update.effective_chat or update.effective_chat.id != SOURCE_CHAT_ID_INT:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/search <TrxID> — search by transaction ID\n/search amount <amount> — search by amount"
        )
        return

    chat_id = update.effective_chat.id

    if context.args[0].lower() == "amount" and len(context.args) > 1:
        amount_query = " ".join(context.args[1:])
        results = await asyncio.to_thread(_search_transactions, amount=amount_query)
        label = f"amount matching '{amount_query}'"
    else:
        trx_query = " ".join(context.args)
        results = await asyncio.to_thread(_search_transactions, trx_id=trx_query)
        label = f"Trx ID matching '{trx_query}'"

    if results:
        # Cap in-memory cache at 50 entries
        if len(_last_search_results) > 50:
            _last_search_results.pop(next(iter(_last_search_results)))
        _last_search_results[chat_id] = results
    msg = _format_search_compact(results)
    await update.effective_message.reply_text(msg, parse_mode="HTML")


# ---------------------------------------------------------------------------
# 9. Message Handler
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route SMS messages to parser, media to forward.

    Accepts messages from any chat (not just SOURCE_CHAT_ID) so that
    sms-fw.com forwarded SMS arriving via different chat paths still get
    processed and forwarded to the destination channel.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info("Incoming message from chat_id=%s (expected source=%s)",
                 chat_id, SOURCE_CHAT_ID_INT)
    message = update.effective_message
    if message is None:
        return
    if message.text:
        await _handle_text(message, context)
        return
    if message.photo or message.document:
        await _forward_media(message, context)
        return


async def _handle_text(message, context: ContextTypes.DEFAULT_TYPE):
    """Try to parse as SMS; if successful, format template; else forward raw."""
    text = message.text or ""
    chat_id = message.chat.id if message.chat else 0

    # Check if this is a numeric reply to a search result (compact → detail)
    if chat_id in _last_search_results and message.reply_to_message and text.strip().isdigit():
        idx = int(text.strip()) - 1
        results = _last_search_results[chat_id]
        if 0 <= idx < len(results):
            full = _get_transaction_by_id(results[idx]["id"])
            if full:
                parsed = ParsedSMS()
                parsed.service = full["service"]
                parsed.time = full["time"] or ""
                parsed.date = full["date"] or ""
                parsed.trx_id = full["trx_id"] or ""
                parsed.amount = full["amount_raw"] or ""
                parsed.last_number = full["last_number"] or ""
                parsed.original_body = full["original_body"] or ""
                parsed.transaction_type = full["txn_type"]
                formatted = FORMATTERS[parsed.service](parsed) if parsed.service in FORMATTERS else f"{parsed.service} — {full['raw_message']}"
                await message.reply_text(formatted)
                return
        await message.reply_text(f"Number out of range (1-{len(results)}).")
        return

    # Security filter: block OTPs, passwords, PINs, and other sensitive content
    if _is_sensitive(text):
        logger.warning("Blocked sensitive message from reaching channel")
        return

    # Attempt smart parsing
    parsed = detect_and_parse(text)
    if parsed and parsed.service in FORMATTERS:
        formatter = FORMATTERS[parsed.service]
        formatted = formatter(parsed)
        logger.info("Parsed %s %s: %s TK, Trx %s",
                    parsed.service, parsed.transaction_type,
                    parsed.amount or "?", parsed.trx_id or "?")
        # Save to database for summaries & search
        asyncio.create_task(asyncio.to_thread(_save_transaction, parsed, text))
    else:
        # Fallback: just forward the raw text with a generic prefix
        logger.debug("Could not parse SMS, forwarding raw")
        formatted = f"\U0001F4E8 New message:\n\n{text}"

    try:
        await context.bot.send_message(
            chat_id=DESTINATION_CHANNEL_ID_INT,
            text=formatted,
            parse_mode=None,
        )
    except Exception as exc:
        logger.error("Failed to send: %s", exc)


async def _forward_media(message, context: ContextTypes.DEFAULT_TYPE):
    """Forward photos / documents as-is."""
    try:
        await context.bot.forward_message(
            chat_id=DESTINATION_CHANNEL_ID_INT,
            from_chat_id=SOURCE_CHAT_ID_INT,
            message_id=message.message_id,
        )
        logger.info("Forwarded media %d", message.message_id)
    except Exception as exc:
        logger.error("Failed to forward media: %s", exc)


# ── Scheduled summary sender ────────────────────────────────────────────────


async def _scheduled_summary_loop() -> None:
    """Background loop: sends daily summary at 1 AM BDT, weekly on Sunday."""
    global _last_daily_summary_date, _last_weekly_summary_week
    await asyncio.sleep(30)  # Wait for bot to fully start
    while True:
        try:
            now = datetime.now(BANGLADESH_TZ)
            today = now.strftime("%d/%m/%Y")
            this_week = now.strftime("%Y-%W")

            # Daily summary at 1:00 AM (within a 30-min window)
            if now.hour == 1 and now.minute < 30:
                if today != _last_daily_summary_date:
                    _last_daily_summary_date = today
                    data = _query_summary(today, today)
                    msg = _format_summary(data, f"Daily Summary — {today}")
                    await _send_to_channel(msg)

            # Weekly summary on Sunday at 1:00 AM
            if now.weekday() == 6 and now.hour == 1 and now.minute < 30:
                if this_week != _last_weekly_summary_week:
                    _last_weekly_summary_week = this_week
                    week_ago = now - timedelta(days=7)
                    start = week_ago.strftime("%d/%m/%Y")
                    end = today
                    data = _query_summary(start, end)
                    msg = _format_summary(data, f"Weekly Summary — {start} to {end}")
                    await _send_to_channel(msg)

        except Exception as exc:
            logger.error("Scheduled summary error: %s", exc)
        await asyncio.sleep(1800)  # Check every 30 minutes


async def _send_to_channel(text: str) -> None:
    """Helper: send a message to the destination channel using cached bot instance."""
    if _bot_instance is None:
        logger.error("Cannot send summary: bot not initialized")
        return
    try:
        await _bot_instance.send_message(chat_id=DESTINATION_CHANNEL_ID_INT, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.error("Failed to send scheduled summary: %s", exc)


# ---------------------------------------------------------------------------
# 10. Error Handler
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception: %s", context.error, exc_info=True)


# ---------------------------------------------------------------------------
# 11. Main Entry Point
# ---------------------------------------------------------------------------


async def main():
    _init_db()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # Register handlers in priority order: commands first, then catch-all
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("weekly", cmd_weekly))
    application.add_handler(CommandHandler("search", cmd_search))
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.ALL,
        handle_message,
    ))
    application.add_error_handler(error_handler)

    # Manually start the application (avoids event loop conflict with run_polling)
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Cache bot instance for background tasks
    global _bot_instance
    _bot_instance = application.bot

    # Start background scheduled summary
    asyncio.create_task(_scheduled_summary_loop())

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    port = int(os.getenv("PORT", 8080))
    logger.info("Flask health-check on port %s", port)
    logger.info("SMS Parser Bot started - listening %s, forwarding to %s",
                SOURCE_CHAT_ID_INT, DESTINATION_CHANNEL_ID_INT)

    # Keep running until interrupted
    stop_signal = asyncio.Event()
    try:
        await stop_signal.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as exc:
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)
