from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import Dict, List, Optional, Tuple

from flask import Flask, request, jsonify

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
APP_TZ = os.getenv("APP_TZ", "Europe/Rome")
TZ = ZoneInfo(APP_TZ) if ZoneInfo else None

SLOT_MINUTES = 30
SERVICE_ID_DEFAULT = "taglio_uomo"

# Orari REALI negozio (ultimi che mi hai dato):
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

SERVICES = {
    "taglio_uomo": {"label": "Taglio uomo", "duration": 30, "aliases": ["taglio", "taglio uomo", "capelli", "barbiere"]},
}

CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}

WEEKDAYS_IT = {
    "lunedì": 0, "lunedi": 0, "lun": 0,
    "martedì": 1, "martedi": 1, "mar": 1,
    "mercoledì": 2, "mercoledi": 2, "mer": 2,
    "giovedì": 3, "giovedi": 3, "gio": 3,
    "venerdì": 4, "venerdi": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}


GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID") or ""
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or ""

app = Flask(__name__)

# memoria breve (solo per test): per phone teniamo stato + ultime opzioni
SESSIONS: Dict[str, dict] = {}

_CALENDAR = None


# =========================
# TIME UTILS
# =========================
def now_local() -> dt.datetime:
    if TZ:
        return dt.datetime.now(TZ)
    return dt.datetime.now()

def to_local(d: dt.datetime) -> dt.datetime:
    if TZ and d.tzinfo is None:
        return d.replace(tzinfo=TZ)
    return d

def round_to_next_slot(d: dt.datetime) -> dt.datetime:
    d = to_local(d).replace(second=0, microsecond=0)
    minutes = (d.minute // SLOT_MINUTES) * SLOT_MINUTES
    base = d.replace(minute=minutes)
    if base < d:
        base += dt.timedelta(minutes=SLOT_MINUTES)
    return base

def parse_time(text: str) -> Optional[dt.time]:
    t = text.strip().lower()
    # 17:30 o 17.30
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))
    # 1730
    m3 = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m3:
        return dt.time(int(m3.group(1)), int(m3.group(2)))
    # 17
    m2 = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m2:
        return dt.time(int(m2.group(1)), 0)
    return None

def parse_date(text: str) -> Optional[dt.date]:
    t = text.strip().lower()
    # 17/12 o 17-12 o 17/12/2025
    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year_raw = m.group(3)
    year = now_local().year
    if year_raw:
        y = int(year_raw)
        year = (2000 + y) if y < 100 else y
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

    if "mattina" in t:
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)
    if "pomeriggio" in t:
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)
    if "sera" in t:
        after = after or dt.time(17, 30)
        before = before or dt.time(21, 30)

    return after, before

def within_business_hours(date_: dt.date, t: dt.time, duration_min: int) -> bool:
    intervals = BUSINESS_HOURS.get(date_.weekday(), [])
    if not intervals:
        return False
    slot_end_dt = dt.datetime.combine(date_, t) + dt.timedelta(minutes=duration_min)
    slot_end = slot_end_dt.time()
    for start_s, end_s in intervals:
        hs, ms = map(int, start_s.split(":"))
        he, me = map(int, end_s.split(":"))
        start = dt.time(hs, ms)
        end = dt.time(he, me)
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
    global _CALENDAR
    if _CALENDAR is not None:
        return _CALENDAR

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Manca GOOGLE_SERVICE_ACCOUNT_JSON in Railway Variables")

    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("Manca GOOGLE_CALENDAR_ID in Railway Variables")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    _CALENDAR = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _CALENDAR

def is_free(calendar, start: dt.datetime, end: dt.datetime) -> bool:
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    fb = calendar.freebusy().query(body=body).execute()
    busy = fb["calendars"][GOOGLE_CALENDAR_ID].get("busy", [])
    return len(busy) == 0

