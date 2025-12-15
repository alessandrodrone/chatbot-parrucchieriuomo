from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os
import re
import json
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)

TZ = ZoneInfo("Europe/Rome")
SLOT_MINUTES = 30

# =========================
# ORARI REALI PARRUCCHIERE
# =========================
# 0=Lun ... 6=Dom
# lista di fasce (start, end) in orario locale
OPEN_HOURS = {
    0: [],  # Lun chiuso
    1: [(time(9, 0), time(19, 30))],     # Mar
    2: [(time(9, 30), time(21, 30))],    # Mer
    3: [(time(9, 0), time(19, 30))],     # Gio
    4: [(time(9, 30), time(21, 30))],    # Ven
    5: [(time(10, 0), time(19, 0))],     # Sab
    6: [],  # Dom chiuso
}

CONFIRM_WORDS = {"ok", "okay", "va bene", "confermo", "si", "sì", "perfetto"}
CANCEL_WORDS = {"annulla", "no", "cancella", "stop"}

# =========================
# SESSIONI (memoria breve)
# =========================
SESSIONS = {}

# =========================
# GOOGLE CALENDAR
# =========================
def get_calendar_service():
    # Preferisci variabile JSON
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)

    # Fallback: file credentials.json (se presente nel repo)
    if os.path.exists("credentials.json"):
        creds = service_account.Credentials.from_service_account_file(
            "credentials.json", scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)

    raise RuntimeError(
        "Manca Google Service Account. Imposta GOOGLE_SERVICE_ACCOUNT_JSON su Railway "
        "oppure carica credentials.json nel progetto."
    )

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
if not CALENDAR_ID:
    # Non crashare: rispondiamo con messaggio in chat se manca
    CALENDAR_ID = None

calendar = None
try:
    calendar = get_calendar_service()
except Exception:
    calendar = None


# =========================
# PARSING ITALIANO SEMPLICE
# =========================
WEEKDAY_MAP = {
    "lunedì": 0, "lunedi": 0,
    "martedì": 1, "martedi": 1, "mar": 1,
    "mercoledì": 2, "mercoledi": 2, "mer": 2,
    "giovedì": 3, "giovedi": 3, "gio": 3,
    "venerdì": 4, "venerdi": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}

def next_weekday(start_date: date, weekday: int) -> date:
    days_ahead = (weekday - start_date.weekday()) % 7
    if days_ahead == 0:
        # se oggi è lo stesso giorno, consideriamo "oggi" come valido
        return start_date
    return start_date + timedelta(days=days_ahead)

def parse_time_any(text: str):
    """
    Ritorna time(HH,MM) se trova:
    - 17:30
    - 17.30
    - alle 18
    - 18
    """
    t = text.lower().strip()

    # 17:30 o 17.30
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return time(int(m.group(1)), int(m.group(2)))

    # "alle 18" / "dopo le 18" / "ore 18"
    m = re.search(r"\b(?:alle|dopo\s+le|ore)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return time(int(m.group(1)), 0)

    # solo "18"
    m = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m and ("domani" in t or "oggi" in t or "merco" in t or "mart" in t or "giov" in t or "vener" in t or "sab" in t):
        return time(int(m.group(1)), 0)

    return None

def parse_date_any(text: str) -> date | None:
    t = text.lower().strip()
    today = datetime.now(TZ).date()

    if "oggi" in t:
        return today
    if "domani" in t:
        return today + timedelta(days=1)

    # formato 17/12 o 17/12/2025
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = m.group(3)
        if y:
            yy = int(y)
            if yy < 100:
                yy += 2000
        else:
            yy = today.year
        try:
            return date(yy, mo, d)
        except ValueError:
            return None

    # giorno settimana
    for k, wd in WEEKDAY_MAP.items():
        if k in t:
            return next_weekday(today, wd)

    return None

def parse_after_constraint(text: str):
    """
    Gestisce:
    - "dopo le 17:30"
    - "sera" -> dopo 17:30
    """
    t = text.lower()
    after_t = None

    if "dopo" in t:
        tt = parse_time_any(t)
        if tt:
            after_t = tt

    if "sera" in t:
        # default “sera” = dopo 17:30
        if after_t is None:
            after_t = time(17, 30)

    return after_t


# =========================
# CALENDARIO: FREE/BUSY + CREAZIONE EVENTI
# =========================
def day_open_ranges(d: date):
    return OPEN_HOURS.get(d.weekday(), [])

def is_within_open_hours(start_dt: datetime, end_dt: datetime) -> bool:
    ranges = day_open_ranges(start_dt.date())
    for s, e in ranges:
        sdt = datetime.combine(start_dt.date(), s, tzinfo=TZ)
        edt = datetime.combine(start_dt.date(), e, tzinfo=TZ)
        if start_dt >= sdt and end_dt <= edt:
            return True
    return False

def is_free(start_dt: datetime, end_dt: datetime) -> bool:
    if not calendar or not CALENDAR_ID:
        return False

    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": "Europe/Rome",
        "items": [{"id": CALENDAR_ID}],
    }
    fb = calendar.freebusy().query(body=body).execute()
    busy = fb["calendars"][CALENDAR_ID]["busy"]
    return len(busy) == 0

