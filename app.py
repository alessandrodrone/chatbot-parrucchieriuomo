from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timedelta, time
import os
import re

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SERVICE_NAME = "Taglio uomo"
SLOT_MINUTES = 30
TIMEZONE = "Europe/Rome"

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

# =========================
# ORARI REALI PARRUCCHIERE
# =========================
# 0=lunedì ... 6=domenica
OPENING_HOURS = {
    1: [(time(8,30), time(12,0)), (time(15,0), time(18,0))],
    2: [(time(8,30), time(12,0)), (time(15,0), time(18,0))],
    3: [(time(8,30), time(12,0)), (time(15,0), time(18,0))],
    4: [(time(8,30), time(12,0)), (time(15,0), time(18,0))],
    5: [(time(8,30), time(13,0)), (time(15,0), time(18,0))],
}

# =========================
# GOOGLE CALENDAR
# =========================
creds = service_account.Credentials.from_service_account_file(
    "credentials.json",
    scopes=["https://www.googleapis.com/auth/calendar"]
)
calendar = build("calendar", "v3", credentials=creds)

# =========================
# MEMORIA BREVE
# =========================
SESSIONS = {}

IDLE = "idle"
ASK_TIME = "ask_time"
OFFER_PICK = "offer_pick"
CONFIRM = "confirm"
ASK_DAY = "ask_day"
ASK_ALTERNATIVE = "ask_alternative"

# =========================
# UTILS
# =========================
def normalize_time(text):
    text = text.replace(".", ":")
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\b", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2) or 0)
    if 0 <= h <= 23 and mnt in (0, 30):
        return f"{h:02d}:{mnt:02d}"
    return None

def parse_date(text):
    text = text.lower()
    today = datetime.now().date()
    if "oggi" in text:
        return today
    if "domani" in text:
        return today + timedelta(days=1)
    m = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return datetime(today.year, mo, d).date()
    return None

def is_open(day, t):
    if day.weekday() not in OPENING_HOURS:
        return False
    for start, end in OPENING_HOURS[day.weekday()]:
        if start <= t < end:
            return True
    return False

def is_free(start, end):
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": CALENDAR_ID}]
    }
    fb = calendar.freebusy().query(body=body).execute()
    return not fb["calendars"][CALENDAR_ID]["busy"]

def first_free_at_time(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    start = datetime.combine(day, time(h, m))
    end = start + timedelta(minutes=SLOT_MINUTES)
    if not is_open(day, start.time()):
        return None
    if is_free(start, end):
        return start, end
    return None

def find_next_free_slots(day, limit=3):
    slots = []
    cur = datetime.combine(day, time(0,0))
    end_day = cur + timedelta(days=1)
    while cur < end_day and len(slots) < limit:
        if is_open(day, cur.time()):
            end = cur + timedelta(minutes=SLOT_MINUTES)
            if is_free(cur, end):
                slots.append((cur, end))
        cur += timedelta(minutes=30)
    return slots

def fmt_slot(dt):
    return dt.strftime("%a %d/%m %H:%M")

# =========================
# WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From")
    user_message = request.form.get("Body", "").strip()
    text = user_message.lower()

    sess = SESSIONS.get(from_number, {"state": IDLE})
    resp = MessagingResponse()

    # ===== CHIUSURA SERA =====
    if "sera" in text or "stasera" in text:
        resp.message(
            "La sera siamo chiusi ✂️\n"
            "Ultimo appuntamento alle 17:30.\n"
            "Preferisci pomeriggio o mattina?"
        )
        return str(resp)

    # ===== CONFERMA =====
    if sess["state"] == CONFIRM:
        if "ok" in text:
            c = sess["chosen"]
            calendar.events().insert(
                calendarId=CALENDAR_ID,
                body={
                    "summary": SERVICE_NAME,
                    "start": {"dateTime": c["start"], "timeZone": TIMEZONE},
                    "end": {"dateTime": c["end"], "timeZone": TIMEZONE},
                }
            ).execute()
            resp.message(
                f"✅ Appuntamento confermato!\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {fmt_slot(datetime.fromisoformat(c['start']))}\n\n"
                "A presto 👋"
            )
            SESSIONS.pop(from_number, None)
            return str(resp)
        else:
            sess["state"] = ASK_ALTERNATIVE

    # ===== SELEZIONE SLOT =====
    if sess["state"] == OFFER_PICK:
        hhmm = normalize_time(user_message)
        if hhmm:
            day = sess["day"]
            exact = first_free_at_time(day, hhmm)
            if exact:
                s,e = exact
                sess["chosen"] = {"start": s.isoformat(), "end": e.isoformat()}
                sess["state"] = CONFIRM
                resp.message(
                    f"Confermi questo appuntamento?\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {fmt_slot(s)}\n\n"
                    "Rispondi OK per confermare oppure annulla."
                )
                return str(resp)
            else:
                resp.message("A quell’ora non sono libero. Vuoi altri orari?")
                return str(resp)

    # ===== RICHIESTA GIORNO =====
    day = parse_date(text)
    if day:
        sess["day"] = day
        slots = find_next_free_slots(day)
        if not slots:
            resp.message("Nessuna disponibilità quel giorno. Vuoi un altro giorno?")
            return str(resp)
        msg = "Ecco i prossimi orari liberi:\n"
        for i,(s,_) in enumerate(slots,1):
            msg += f"{i}) {fmt_slot(s)}\n"
        msg += "\nScrivi l’orario che preferisci (es. 17:30)"
        sess["state"] = OFFER_PICK
        SESSIONS[from_number] = sess
        resp.message(msg)
        return str(resp)

    # ===== AVVIO =====
    resp.message(
        "Ciao! Gestisco le prenotazioni per *taglio uomo* 💈\n"
        "Scrivimi ad esempio:\n"
        "• Hai posto domani?\n"
        "• Vorrei prenotare un taglio"
    )
    sess["state"] = ASK_DAY
    SESSIONS[from_number] = sess
    return str(resp)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",8080)))
