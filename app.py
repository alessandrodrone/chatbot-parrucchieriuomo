from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import List, Optional, Tuple, Dict

from flask import Flask, request, jsonify

# Twilio è opzionale: se vuoi testare senza Twilio, non serve installarlo.
try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:
    MessagingResponse = None

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

# Orari REALI negozio:
# lunedì: chiuso
# martedì: 09–19:30
# mercoledì: 09:30–21:30
# giovedì: 09–19:30
# venerdì: 09:30–21:30
# sabato: 10–19
# domenica: chiuso
BUSINESS_HOURS = {
    0: [],  # lun
    1: [("09:00", "19:30")],  # mar
    2: [("09:30", "21:30")],  # mer
    3: [("09:00", "19:30")],  # gio
    4: [("09:30", "21:30")],  # ven
    5: [("10:00", "19:00")],  # sab
    6: [],  # dom
}

CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}

BOOKING_HINTS = {"prenota", "prenotare", "appuntamento", "taglio", "posto", "disponibile", "disponibilità", "hai posto"}
AVAILABILITY_HINTS = {"hai posto", "disponibilità", "disponibile", "posto"}

WEEKDAYS_IT = {
    "lunedì": 0, "lunedi": 0, "lun": 0,
    "martedì": 1, "martedi": 1, "mar": 1,
    "mercoledì": 2, "mercoledi": 2, "mer": 2,
    "giovedì": 3, "giovedi": 3, "gio": 3,
    "venerdì": 4, "venerdi": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID") or "primary"
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # consigliato su Railway
DRY_RUN_NO_CALENDAR = (os.getenv("DRY_RUN_NO_CALENDAR", "0") == "1")

app = Flask(__name__)

# =========================
# MEMORIA BREVE
# =========================
SESSIONS: Dict[str, dict] = {}

# cache del servizio calendar (evita rebuild ad ogni messaggio)
_CALENDAR_SERVICE = None


# =========================
# TIME UTILS
# =========================
def now_local() -> dt.datetime:
    if TZ:
        return dt.datetime.now(TZ)
    return dt.datetime.now()

def to_local(dtobj: dt.datetime) -> dt.datetime:
    if TZ and dtobj.tzinfo is None:
        return dtobj.replace(tzinfo=TZ)
    return dtobj

def round_to_next_slot(d: dt.datetime) -> dt.datetime:
    d = to_local(d)
    base = d.replace(second=0, microsecond=0)
    minutes = (base.minute // SLOT_MINUTES) * SLOT_MINUTES
    base = base.replace(minute=minutes)
    if base < d:
        base += dt.timedelta(minutes=SLOT_MINUTES)
    return base

def parse_time(text: str) -> Optional[dt.time]:
    t = text.strip().lower()
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))
    m2 = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m2:
        return dt.time(int(m2.group(1)), 0)
    m3 = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m3:
        return dt.time(int(m3.group(1)), int(m3.group(2)))
    return None

def parse_date(text: str) -> Optional[dt.date]:
    t = text.strip().lower()
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year_raw = m.group(3)
    if year_raw:
        y = int(year_raw)
        year = 2000 + y if y < 100 else y
    else:
        year = now_local().year
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None