def create_event(calendar, start: dt.datetime, end: dt.datetime, phone: str, service_id: str) -> str:
    service = SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT])
    event = {
        "summary": service["label"],
        "start": {"dateTime": start.isoformat(), "timeZone": APP_TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": APP_TZ},
        "description": f"Prenotazione\nTelefono: {phone}",
        "extendedProperties": {
            "private": {
                "phone": phone,
                "service_id": service_id,
                "duration_min": str(service.get("duration", SLOT_MINUTES)),
            }
        },
    }
    created = calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("id", "")


# =========================
# SLOTS
# =========================
def find_slots(
    calendar,
    preferred_date: Optional[dt.date],
    after: Optional[dt.time],
    before: Optional[dt.time],
    duration_min: int,
    limit: int = 5,
    max_days: int = 10
) -> List[dt.datetime]:
    slots: List[dt.datetime] = []
    today = now_local().date()
    start_date = preferred_date or today

    for day_offset in range(0, max_days + 1):
        d = start_date + dt.timedelta(days=day_offset)
        intervals = BUSINESS_HOURS.get(d.weekday(), [])
        if not intervals:
            continue

        for start_s, end_s in intervals:
            hs, ms = map(int, start_s.split(":"))
            he, me = map(int, end_s.split(":"))
            start_dt = to_local(dt.datetime.combine(d, dt.time(hs, ms)))
            end_dt = to_local(dt.datetime.combine(d, dt.time(he, me)))

            if after:
                start_dt = max(start_dt, to_local(dt.datetime.combine(d, after)))
            if before:
                end_dt = min(end_dt, to_local(dt.datetime.combine(d, before)))

            if end_dt <= start_dt:
                continue

            if d == today:
                start_dt = max(start_dt, round_to_next_slot(now_local()))

            cur = round_to_next_slot(start_dt)
            while cur + dt.timedelta(minutes=duration_min) <= end_dt:
                if within_business_hours(d, cur.time(), duration_min):
                    cur_end = cur + dt.timedelta(minutes=duration_min)
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
# CONVERSATION
# =========================
def help_text() -> str:
    return (
        "Ciao! Prenoto appuntamenti per *Taglio uomo* 💈\n"
        "Scrivimi ad esempio:\n"
        "• “Hai posto domani sera dopo le 18?”\n"
        "• “Vorrei prenotare il 17/12 alle 18:00”\n"
        "• “Mercoledì dopo le 18”"
    )

def detect_service_id(text: str) -> str:
    tlow = text.lower()
    for sid, meta in SERVICES.items():
        for a in meta.get("aliases", []):
            if a in tlow:
                return sid
    return SERVICE_ID_DEFAULT

def set_session(phone: str, **kwargs):
    s = SESSIONS.get(phone, {})
    s.update(kwargs)
    SESSIONS[phone] = s

def reset_flow(phone: str):
    SESSIONS[phone] = {}

def render_slots(title: str, slots: List[dt.datetime]) -> str:
    lines = [title]
    for i, sl in enumerate(slots, start=1):
        lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
    lines.append("\nRispondi con il numero oppure scrivi una nuova preferenza (es. “mercoledì dopo le 18”).")
    return "\n".join(lines)

def try_specific_or_alternatives(phone: str, calendar, date_: dt.date, time_: dt.time, service_id: str) -> str:
    duration = int(SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT]).get("duration", SLOT_MINUTES))

    if not within_business_hours(date_, time_, duration):
        slots = find_slots(calendar, date_, None, None, duration, limit=5, max_days=10)
        if not slots:
            return "Siamo chiusi in quell’orario 😕 Dimmi un altro giorno o fascia."
        set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
        return render_slots("In quell’orario non possiamo. Ecco alcune alternative:", slots)

    start = to_local(dt.datetime.combine(date_, time_))
    end = start + dt.timedelta(minutes=duration)

    if is_free(calendar, start, end):
        set_session(phone, state="confirm", chosen_iso=start.isoformat(), service_id=service_id)
        return (
            "Perfetto 👍 Confermi questo appuntamento?\n"
            f"💈 {SERVICES[service_id]['label']}\n"
            f"🕒 {format_dt(start)}\n\n"
            "Rispondi OK per confermare oppure “annulla”."
        )

    # occupato → alternative stesso giorno, poi giorni successivi
    slots = find_slots(calendar, date_, None, None, duration, limit=5, max_days=10)
    if not slots:
        return "A quell’ora non ho posto e non trovo alternative a breve. Vuoi un altro orario?"
    set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
    return render_slots("A quell’ora non ho posto. Ecco alcune alternative:", slots)