def create_event(start_dt: datetime, end_dt: datetime, phone: str):
    if not calendar or not CALENDAR_ID:
        raise RuntimeError("Calendar non configurato")

    event = {
        "summary": "💈 Taglio uomo",
        "description": f"Prenotazione WhatsApp - {phone}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Rome"},
    }
    return calendar.events().insert(calendarId=CALENDAR_ID, body=event).execute()


def iter_candidate_slots(d: date, after_t: time | None = None):
    ranges = day_open_ranges(d)
    for (s, e) in ranges:
        cur = datetime.combine(d, s, tzinfo=TZ)
        end = datetime.combine(d, e, tzinfo=TZ)

        if after_t:
            after_dt = datetime.combine(d, after_t, tzinfo=TZ)
            if cur < after_dt:
                cur = after_dt

        # arrotonda al prossimo slot da 30 min
        minute = (cur.minute // SLOT_MINUTES) * SLOT_MINUTES
        cur = cur.replace(minute=minute, second=0, microsecond=0)
        if cur.minute % SLOT_MINUTES != 0:
            cur += timedelta(minutes=SLOT_MINUTES - (cur.minute % SLOT_MINUTES))

        while cur + timedelta(minutes=SLOT_MINUTES) <= end:
            yield cur
            cur += timedelta(minutes=SLOT_MINUTES)


def find_free_slots(preferred_date: date | None,
                    exact_time: time | None,
                    after_time: time | None,
                    max_days: int = 7,
                    limit: int = 5):
    """
    Ritorna slot liberi (datetime start) rispettando:
    - giorno specifico (preferred_date) se fornito
    - orario esatto (exact_time) se fornito: cerca quello, altrimenti alternative
    - vincolo "dopo le X" (after_time)
    """
    today = datetime.now(TZ).date()
    base = preferred_date or today

    # se la data è nel passato, riparti da oggi
    if base < today:
        base = today

    results = []

    # Se l'utente chiede un orario preciso, proviamo prima quel giorno e quell’ora
    if preferred_date and exact_time:
        start_dt = datetime.combine(preferred_date, exact_time, tzinfo=TZ)
        end_dt = start_dt + timedelta(minutes=SLOT_MINUTES)
        if is_within_open_hours(start_dt, end_dt) and is_free(start_dt, end_dt):
            return [start_dt]

        # se non disponibile, proponi alternative nello stesso giorno:
        for cand in iter_candidate_slots(preferred_date, after_time or exact_time):
            end_c = cand + timedelta(minutes=SLOT_MINUTES)
            if is_within_open_hours(cand, end_c) and is_free(cand, end_c):
                results.append(cand)
                if len(results) >= limit:
                    return results

        # oppure stesso orario nei prossimi giorni
        for i in range(1, max_days + 1):
            d = preferred_date + timedelta(days=i)
            start_dt = datetime.combine(d, exact_time, tzinfo=TZ)
            end_dt = start_dt + timedelta(minutes=SLOT_MINUTES)
            if is_within_open_hours(start_dt, end_dt) and is_free(start_dt, end_dt):
                results.append(start_dt)
                if len(results) >= limit:
                    return results

        return results

    # Caso generale: cerca slot liberi a partire dalla data
    for i in range(0, max_days + 1):
        d = base + timedelta(days=i)
        # salta giorni chiusi
        if not day_open_ranges(d):
            continue

        for cand in iter_candidate_slots(d, after_time):
            end_c = cand + timedelta(minutes=SLOT_MINUTES)
            if is_free(cand, end_c):
                results.append(cand)
                if len(results) >= limit:
                    return results

    return results


def fmt_slot(dt: datetime) -> str:
    # "Mar 16/12 09:30"
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[dt.weekday()]} {dt.strftime('%d/%m')} {dt.strftime('%H:%M')}"


# =========================
# RISPOSTE
# =========================
def reply_text_for_slots(slots):
    if not slots:
        return ("Al momento non trovo disponibilità nelle fasce richieste 😕\n"
                "Vuoi provare con un altro giorno o un altro orario?")

    lines = ["Perfetto 💈 Ecco i prossimi orari liberi (30 minuti):"]
    for i, s in enumerate(slots, start=1):
        lines.append(f"{i}) {fmt_slot(s)}")
    lines.append("\nRispondi con 1, 2, 3... oppure scrivimi giorno e/o orario (es. “mercoledì dopo le 18” o “17:30”).")
    return "\n".join(lines)


def wants_booking(text: str) -> bool:
    t = text.lower()
    keys = ["prenot", "appunt", "posto", "disponib", "taglio", "domani", "mart", "merc", "giov", "vener", "sab", "/"]
    return any(k in t for k in keys)


