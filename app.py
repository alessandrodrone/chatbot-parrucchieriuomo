from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import List, Optional, Tuple, Dict

from flask import Flask, request, jsonify

# Google Calendar (opzionale)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# =========================
# CONFIG
# =========================
APP_TZ = "Europe/Rome"
TZ = ZoneInfo(APP_TZ) if ZoneInfo else None

SERVICE_NAME = "Taglio uomo"
SLOT_MINUTES = 30

BUSINESS_HOURS = {
    0: [],
    1: [("09:00", "19:30")],
    2: [("09:30", "21:30")],
    3: [("09:00", "19:30")],
    4: [("09:30", "21:30")],
    5: [("10:00", "19:00")],
    6: [],
}

CONFIRM_WORDS = {"ok", "confermo", "va bene", "si", "sì", "perfetto"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no"}

WEEKDAYS_IT = {
    "lunedi": 0, "lunedì": 0,
    "martedi": 1, "martedì": 1,
    "mercoledi": 2, "mercoledì": 2,
    "giovedi": 3, "giovedì": 3,
    "venerdi": 4, "venerdì": 4,
    "sabato": 5,
    "domenica": 6,
}

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DRY_RUN = os.getenv("DRY_RUN_NO_CALENDAR", "1") == "1"

app = Flask(__name__)

# =========================
# MEMORIA BREVE
# =========================
SESSIONS: Dict[str, dict] = {}
_CALENDAR = None

# =========================
# UTILS
# =========================
def now():
    return dt.datetime.now(TZ) if TZ else dt.datetime.now()

def parse_time(t: str) -> Optional[dt.time]:
    m = re.search(r"([0-2]?\d)[:\.]?([0-5]\d)?", t)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2)) if m.group(2) else 0
    return dt.time(h, mnt)

def parse_date(t: str) -> Optional[dt.date]:
    m = re.search(r"(\d{1,2})[\/\-](\d{1,2})", t)
    if m:
        return dt.date(now().year, int(m.group(2)), int(m.group(1)))
    for k, wd in WEEKDAYS_IT.items():
        if k in t:
            today = now().date()
            return today + dt.timedelta((wd - today.weekday()) % 7)
    if "domani" in t:
        return now().date() + dt.timedelta(days=1)
    return None

def within_hours(d: dt.date, t: dt.time) -> bool:
    for s, e in BUSINESS_HOURS.get(d.weekday(), []):
        sh, sm = map(int, s.split(":"))
        eh, em = map(int, e.split(":"))
        start = dt.time(sh, sm)
        end = dt.time(eh, em)
        end_slot = (dt.datetime.combine(d, t) + dt.timedelta(minutes=SLOT_MINUTES)).time()
        if start <= t and end_slot <= end:
            return True
    return False

def get_calendar():
    global _CALENDAR
    if DRY_RUN:
        return None
    if _CALENDAR:
        return _CALENDAR
    if not SERVICE_ACCOUNT_JSON:
        return None
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    _CALENDAR = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _CALENDAR

# =========================
# CORE BOT
# =========================
def handle_message(phone: str, msg: str) -> str:
    msg = msg.lower().strip()
    s = SESSIONS.get(phone, {})

    if msg in {"ciao", "salve", "buongiorno"}:
        return (
            "Ciao! 💈 Prenoto appuntamenti per *taglio uomo*.\n"
            "Dimmi ad esempio:\n"
            "• domani alle 18\n"
            "• mercoledì sera\n"
            "• 17/12 alle 19"
        )

    if msg in CANCEL_WORDS:
        SESSIONS.pop(phone, None)
        return "Prenotazione annullata 👍"

    if s.get("state") == "confirm":
        if msg in CONFIRM_WORDS:
            SESSIONS.pop(phone, None)
            return "✅ Appuntamento confermato! A presto 👋"
        return "Scrivi OK per confermare oppure annulla."

    date_ = parse_date(msg)
    time_ = parse_time(msg)

    if date_ and time_:
        if not within_hours(date_, time_):
            return "In quell’orario siamo chiusi. Vuoi un altro orario?"
        s["state"] = "confirm"
        s["date"] = str(date_)
        s["time"] = str(time_)
        SESSIONS[phone] = s
        return f"Confermi il taglio il {date_.strftime('%d/%m')} alle {time_.strftime('%H:%M')}?"

    if date_ and not time_:
        s["date"] = str(date_)
        SESSIONS[phone] = s
        return "Perfetto 👍 A che ora?"

    if time_ and not date_:
        s["time"] = str(time_)
        SESSIONS[phone] = s
        return "Ok 👍 Per che giorno?"

    return "Non ho capito 😅 Prova con: *domani alle 18*"

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

@app.route("/test")
def test():
    phone = request.args.get("phone", "+393000000000")
    msg = request.args.get("msg", "")
    reply = handle_message(phone, msg)
    return jsonify({
        "phone": phone,
        "user": msg,
        "bot": reply,
        "session": SESSIONS.get(phone, {})
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