def handle_message(phone: str, text: str) -> str:
    t = (text or "").strip()
    tlow = t.lower()

    if not t:
        return help_text()

    # annulla
    if any(w in tlow for w in CANCEL_WORDS):
        reset_flow(phone)
        return "Ok 👍 annullato. Dimmi giorno e ora (es. “mercoledì 18:00” o “domani sera dopo le 18”)."

    service_id = detect_service_id(t)
    duration = int(SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT]).get("duration", SLOT_MINUTES))

    s = SESSIONS.get(phone, {})
    state = s.get("state")

    # parse preferenze
    date_ = parse_date(t) or parse_relative_day(t)
    time_ = parse_time(t)
    after, before = time_window_from_text(t)

    # calendar
    calendar = get_calendar()

    # STATE: confirm
    if state == "confirm":
        if tlow.strip() in CONFIRM_WORDS:
            chosen_iso = s.get("chosen_iso")
            service_id = s.get("service_id", service_id)
            duration = int(SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT]).get("duration", SLOT_MINUTES))

            if not chosen_iso:
                reset_flow(phone)
                return "Ops, ho perso lo slot. Dimmi giorno e ora e riproviamo."

            start = to_local(dt.datetime.fromisoformat(chosen_iso))
            end = start + dt.timedelta(minutes=duration)

            if not is_free(calendar, start, end):
                reset_flow(phone)
                return "Quello slot è stato appena preso 😅 Vuoi che ti proponga altri orari?"

            create_event(calendar, start, end, phone, service_id)
            reset_flow(phone)
            return f"✅ Appuntamento confermato!\n💈 {SERVICES[service_id]['label']}\n🕒 {format_dt(start)}\n\nA presto 👋"

        # se non è conferma, lo tratto come nuova preferenza
        set_session(phone, state=None, chosen_iso=None)

    # STATE: choose
    if state == "choose":
        options: List[str] = s.get("options", [])
        n = parse_choice_number(t)
        if n and 1 <= n <= len(options):
            chosen = to_local(dt.datetime.fromisoformat(options[n - 1]))
            set_session(phone, state="confirm", chosen_iso=options[n - 1], service_id=s.get("service_id", service_id))
            return (
                "Confermi questo appuntamento?\n"
                f"💈 {SERVICES[s.get('service_id', service_id)]['label']}\n"
                f"🕒 {format_dt(chosen)}\n\n"
                "Rispondi OK per confermare oppure “annulla”."
            )
        # NON bloccare: se scrive “sera”, “17:30”, “mercoledì dopo le 18”, ricalcolo
        set_session(phone, state=None, options=None)

    # Se ha solo una data → chiedi ora/fascia
    if date_ and not time_ and not after and not before:
        set_session(phone, state="need_time", preferred_date=date_.isoformat(), service_id=service_id)
        return "Perfetto 👍 A che ora preferisci? (es. 17:30) oppure dimmi una fascia (es. “dopo le 18”)."

    # Se ha solo ora/fascia → chiedi giorno
    if (time_ or after or before) and not date_:
        set_session(phone, state="need_date", preferred_time=(time_.isoformat() if time_ else None),
                    after=(after.isoformat() if after else None), before=(before.isoformat() if before else None),
                    service_id=service_id)
        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # STATE: need_time
    if state == "need_time":
        preferred_date = dt.date.fromisoformat(s.get("preferred_date")) if s.get("preferred_date") else None
        service_id = s.get("service_id", service_id)
        duration = int(SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT]).get("duration", SLOT_MINUTES))

        if preferred_date:
            if time_:
                return try_specific_or_alternatives(phone, calendar, preferred_date, time_, service_id)
            if after or before:
                slots = find_slots(calendar, preferred_date, after, before, duration, limit=5, max_days=10)
                if not slots:
                    return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
                set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
                return render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

        return "A che ora preferisci? (es. 17:30) oppure dimmi una fascia (es. “dopo le 18”)."

    # STATE: need_date
    if state == "need_date":
        service_id = s.get("service_id", service_id)
        duration = int(SERVICES.get(service_id, SERVICES[SERVICE_ID_DEFAULT]).get("duration", SLOT_MINUTES))

        if date_:
            pref_time = dt.time.fromisoformat(s["preferred_time"]) if s.get("preferred_time") else None
            a = dt.time.fromisoformat(s["after"]) if s.get("after") else None
            b = dt.time.fromisoformat(s["before"]) if s.get("before") else None

            if pref_time:
                return try_specific_or_alternatives(phone, calendar, date_, pref_time, service_id)

            slots = find_slots(calendar, date_, a, b, duration, limit=5, max_days=10)
            if not slots:
                return "Non vedo disponibilità in quel giorno/fascia. Vuoi un altro giorno o un altro orario?"
            set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
            return render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

        return "Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # Data + ora precisa
    if date_ and time_:
        return try_specific_or_alternatives(phone, calendar, date_, time_, service_id)

    # Data + fascia
    if date_ and (after or before) and not time_:
        slots = find_slots(calendar, date_, after, before, duration, limit=5, max_days=10)
        if not slots:
            return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
        set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
        return render_slots("Perfetto 👍 Ecco alcune disponibilità:", slots)

    # “hai posto ...” → proponi slot
    if "posto" in tlow or "disponib" in tlow or "hai posto" in tlow:
        pref = date_
        if not pref and "domani" in tlow:
            pref = now_local().date() + dt.timedelta(days=1)
        slots = find_slots(calendar, pref, after, before, duration, limit=5, max_days=10)
        if not slots:
            return "Non vedo disponibilità a breve. Dimmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)."
        set_session(phone, state="choose", options=[x.isoformat() for x in slots], service_id=service_id)
        return render_slots("Ecco i prossimi orari liberi:", slots)

    # fallback
    if tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"}:
        return help_text()

    return "Dimmi giorno e ora (es. “mercoledì 18:00”) oppure una fascia (es. “domani sera dopo le 18”)."


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
    return jsonify({
        "phone": phone,
        "message_in": msg,
        "bot_reply": reply,
        "session": SESSIONS.get(phone, {}),
        "calendar_id": GOOGLE_CALENDAR_ID,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
