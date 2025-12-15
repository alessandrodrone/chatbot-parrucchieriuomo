from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os, json, re
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# =========================
# CONFIG BASE
# =========================
TIMEZONE = pytz.timezone("Europe/Rome")
SLOT_MINUTES = 30

WORKING_HOURS = {
    0: [],  # lunedì chiuso
    1: [(8,30,12,0), (15,0,18,0)],
    2: [(8,30,12,0), (15,0,18,0)],
    3: [(8,30,12,0), (15,0,18,0)],
    4: [(8,30,12,0), (15,0,18,0)],
    5: [(8,30,13,0), (15,0,18,0)],
    6: []   # domenica chiuso
}

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}

# =========================
# GOOGLE CALENDAR
# =========================
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not GOOGLE_CALENDAR_ID or not GOOGLE_CREDS:
    raise RuntimeError("Variabili Google Calendar mancanti")

creds = service_account.Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDS),
    scopes=["https://www.googleapis.com/auth/calendar"]
)

calendar = build("calendar", "v3", credentials=creds)

# =========================
# UTILS
# =========================
def parse_time(text):
    match = re.search(r"(\d{1,2})[:\.](\d{2})", text)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        if 0 <= h < 24 and m in (0,30):
            return h, m
    return None

def get_day_ranges(date):
    ranges = []
    for sh, sm, eh, em in WORKING_HOURS[date.weekday()]:
        start = TIMEZONE.localize(datetime(date.year, date.month, date.day, sh, sm))
        end = TIMEZONE.localize(datetime(date.year, date.month, date.day, eh, em))
        ranges.append((start, end))
    return ranges

def is_free(start, end):
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }
    fb = calendar.freebusy().query(body=body).execute()
    return len(fb["calendars"][GOOGLE_CALENDAR_ID]["busy"]) == 0

def next_free_slots(day, limit=5):
    slots = []
    for start_range, end_range in get_day_ranges(day):
        cur = start_range
        while cur + timedelta(minutes=SLOT_MINUTES) <= end_range:
            end = cur + timedelta(minutes=SLOT_MINUTES)
            if is_free(cur, end):
                slots.append(cur)
                if len(slots) >= limit:
                    return slots
            cur += timedelta(minutes=SLOT_MINUTES)
    return slots

def create_event(start):
    end = start + timedelta(minutes=SLOT_MINUTES)
    event = {
        "summary": "Taglio uomo",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()}
    }
    calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

# =========================
# WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From")
    text = request.form.get("Body", "").lower().strip()

    session = SESSIONS.get(from_number, {})

    # SALUTO / RESET
    if text in ("ciao", "buongiorno", "salve"):
        SESSIONS[from_number] = {}
        return reply(
            "Ciao! 💈 Gestisco le prenotazioni per *taglio uomo*.\n"
            "Scrivimi ad esempio:\n"
            "- “Hai posto domani?”\n"
            "- “Vorrei prenotare un taglio”"
        )

    # SE ASPETTA CONFERMA
    if session.get("state") == "confirm":
        if text in ("ok", "confermo", "va bene"):
            create_event(session["slot"])
            SESSIONS[from_number] = {}
            return reply(
                f"✅ Appuntamento confermato!\n"
                f"💈 Taglio uomo\n"
                f"🕒 {session['slot'].strftime('%d/%m %H:%M')}\n\n"
                "A presto 👋"
            )
        else:
            SESSIONS[from_number] = {}
            return reply("Nessun problema! Scrivimi quando vuoi prenotare 😊")

    # ORARIO DIRETTO
    t = parse_time(text)
    if t:
        h, m = t
        day = datetime.now(TIMEZONE).date() + timedelta(days=1)
        slot = TIMEZONE.localize(datetime(day.year, day.month, day.day, h, m))
        if is_free(slot, slot + timedelta(minutes=30)):
            session["slot"] = slot
            session["state"] = "confirm"
            SESSIONS[from_number] = session
            return reply(
                f"Confermi questo appuntamento?\n"
                f"💈 Taglio uomo\n"
                f"🕒 {slot.strftime('%d/%m %H:%M')}\n\n"
                "Rispondi OK per confermare"
            )
        else:
            return reply("A quell’ora non ho posto. Vuoi che ti proponga alternative?")

    # RICHIESTA DISPONIBILITÀ
    if "posto" in text or "prenot" in text:
        day = datetime.now(TIMEZONE).date() + timedelta(days=1)
        slots = next_free_slots(day)
        if not slots:
            return reply("Domani sono pieno 😕 Vuoi vedere dopodomani?")
        session["slots"] = slots
        SESSIONS[from_number] = session
        msg = "Ecco i prossimi orari liberi:\n"
        for i,s in enumerate(slots,1):
            msg += f"{i}) {s.strftime('%d/%m %H:%M')}\n"
        msg += "\nRispondi con il numero oppure scrivi un orario."
        return reply(msg)

    # SCELTA NUMERO
    if text.isdigit() and session.get("slots"):
        idx = int(text)-1
        if 0 <= idx < len(session["slots"]):
            slot = session["slots"][idx]
            session["slot"] = slot
            session["state"] = "confirm"
            SESSIONS[from_number] = session
            return reply(
                f"Confermi questo appuntamento?\n"
                f"💈 Taglio uomo\n"
                f"🕒 {slot.strftime('%d/%m %H:%M')}\n\n"
                "Rispondi OK per confermare"
            )

    return reply(
        "Scrivimi ad esempio:\n"
        "- “Hai posto domani?”\n"
        "- “Vorrei prenotare un taglio”"
    )

def reply(text):
    r = MessagingResponse()
    r.message(text)
    return str(r)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"