def next_weekday(target_wd: int) -> dt.date:
    today = now_local().date()
    days_ahead = (target_wd - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + dt.timedelta(days=days_ahead)

def parse_relative_day(text: str) -> Optional[dt.date]:
    t = text.lower()
    today = now_local().date()
    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            return next_weekday(wd)
    return None

def time_window_from_text(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    after = None
    before = None

    m_after = re.search(r"\bdopo\s+le?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m_after:
        after = parse_time(m_after.group(1))

    m_before = re.search(r"\bprima\s+delle?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m_before:
        before = parse_time(m_before.group(1))

    # fasce generiche
    if "mattina" in t:
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)
    if "pomeriggio" in t:
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)
    if "sera" in t:
        after = after or dt.time(17, 30)
        # nel tuo negozio mer/ven sono aperti fino 21:30
        before = before or dt.time(21, 30)

    return after, before

def within_business_hours(date_: dt.date, t: dt.time) -> bool:
    intervals = BUSINESS_HOURS.get(date_.weekday(), [])
    for start_s, end_s in intervals:
        hs, ms = map(int, start_s.split(":"))
        he, me = map(int, end_s.split(":"))
        start = dt.time(hs, ms)
        end = dt.time(he, me)
        slot_end_dt = dt.datetime.combine(date_, t) + dt.timedelta(minutes=SLOT_MINUTES)
        slot_end = slot_end_dt.time()
        if start <= t and slot_end <= end:
            return True
    return False

def format_dt(d: dt.datetime) -> str:
    d = to_local(d)
    s = d.strftime("%a %d/%m %H:%M")
    return (s.replace("Mon", "Lun")
             .replace("Tue", "Mar")
             .replace("Wed", "Mer")
             .replace("Thu", "Gio")
             .replace("Fri", "Ven")
             .replace("Sat", "Sab")
             .replace("Sun", "Dom"))

def parse_choice_number(text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\b", text.strip())
    if not m:
        return None
    return int(m.group(1))


# =========================
# GOOGLE CALENDAR
# =========================
def get_calendar():
    """
    Non crasha se mancano variabili:
    - se DRY_RUN_NO_CALENDAR=1: non usa Calendar
    - altrimenti: richiede GOOGLE_SERVICE_ACCOUNT_JSON oppure credentials.json nel repo
    """
    global _CALENDAR_SERVICE

    if DRY_RUN_NO_CALENDAR:
        return None

    if _CALENDAR_SERVICE is not None:
        return _CALENDAR_SERVICE

    scopes = ["https://www.googleapis.com/auth/calendar"]
    try:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            # fallback file (solo se esiste davvero)
            creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=scopes)

        _CALENDAR_SERVICE = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _CALENDAR_SERVICE
    except Exception as e:
        # IMPORTANTISSIMO: non far crashare tutto il server
        _CALENDAR_SERVICE = None
        return None

def is_free(calendar, start: dt.datetime, end: dt.datetime) -> bool:
    if calendar is None:
        return True  # dry-run: consideriamo libero
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    fb = calendar.freebusy().query(body=body).execute()
    busy = fb["calendars"][GOOGLE_CALENDAR_ID].get("busy", [])
    return len(busy) == 0

def create_event(calendar, start: dt.datetime, end: dt.datetime, phone: str) -> str:
    if calendar is None:
        return "dry-run-event"
    event = {
        "summary": SERVICE_NAME,
        "start": {"dateTime": start.isoformat(), "timeZone": APP_TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": APP_TZ},
        "description": f"Prenotazione\nTelefono: {phone}",
        "extendedProperties": {"private": {"phone": phone, "service": "taglio_uomo"}},
    }
    created = calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("id", "")

def get_customer_history(calendar, phone: str) -> Tuple[int, Optional[dt.datetime]]:
    if calendar is None:
        return 0, None
    try:
        events = calendar.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
            privateExtendedProperty=f"phone={phone}",
            timeMin=(now_local() - dt.timedelta(days=3650)).isoformat(),
        ).execute()
        items = events.get("items", [])
        count = len(items)
        last_dt = None
        if items:
            s = items[-1]["start"].get("dateTime")
            if s:
                last_dt = dt.datetime.fromisoformat(s)
        return count, last_dt
    except Exception:
        return 0, None

def find_slots(calendar, preferred_date: Optional[dt.date], after: Optional[dt.time], before: Optional[dt.time],
               limit: int = 5, max_days: int = 10) -> List[dt.datetime]:
    slots: List[dt.datetime] = []
    today = now_local().date()
    start_date = preferred_date or today

    for day_offset in range(0, max_days + 1):
        d = start_date + dt.timedelta(days=day_offset)

        if not BUSINESS_HOURS.get(d.weekday(), []):
            continue

        intervals = BUSINESS_HOURS[d.weekday()]
        for start_s, end_s in intervals:
            hs, ms = map(int, start_s.split(":"))
            he, me = map(int, end_s.split(":"))
            start_dt = dt.datetime.combine(d, dt.time(hs, ms))
            end_dt = dt.datetime.combine(d, dt.time(he, me))
            start_dt = to_local(start_dt)
            end_dt = to_local(end_dt)

            if after:
                start_dt = max(start_dt, to_local(dt.datetime.combine(d, after)))
            if before:
                end_dt = min(end_dt, to_local(dt.datetime.combine(d, before)))

            if end_dt <= start_dt:
                continue

            if d == today:
                start_dt = max(start_dt, round_to_next_slot(now_local()))

            cur = round_to_next_slot(start_dt)
            while cur + dt.timedelta(minutes=SLOT_MINUTES) <= end_dt:
                cur_end = cur + dt.timedelta(minutes=SLOT_MINUTES)
                if within_business_hours(d, cur.time()):
                    try:
                        if is_free(calendar, cur, cur_end):
                            slots.append(cur)
                            if len(slots) >= limit:
                                return slots
                    except HttpError:
                        return slots
                cur += dt.timedelta(minutes=SLOT_MINUTES)

    return slots


# =========================
# SESSION HELPERS
# =========================
def set_session(phone: str, **kwargs):
    s = SESSIONS.get(phone, {})
    s.update(kwargs)
    SESSIONS[phone] = s

def reset_flow(phone: str):
    SESSIONS[phone] = {}

def help_text() -> str:
    return (
        "Ciao! Io gestisco solo le prenotazioni per taglio uomo 💈\n"
        "Scrivimi ad esempio:\n"
        "• “Hai posto domani?”\n"
        "• “Vorrei prenotare il 17/12 alle 18:00”\n"
        "• “Mercoledì dopo le 18”"
    )


# =========================
# CORE LOGIC
# =========================
def handle_message(phone: str, text: str) -> str:
    t = (text or "").strip()
    tlow = t.lower()

    if not t:
        return help_text()

    # annulla
    if any(w in tlow for w in CANCEL_WORDS):
        reset_flow(phone)
        return "Va bene 👍 Prenotazione annullata. Se vuoi riprovare, dimmi giorno e ora (es. “Mercoledì 18:00”)."

    s = SESSIONS.get(phone, {})
    state = s.get("state")

    abs_date = parse_date(t)
    rel_date = parse_relative_day(t)
    date_ = abs_date or rel_date
    time_ = parse_time(t)
    after, before = time_window_from_text(t)

    wants_booking = (
        any(k in tlow for k in BOOKING_HINTS)
        or bool(date_) or bool(time_) or bool(after) or bool(before)
        or ("dopo" in tlow) or ("prima" in tlow) or ("sera" in tlow) or ("mattina" in tlow) or ("pomeriggio" in tlow)
    )

    calendar = get_calendar()

    # greeting -> memoria lunga
    if not wants_booking and tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"}:
        count, last_dt = get_customer_history(calendar, phone)
        if count > 0 and last_dt:
            return (
                f"Ciao! Bentornato 😊\n"
                f"Ultimo taglio: {format_dt(last_dt)}.\n\n"
                f"Quando vuoi venire per il prossimo {SERVICE_NAME}?"
            )
        return help_text()

    # =========================
    # STATE: confirm
    # =========================
    if state == "confirm":
        if tlow.strip() in CONFIRM_WORDS:
            chosen_iso = s.get("chosen_iso")
            if not chosen_iso:
                reset_flow(phone)
                return "Ops, ho perso lo slot. Riproviamo: che giorno e a che ora preferisci?"
            start = to_local(dt.datetime.fromisoformat(chosen_iso))
            end = start + dt.timedelta(minutes=SLOT_MINUTES)

            try:
                if not is_free(calendar, start, end):
                    reset_flow(phone)
                    return "Quello slot è appena stato preso 😅 Vuoi che ti proponga altri orari?"
                create_event(calendar, start, end, phone)
                reset_flow(phone)
                return f"✅ Appuntamento confermato!\n💈 {SERVICE_NAME}\n🕒 {format_dt(start)}\n\nA presto 👋"
            except Exception as e:
                reset_flow(phone)
                return f"Problema tecnico nel salvare in agenda ({type(e).__name__}). Riprova tra poco."

        # se in conferma mi scrive un'altra preferenza -> ricalcolo
        set_session(phone, state=None, chosen_iso=None)

    # =========================
    # STATE: choose (lista proposta)
    # =========================
    if state == "choose":
        options: List[str] = s.get("options", [])  # ISO strings
        n = parse_choice_number(t)
        if n and 1 <= n <= len(options):
            chosen = to_local(dt.datetime.fromisoformat(options[n - 1]))
            set_session(phone, state="confirm", chosen_iso=options[n - 1])
            return (
                "Confermi questo appuntamento?\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {format_dt(chosen)}\n\n"
                "Rispondi OK per confermare oppure “annulla”."
            )

        # se l'utente chiede “sera” o mette un orario o un giorno -> NON bloccare con “1,2,3”
        if wants_booking:
            # svuota lista e continua in modalità preferenza
            set_session(phone, state=None, options=None)
        else:
            return "Dimmi un numero della lista oppure una preferenza tipo “mercoledì dopo le 18” 🙂"

    # =========================
    # STATE: need_time (ho la data, manca l’ora/fascia)
    # =========================
    if state == "need_time":
        preferred_date_iso = s.get("preferred_date")
        preferred_date = dt.date.fromisoformat(preferred_date_iso) if preferred_date_iso else None

        # se ora o fascia arrivano ora, ricalcolo slot
        time_2 = time_ or parse_time(t)
        after2, before2 = time_window_from_text(t)

        if preferred_date and time_2:
            # prova slot preciso
            return _try_specific_slot_or_alternatives(phone, calendar, preferred_date, time_2)

        if preferred_date and (after2 or before2):
            slots = find_slots(calendar, preferred_date, after2, before2, limit=5, max_days=10)
            if not slots:
                return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
            set_session(phone, state="choose", options=[x.isoformat() for x in slots])
            return _render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

        return "Perfetto 👍 A che ora preferisci? (es. 17:30) oppure dimmi una fascia (es. “dopo le 18”)."

    # =========================
    # STATE: need_date (ho ora/fascia, manca il giorno)
    # =========================
    if state == "need_date":
        # se arriva una data ora, continua
        if date_:
            preferred_time_iso = s.get("preferred_time")
            preferred_time = dt.time.fromisoformat(preferred_time_iso) if preferred_time_iso else None
            after_iso = s.get("after")
            before_iso = s.get("before")
            after_s = dt.time.fromisoformat(after_iso) if after_iso else None
            before_s = dt.time.fromisoformat(before_iso) if before_iso else None

            if preferred_time:
                return _try_specific_slot_or_alternatives(phone, calendar, date_, preferred_time)

            slots = find_slots(calendar, date_, after_s, before_s, limit=5, max_days=10)
            if not slots:
                return "Non vedo disponibilità in quel giorno/fascia. Vuoi un altro giorno o un altro orario?"
            set_session(phone, state="choose", options=[x.isoformat() for x in slots])
            return _render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # =========================
    # NORMAL FLOW
    # =========================

    # solo data (es: “17/12”)
    if date_ and not time_ and not after and not before:
        set_session(phone, state="need_time", preferred_date=date_.isoformat())
        return "Perfetto 👍 A che ora preferisci? (es. 17:30) oppure dimmi una fascia (es. “dopo le 18”)."

    # solo ora o fascia (es: “18”, “sera”, “dopo le 18”) senza data
    if (time_ or after or before) and not date_:
        set_session(
            phone,
            state="need_date",
            preferred_time=(time_.isoformat() if time_ else None),
            after=(after.isoformat() if after else None),
            before=(before.isoformat() if before else None),
        )
        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # data + ora precisa
    if date_ and time_:
        return _try_specific_slot_or_alternatives(phone, calendar, date_, time_)

    # data + fascia (dopo/prima/sera etc)
    if date_ and (after or before) and not time_:
        slots = find_slots(calendar, date_, after, before, limit=5, max_days=10)
        if not slots:
            return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
        set_session(phone, state="choose", options=[x.isoformat() for x in slots])
        return _render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

    # “hai posto domani?” / “disponibilità”
    if ("hai posto" in tlow) or any(k in tlow for k in AVAILABILITY_HINTS):
        preferred_date = date_
        if not preferred_date and "domani" in tlow:
            preferred_date = now_local().date() + dt.timedelta(days=1)
        slots = find_slots(calendar, preferred_date, after, before, limit=5, max_days=10)
        if not slots:
            return "Non vedo disponibilità a breve. Dimmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)."
        set_session(phone, state="choose", options=[x.isoformat() for x in slots])
        return _render_slots("Ecco i prossimi orari liberi:", slots)

    # se parla ma non capisco -> help
    if not wants_booking:
        return help_text()

    return "Dimmi giorno e ora (es. “mercoledì 18:00”) oppure una fascia (es. “domani sera dopo le 17:30”)."


def _render_slots(title: str, slots: List[dt.datetime]) -> str:
    lines = [title]
    for i, sl in enumerate(slots, start=1):
        sl = to_local(sl)
        lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
    lines.append("\nRispondi con il numero oppure scrivi una preferenza (es. “mercoledì dopo le 18”).")
    return "\n".join(lines)


def _try_specific_slot_or_alternatives(phone: str, calendar, date_: dt.date, time_: dt.time) -> str:
    # controllo orari negozio
    if not within_business_hours(date_, time_):
        # alternativa: stesso giorno qualsiasi slot valido
        slots = find_slots(calendar, date_, None, None, limit=5, max_days=10)
        if not slots:
            return "Siamo chiusi o non ho disponibilità in quel momento. Vuoi un altro giorno o una fascia?"
        set_session(phone, state="choose", options=[x.isoformat() for x in slots])
        return _render_slots("In quell’orario non siamo disponibili. Ecco alcune alternative:", slots)

    start = to_local(dt.datetime.combine(date_, time_))
    end = start + dt.timedelta(minutes=SLOT_MINUTES)

    try:
        if is_free(calendar, start, end):
            set_session(phone, state="confirm", chosen_iso=start.isoformat())
            return (
                "Perfetto 👍 Confermi questo appuntamento?\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {format_dt(start)}\n\n"
                "Rispondi OK per confermare oppure “annulla”."
            )

        # slot occupato -> propone alternative (stesso giorno, e poi giorni successivi)
        slots = find_slots(calendar, date_, None, None, limit=5, max_days=10)
        if not slots:
            return "A quell’ora non ho posto e non trovo alternative nei prossimi giorni. Vuoi un altro orario?"
        set_session(phone, state="choose", options=[x.isoformat() for x in slots])
        return _render_slots("A quell’ora non ho posto. Ecco alcune alternative:", slots)

    except HttpError as e:
        return f"Errore Google Calendar ({getattr(e.resp,'status', '??')}). Controlla Calendar API e condivisione calendario."
    except Exception as e:
        return f"Problema tecnico nel controllare l’agenda ({type(e).__name__}). Riprova tra poco."


# =========================
# ROUTES
# =========================

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

@app.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone", "+393000000000")
    msg = request.args.get("msg", "ciao")

    reply = handle_message(phone, msg)

    return {
        "phone": phone,
        "message_in": msg,
        "bot_reply": reply
    }

# ✅ TEST SENZA TWILIO (browser)
# Esempio:
# /test?phone=+39333&msg=hai%20posto%20domani%20sera%20dopo%20le%2018
@app.route("/test", methods=["GET", "POST"])
def test_endpoint():
    phone = request.values.get("phone", "test_user")
    msg = request.values.get("msg", "") or request.values.get("message", "")
    msg = (msg or "").strip()

    if not msg:
        return jsonify({
            "error": "Usa ?msg=... e opzionale ?phone=...",
            "examples": [
                "/test?msg=ciao",
                "/test?phone=+39333&msg=hai posto domani?",
                "/test?phone=+39333&msg=mercoledì dopo le 18",
                "/test?phone=+39333&msg=17/12 alle 18:00",
            ],
            "session": SESSIONS.get(phone, {})
        }), 400

    reply = handle_message(phone, msg)
    return jsonify({
        "phone": phone,
        "user": msg,
        "bot": reply,
        "session": SESSIONS.get(phone, {})
    })

# ✅ WHATSAPP / TWILIO (opzionale)
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    if MessagingResponse is None:
        return "Twilio non installato. Usa /test per provare senza Twilio.", 500

    phone = request.form.get("From", "").strip()
    body = request.form.get("Body", "").strip()
    if not body:
        body = "ciao"

    reply = handle_message(phone, body)

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