# =========================
# WEBHOOK WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "")
    msg = (request.form.get("Body", "") or "").strip()

    resp = MessagingResponse()

    # sanity
    if not msg:
        resp.message("Ciao! Vuoi prenotare un taglio uomo? Scrivimi giorno e orario 😊")
        return str(resp)

    # init session
    s = SESSIONS.get(phone, {
        "state": "idle",              # idle | choosing | confirming
        "last_slots": [],
        "pending_slot": None
    })

    low = msg.lower().strip()

    # 1) conferma
    if s["state"] == "confirming":
        if low in CONFIRM_WORDS:
            slot = s.get("pending_slot")
            if not slot:
                s["state"] = "idle"
                SESSIONS[phone] = s
                resp.message("Ok! Dimmi che giorno/orario preferisci 😊")
                return str(resp)

            try:
                start_dt = datetime.fromisoformat(slot).astimezone(TZ)
                end_dt = start_dt + timedelta(minutes=SLOT_MINUTES)
                create_event(start_dt, end_dt, phone)

                resp.message(
                    "✅ Appuntamento confermato!\n"
                    f"💈 Taglio uomo\n🕒 {fmt_slot(start_dt)}\n\nA presto 👋"
                )
            except HttpError:
                resp.message("Ho avuto un problema a salvare in calendario 😕 Riprova tra poco.")
            except Exception:
                resp.message("Problema tecnico. Riprova tra poco.")
            finally:
                s["state"] = "idle"
                s["last_slots"] = []
                s["pending_slot"] = None
                SESSIONS[phone] = s
            return str(resp)

        if low in CANCEL_WORDS:
            s["state"] = "idle"
            s["pending_slot"] = None
            SESSIONS[phone] = s
            resp.message("Va bene, annullato. Dimmi un altro giorno/orario 😊")
            return str(resp)

        # se in conferma l’utente cambia idea con un nuovo vincolo (es. “mercoledì dopo le 18”)
        s["state"] = "idle"
        s["pending_slot"] = None
        # continua nel flusso sotto (ricerca nuovi slot)

    # 2) scelta numerica
    if s["state"] == "choosing":
        if re.fullmatch(r"\d+", low):
            idx = int(low) - 1
            if 0 <= idx < len(s["last_slots"]):
                chosen = s["last_slots"][idx]
                s["pending_slot"] = chosen.isoformat()
                s["state"] = "confirming"
                SESSIONS[phone] = s
                resp.message(
                    "Confermi questo appuntamento?\n"
                    f"💈 Taglio uomo\n🕒 {fmt_slot(chosen)}\n\n"
                    "Rispondi OK per confermare oppure annulla."
                )
                return str(resp)
            # numero non valido → non bloccare, chiedi chiarimento
            # (continua sotto)
        # se non è numero, lo trattiamo come nuova richiesta (orario/giorno)

    # 3) intent: prenotazione
    if not wants_booking(low):
        resp.message(
            "Ciao! Io gestisco solo le prenotazioni per taglio uomo 💈\n"
            "Scrivimi ad esempio:\n"
            "- “Hai posto domani dopo le 17:30?”\n"
            "- “Mercoledì dopo le 18”\n"
            "- “Il 17/12 alle 10:00”"
        )
        s["state"] = "idle"
        s["last_slots"] = []
        s["pending_slot"] = None
        SESSIONS[phone] = s
        return str(resp)

    # 4) estrai vincoli
    preferred_date = parse_date_any(low)
    exact_time = None
    after_time = parse_after_constraint(low)

    # se l'utente scrive un orario secco (17:30), lo usiamo come "orario preferito"
    tmsg = parse_time_any(low)
    if tmsg:
        exact_time = tmsg
        # se non c’è “dopo”, consideriamo l’orario come after_time per proporre slot da lì in poi
        if after_time is None and ("dopo" not in low):
            after_time = tmsg

    # 5) cerca slot
    if not CALENDAR_ID or not calendar:
        resp.message("Calendar non configurato. Controlla GOOGLE_CALENDAR_ID e GOOGLE_SERVICE_ACCOUNT_JSON su Railway.")
        return str(resp)

    slots = find_free_slots(
        preferred_date=preferred_date,
        exact_time=exact_time if preferred_date else None,  # solo se c’è anche la data
        after_time=after_time,
        max_days=7,
        limit=5
    )

    if slots:
        s["state"] = "choosing"
        s["last_slots"] = slots
        s["pending_slot"] = None
        SESSIONS[phone] = s
        resp.message(reply_text_for_slots(slots))
        return str(resp)

    # se nessun risultato, prova fallback: stesso orario nei prossimi giorni (se l’utente ha dato un orario)
    if exact_time and not preferred_date:
        slots2 = find_free_slots(
            preferred_date=datetime.now(TZ).date(),
            exact_time=None,
            after_time=exact_time,
            max_days=14,
            limit=5
        )
        s["state"] = "choosing"
        s["last_slots"] = slots2
        s["pending_slot"] = None
        SESSIONS[phone] = s
        resp.message(reply_text_for_slots(slots2))
        return str(resp)

    s["state"] = "idle"
    s["last_slots"] = []
    s["pending_slot"] = None
    SESSIONS[phone] = s
    resp.message("Non trovo disponibilità con quei vincoli 😕 Vuoi un altro giorno/orario?")
    return str(resp)


@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
