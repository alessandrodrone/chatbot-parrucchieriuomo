from __future__ import annotations
import os, re, json, datetime as dt
from typing import Optional, Dict, Any, List, Tuple
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================================================
# APP
# ======================================================
app = Flask(__name__)

# ======================================================
# ENV
# ======================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SESSION_TTL_MINUTES = 30
MAX_LOOKAHEAD_DAYS = 14

# ======================================================
# GOOGLE CLIENTS
# ======================================================
def creds():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar"
    ]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)

_sheets = None
_calendar = None

def sheets():
    global _sheets
    if not _sheets:
        _sheets = build("sheets", "v4", credentials=creds(), cache_discovery=False)
    return _sheets

def calendar():
    global _calendar
    if not _calendar:
        _calendar = build("calendar", "v3", credentials=creds(), cache_discovery=False)
    return _calendar

# ======================================================
# UTILS
# ======================================================
def norm_phone(p: str) -> str:
    return re.sub(r"\D+", "", p or "")

def now():
    return dt.datetime.now()

# ======================================================
# PARSING NATURALE
# ======================================================
def parse_period(text: str) -> Tuple[dt.date, dt.date]:
    t = text.lower()
    today = now().date()

    if "settimana prossima" in t:
        start = today + dt.timedelta(days=(7 - today.weekday()))
        return start, start + dt.timedelta(days=6)

    if "questa settimana" in t:
        return today, today + dt.timedelta(days=6)

    if "weekend" in t:
        sat = today + dt.timedelta(days=(5 - today.weekday()) % 7)
        return sat, sat + dt.timedelta(days=1)

    if "domani" in t:
        d = today + dt.timedelta(days=1)
        return d, d

    return today, today + dt.timedelta(days=MAX_LOOKAHEAD_DAYS)

def parse_time_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    after = before = None

    if "sera" in t:
        after = dt.time(18,0)

    if "pomeriggio" in t:
        after = dt.time(14,0)
        before = dt.time(18,0)

    m = re.search(r"alle (\d{1,2})", t)
    if m:
        h = int(m.group(1))
        after = dt.time(h,0)
        before = dt.time(h,0)

    return after, before

# ======================================================
# SLOT LOGIC
# ======================================================
def generate_slots(start_date, end_date, after, before):
    slots = []
    d = start_date
    while d <= end_date:
        for h in range(9, 21):
            t = dt.time(h,0)
            if after and t < after: continue
            if before and t > before: continue
            slots.append(dt.datetime.combine(d, t))
        d += dt.timedelta(days=1)
    return slots

# ======================================================
# CORE BOT
# ======================================================
def handle_message(shop: Dict[str,str], phone: str, text: str) -> str:
    t = text.lower()

    # 1️⃣ periodo
    start_d, end_d = parse_period(t)

    # 2️⃣ fascia
    after, before = parse_time_window(t)

    # 3️⃣ servizio
    services = ["taglio uomo", "barba", "taglio + barba"]
    service = next((s for s in services if s in t), None)

    # 4️⃣ slot
    slots = generate_slots(start_d, end_d, after, before)

    if not slots:
        return "Non vedo disponibilità 😕 Vuoi provare un altro giorno o fascia?"

    # se manca servizio → mostra slot ma chiedi servizio
    if not service:
        msg = "Perfetto 👍 Ho trovato queste disponibilità:\n"
        for i,s in enumerate(slots[:5],1):
            msg += f"{i}) {s.strftime('%a %d/%m %H:%M')}\n"
        msg += "\nDimmi anche che servizio desideri così confermiamo 👌"
        return msg

    # conferma diretta
    s = slots[0]
    return (
        f"Confermi questo appuntamento?\n"
        f"💈 *{service}*\n"
        f"🕒 {s.strftime('%a %d/%m %H:%M')}\n\n"
        f"Rispondi *OK* per confermare oppure *annulla*."
    )

# ======================================================
# ROUTES
# ======================================================
@app.route("/test")
def test():
    phone = request.args.get("phone")
    msg = request.args.get("msg")
    shop = {"name":"Barber Test"}
    return jsonify({
        "bot_reply": handle_message(shop, phone, msg)
    })

@app.route("/")
def home():
    return "Bot parrucchieri attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
