from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os, json, re
from datetime import datetime, timedelta, time
import pytz

from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================
# CONFIG
# =========================
TIMEZONE = pytz.timezone("Europe/Rome")
SLOT_MINUTES = 30

WORKING_HOURS = {
    0: [],                     # lunedì chiuso
    1: [(9, 0, 19, 30)],       # martedì
    2: [(9, 30, 21, 30)],      # mercoledì
    3: [(9, 0, 19, 30)],       # giovedì
    4: [(9, 30, 21, 30)],      # venerdì
    5: [(10, 0, 19, 0)],       # sabato
    6: []                      # domenica chiuso
}

# =========================
# GOOGLE CALENDAR
# =========================
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))

creds = service_account.Credentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO,
    scopes=["https://www.googleapis.com/auth/calendar"]
)

calendar = build("calendar", "v3", credentials=creds)

# =========================
# APP
# =========================
app = Flask(__name__)

SESSIONS = {}

# =========================
# UTILS
# =========================
def parse_date(text):
    text = text.lower().strip()

    if "domani" in text:
        return datetime.now(TIMEZONE).date() + timedelta(days=1)

    m = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = datetime.now().year
        return datetime(year, month, day).date()

    return None


def parse_time(text):
    m = re.search(r"(\d{1,2})[:\.](\d{2})", text)
    if m:
        return time(int(m.group(1)), int(m.group(2)))
    return None


def get_working_slots(date):
    weekday = date.weekday()
    ranges = WORKING_HOURS.get(weekday, [])
    slots = []

    for h1, m1, h2, m2 in ranges:
        start = datetime.combine(date, time(h1, m1, tzinfo=TIMEZONE))
        end = datetime.combine(date, time(h2, m2, tzinfo=TIMEZONE))

        while start + timedelta(minutes=SLOT_MINUTES) <= end:
            slots.append(start)
            start += timedelta(minutes=SLOT_MINUTES)

    return slots


def is_free(start):
    end = start + timedelta(minutes=SLOT_MINUTES)
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }

    fb = calendar.freebusy().query(body=body).execute()
    return not fb["calendars"][GOOGLE_CALENDAR_ID]["busy"]


def find_free_slots(date, limit=5):
    slots = []
    for s in get_working_slots(date):
        if is_free(s):
            slots.append(s)
        if len(slots) >= limit:
            break
    return slots


def format_slots(slots):
    out = []
    for i, s in enumerate(slots, 1):
        out.append(f"{i}) {s.strftime('%d/%m %H:%M')}")
    return "\n".join(out)


def create_event(start):
    event = {
        "summary": "Taglio uomo 💈",
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": "Europe/Rome"},
    }
    calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

# =========================
# WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From")
    msg = request.form.get("Body", "").strip().lower()

    session = SESSIONS.get(from_number, {})

    # START
    if not session:
        SESSIONS[from_number] = {"state": "idle"}
        r = MessagingResponse()
        r.message(
            "Ciao! Io gestisco solo le prenotazioni per taglio uomo 💈\n"
            "Scrivimi:\n"
            "- “Hai posto domani?”\n"
            "- “Vorrei prenotare un taglio”"
        )
        return str(r)

    # DATE
    date = parse_date(msg)
    if date:
        slots = find_free_slots(date)
        if not slots:
            r = MessagingResponse()
            r.message("Quel giorno non ho disponibilità. Vuoi un altro giorno?")
            return str(r)

        session.update({"state": "choose_slot", "date": date, "slots": slots})
        r = MessagingResponse()
        r.message(
            f"Ecco gli orari liberi:\n{format_slots(slots)}\n\n"
            "Rispondi con il numero oppure scrivi un orario (es. 18:00)."
        )
        return str(r)

    # SLOT SELECTION
    if session.get("state") == "choose_slot":
        slots = session["slots"]

        if msg.isdigit():
            i = int(msg) - 1
            if 0 <= i < len(slots):
                session["selected"] = slots[i]
                session["state"] = "confirm"
            else:
                r = MessagingResponse()
                r.message("Numero non valido.")
                return str(r)

        else:
            t = parse_time(msg)
            if t:
                for s in slots:
                    if s.time() == t:
                        session["selected"] = s
                        session["state"] = "confirm"
                        break

        if session.get("state") != "confirm":
            r = MessagingResponse()
            r.message("Per favore rispondi con il numero oppure un orario valido.")
            return str(r)

        s = session["selected"]
        r = MessagingResponse()
        r.message(
            f"Confermi questo appuntamento?\n"
            f"💈 Taglio uomo\n"
            f"🕒 {s.strftime('%d/%m %H:%M')}\n\n"
            "Rispondi OK per confermare"
        )
        return str(r)

    # CONFIRM
    if session.get("state") == "confirm" and msg in ["ok", "si", "sì"]:
        create_event(session["selected"])
        SESSIONS.pop(from_number, None)

        r = MessagingResponse()
        r.message(
            "✅ Appuntamento confermato!\n"
            "💈 Taglio uomo\n"
            f"🕒 {session['selected'].strftime('%d/%m %H:%M')}\n\n"
            "A presto 👋"
        )
        return str(r)

    # FALLBACK
    r = MessagingResponse()
    r.message(
        "Scrivimi ad esempio:\n"
        "- “Hai posto domani?”\n"
        "- “Vorrei prenotare un taglio”"
    )
    return str(r)


@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
