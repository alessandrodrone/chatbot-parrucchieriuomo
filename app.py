from __future__ import annotations

import os
import re
import json
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # fallback (ma su Railway di solito va)


# =========================
# CONFIG
# =========================
APP_TZ = "Europe/Rome"
TZ = ZoneInfo(APP_TZ) if ZoneInfo else None

SERVICE_NAME = "Taglio uomo"
SLOT_MINUTES = 30

# Orari REALI del negozio:
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

# parole chiave
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

app = Flask(__name__)

# =========================
# GOOGLE CALENDAR AUTH
# =========================
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID") or "primary"

def build_calendar_service():
    """
    Supporta 2 modalità:
    A) GOOGLE_SERVICE_ACCOUNT_JSON (consigliata su Railway)
    B) credentials.json nel repo
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    scopes = ["https://www.googleapis.com/auth/calendar"]

    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        # fallback file (solo se esiste davvero nel repo)
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=scopes)

    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def get_calendar():
    return build_calendar_service()

# =========================
# MEMORIA BREVE (sessione)
# =========================
SESSIONS: Dict[str, dict] = {}

# =========================
# UTILS
# =========================
def now_local() -> dt.datetime:
    if TZ:
        return dt.datetime.now(TZ)
    return dt.datetime.now()

def parse_time(text: str) -> Optional[dt.time]:
    """
    Accetta: 17:30, 17.30, 1730, 17
    """
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
    """
    Accetta: 17/12, 17-12, 17/12/2025
    """
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

    # giorno della settimana
    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            return next_weekday(wd)
    return None

def time_window_from_text(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    """
    Estrae vincoli tipo:
    - "dopo le 18"
    - "prima delle 12"
    - "sera", "mattina", "pomeriggio"
    """
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
        before = before or dt.time(22, 0)

    return after, before

def within_business_hours(date_: dt.date, t: dt.time) -> bool:
    intervals = BUSINESS_HOURS.get(date_.weekday(), [])
    for start_s, end_s in intervals:
        hs, ms = map(int, start_s.split(":"))
        he, me = map(int, end_s.split(":"))
        start = dt.time(hs, ms)
        end = dt.time(he, me)
        # slot deve finire entro orario di chiusura
        slot_end_dt = dt.datetime.combine(date_, t) + dt.timedelta(minutes=SLOT_MINUTES)
        slot_end = slot_end_dt.time()
        if start <= t and slot_end <= end:
            return True
    return False

def round_to_next_slot(d: dt.datetime) -> dt.datetime:
    minutes = (d.minute // SLOT_MINUTES) * SLOT_MINUTES
    base = d.replace(minute=minutes, second=0, microsecond=0)
    if base < d:
        base += dt.timedelta(minutes=SLOT_MINUTES)
    return base

def format_dt(d: dt.datetime) -> str:
    return d.strftime("%a %d/%m %H:%M").replace("Mon", "Lun").replace("Tue", "Mar").replace("Wed", "Mer").replace("Thu", "Gio").replace("Fri", "Ven").replace("Sat", "Sab").replace("Sun", "Dom")

def parse_choice_number(text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\b", text.strip())
    if not m:
        return None
    return int(m.group(1))

# =========================
# GOOGLE CALENDAR HELPERS
# =========================
def is_free(calendar, start: dt.datetime, end: dt.datetime) -> bool:
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    fb = calendar.freebusy().query(body=body).execute()
    busy = fb["calendars"][GOOGLE_CALENDAR_ID].get("busy", [])
    return len(busy) == 0

def create_event(calendar, start: dt.datetime, end: dt.datetime, phone: str) -> str:
    event = {
        "summary": SERVICE_NAME,
        "start": {"dateTime": start.isoformat(), "timeZone": APP_TZ},
        "end": {"dateTime": end.isoformat(), "timeZone": APP_TZ},
        "description": f"Prenotazione WhatsApp\nTelefono: {phone}",
        "extendedProperties": {"private": {"phone": phone, "service": "taglio_uomo"}},
    }
    created = calendar.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("id", "")

def get_customer_history(calendar, phone: str) -> Tuple[int, Optional[dt.datetime]]:
    """
    Memoria lunga "gratis" dal calendario:
    - conta appuntamenti
    - ultimo appuntamento
    """
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
            last = items[-1]
            s = last["start"].get("dateTime")
            if s:
                last_dt = dt.datetime.fromisoformat(s)
        return count, last_dt
    except Exception:
        return 0, None

def find_slots(calendar, preferred_date: Optional[dt.date], after: Optional[dt.time], before: Optional[dt.time], limit: int = 5, max_days: int = 10) -> List[dt.datetime]:
    """
    Cerca slot liberi rispettando:
    - orari negozio
    - vincoli after/before
    - calendario occupato
    """
    slots: List[dt.datetime] = []
    today = now_local().date()
    start_date = preferred_date or today

    for day_offset in range(0, max_days + 1):
        d = start_date + dt.timedelta(days=day_offset)
        # se negozio chiuso, skip
        if not BUSINESS_HOURS.get(d.weekday(), []):
            continue

        # finestre del giorno
        intervals = BUSINESS_HOURS[d.weekday()]
        for start_s, end_s in intervals:
            hs, ms = map(int, start_s.split(":"))
            he, me = map(int, end_s.split(":"))
            start_dt = dt.datetime.combine(d, dt.time(hs, ms))
            end_dt = dt.datetime.combine(d, dt.time(he, me))

            # applica vincoli after/before
            if after:
                start_dt = max(start_dt, dt.datetime.combine(d, after))
            if before:
                end_dt = min(end_dt, dt.datetime.combine(d, before))

            # se finestra troppo piccola
            if end_dt <= start_dt:
                continue

            # se oggi: non proporre passato
            if d == today:
                start_dt = max(start_dt, round_to_next_slot(now_local()))

            cur = round_to_next_slot(start_dt)

            while cur + dt.timedelta(minutes=SLOT_MINUTES) <= end_dt:
                cur_end = cur + dt.timedelta(minutes=SLOT_MINUTES)
                # check business
                if within_business_hours(d, cur.time()):
                    try:
                        if is_free(calendar, cur, cur_end):
                            slots.append(cur)
                            if len(slots) >= limit:
                                return slots
                    except HttpError:
                        # se API non va, interrompi
                        return slots
                cur += dt.timedelta(minutes=SLOT_MINUTES)

    return slots

# =========================
# CONVERSATION LOGIC
# =========================
def help_text() -> str:
    return (
        "Ciao! Io gestisco solo le prenotazioni per taglio uomo 💈\n"
        "Scrivimi ad esempio:\n"
        "- “Hai posto domani?”\n"
        "- “Vorrei prenotare il 17/12 alle 18:00”\n"
        "- “Mercoledì dopo le 18”"
    )

def set_session(phone: str, **kwargs):
    s = SESSIONS.get(phone, {})
    s.update(kwargs)
    # “memoria breve” anti-reset: conserva solo cose utili
    SESSIONS[phone] = s

def reset_flow(phone: str):
    keep = {}
    # puoi mantenere preferenze future se vuoi; qui resetto tutto
    SESSIONS[phone] = keep

def handle_message(phone: str, text: str) -> str:
    t = text.strip()
    tlow = t.lower()

    # cancella
    if any(w in tlow for w in CANCEL_WORDS):
        reset_flow(phone)
        return "Va bene 👍 Prenotazione annullata. Se vuoi riprovare, dimmi giorno e ora (es. “Mercoledì 18:00”)."

    # sessione
    s = SESSIONS.get(phone, {})
    state = s.get("state")

    # prova a costruire riferimenti data/ora dal messaggio
    abs_date = parse_date(t)
    rel_date = parse_relative_day(t)
    date_ = abs_date or rel_date

    time_ = parse_time(t)
    after, before = time_window_from_text(t)

    wants_booking = any(k in tlow for k in BOOKING_HINTS) or bool(date_) or bool(time_) or ("dopo" in tlow) or ("prima" in tlow)

    # stato: in attesa di conferma finale
    if state == "confirm":
        if tlow.strip() in CONFIRM_WORDS or any(w == tlow.strip() for w in CONFIRM_WORDS):
            chosen_iso = s.get("chosen_iso")
            if not chosen_iso:
                reset_flow(phone)
                return "Ops, ho perso lo slot. Riproviamo: che giorno e a che ora preferisci?"
            start = dt.datetime.fromisoformat(chosen_iso)
            end = start + dt.timedelta(minutes=SLOT_MINUTES)

            try:
                calendar = get_calendar()
                # ricontrollo libero (race condition)
                if not is_free(calendar, start, end):
                    reset_flow(phone)
                    return "Quello slot è appena stato occupato 😅 Vuoi che ti proponga altri orari?"

                create_event(calendar, start, end, phone)
                reset_flow(phone)
                return (
                    f"✅ Appuntamento confermato!\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {format_dt(start)}\n\n"
                    f"A presto 👋"
                )
            except Exception as e:
                reset_flow(phone)
                return f"Ho un problema tecnico nel salvare in agenda ({type(e).__name__}). Riprova tra poco."

        # se invece risponde con altra richiesta tipo “sera dopo le 18”
        # allora interpreto come modifica preferenza e continuo
        state = None
        s["state"] = None
        SESSIONS[phone] = s

    # stato: lista slot mostrata (scegli 1..N) — qui ACCETTO anche orari/nuove preferenze
    if state == "choose":
        options: List[str] = s.get("options", [])  # ISO strings
        n = parse_choice_number(t)
        if n and 1 <= n <= len(options):
            chosen = dt.datetime.fromisoformat(options[n - 1])
            set_session(phone, state="confirm", chosen_iso=options[n - 1])
            return (
                "Confermi questo appuntamento?\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {format_dt(chosen)}\n\n"
                "Rispondi OK per confermare oppure “annulla”."
            )

        # se non è un numero valido, provo a capire se sta chiedendo “sera”, “mercoledì dopo le 18”, “17:30”, ecc.
        if wants_booking:
            # proseguo sotto generando nuovi slot coerenti
            pass
        else:
            return "Dimmi un numero della lista oppure scrivi una preferenza tipo “mercoledì dopo le 18” 🙂"

    # se non capisco e non è booking
    if not wants_booking:
        # saluto o help
        if tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"}:
            try:
                calendar = get_calendar()
                count, last_dt = get_customer_history(calendar, phone)
                if count > 0 and last_dt:
                    return (
                        f"Ciao! Bentornato 😊\n"
                        f"Vedo che hai già prenotato da noi {count} volta/e. Ultima: {format_dt(last_dt)}.\n\n"
                        f"Quando vuoi venire per il prossimo {SERVICE_NAME}?"
                    )
            except Exception:
                pass
            return help_text()

        return help_text()

    # =========================
    # BOOKING FLOW (stateless + session)
    # =========================

    # se l’utente scrive solo una data (es: “il 17/12”) chiedi l’orario o fascia
    if date_ and not time_ and not after and not before:
        set_session(phone, state="need_time", preferred_date=date_.isoformat())
        return "Perfetto 👍 A che ora preferisci? (es. 17:30) oppure dimmi una fascia (es. “dopo le 18”)."

    # se l’utente scrive solo orario (es: “alle 18”) senza data: chiedi giorno
    if (time_ or after or before) and not date_:
        set_session(phone, state="need_date", after=(after.isoformat() if after else None), before=(before.isoformat() if before else None), preferred_time=(time_.isoformat() if time_ else None))
        return "Ok 👍 Per che giorno? (es. “domani”, “mercoledì”, “17/12”)."

    # se ho data e un orario preciso: provo quello slot, altrimenti alternative
    preferred_date = date_
    preferred_time = time_
    if preferred_date and preferred_time:
        if not within_business_hours(preferred_date, preferred_time):
            # proponi alternative stesso giorno vicino (se possibile) oppure stesso orario in altro giorno
            try:
                calendar = get_calendar()
                slots = find_slots(calendar, preferred_date, None, None, limit=5, max_days=7)
                if not slots:
                    return "Non trovo disponibilità nei prossimi giorni. Vuoi indicarmi un’altra fascia oraria?"
                set_session(phone, state="choose", options=[s.isoformat() for s in slots])
                lines = ["Ecco i prossimi orari liberi:"]
                for i, sl in enumerate(slots, start=1):
                    lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
                lines.append("\nRispondi con il numero oppure scrivi una preferenza (es. “mercoledì dopo le 18”).")
                return "\n".join(lines)
            except Exception:
                return "Ho un problema tecnico nel controllare l’agenda. Riprova tra poco."
        try:
            calendar = get_calendar()
            start = dt.datetime.combine(preferred_date, preferred_time)
            if TZ:
                start = start.replace(tzinfo=TZ)
            end = start + dt.timedelta(minutes=SLOT_MINUTES)

            if is_free(calendar, start, end):
                set_session(phone, state="confirm", chosen_iso=start.isoformat())
                return (
                    "Perfetto 👍 Confermi questo appuntamento?\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {format_dt(start)}\n\n"
                    "Rispondi OK per confermare oppure “annulla”."
                )

            # non libero: cerca alternative stesso giorno vicino all’orario, altrimenti giorni successivi stessa fascia
            slots = find_slots(calendar, preferred_date, None, None, limit=5, max_days=7)
            if not slots:
                return "A quell’ora non ho disponibilità e non trovo alternative nei prossimi giorni. Vuoi un altro orario?"
            set_session(phone, state="choose", options=[s.isoformat() for s in slots])
            lines = ["A quell’ora non ho posto. Ecco i prossimi orari liberi:"]
            for i, sl in enumerate(slots, start=1):
                lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
            lines.append("\nRispondi con il numero oppure scrivi un orario/preferenza (es. “domani sera dopo le 18”).")
            return "\n".join(lines)

        except HttpError as e:
            # Mostra messaggio chiaro
            return f"Errore Google Calendar ({e.resp.status}). Controlla che Calendar API sia attiva e che il calendario sia condiviso col service account."
        except Exception as e:
            return f"Problema tecnico nel controllare l’agenda ({type(e).__name__}). Riprova tra poco."

    # se ho data + vincolo fascia (dopo/prima) senza orario esatto: proponi slot coerenti
    if preferred_date and (after or before) and not preferred_time:
        try:
            calendar = get_calendar()
            slots = find_slots(calendar, preferred_date, after, before, limit=5, max_days=7)
            if not slots:
                return "Non vedo disponibilità in quella fascia. Vuoi un altro orario o un altro giorno?"
            set_session(phone, state="choose", options=[s.isoformat() for s in slots])
            lines = ["Perfetto 👍 Ecco alcune disponibilità:"]
            for i, sl in enumerate(slots, start=1):
                lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
            lines.append("\nRispondi con il numero oppure scrivi un altro orario (es. 19:00).")
            return "\n".join(lines)
        except Exception:
            return "Ho un problema tecnico nel controllare l’agenda. Riprova tra poco."

    # se l’utente chiede “hai posto domani?” ecc: proponi subito slot
    if any(k in tlow for k in AVAILABILITY_HINTS) or "hai posto" in tlow:
        preferred_date = preferred_date or (now_local().date() + dt.timedelta(days=1) if "domani" in tlow else None)
        try:
            calendar = get_calendar()
            slots = find_slots(calendar, preferred_date, after, before, limit=5, max_days=10)
            if not slots:
                return "Non vedo disponibilità a breve. Vuoi indicarmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)?"
            set_session(phone, state="choose", options=[s.isoformat() for s in slots])
            lines = ["Ecco i prossimi orari liberi:"]
            for i, sl in enumerate(slots, start=1):
                lines.append(f"{i}) {sl.strftime('%d/%m %H:%M')}")
            lines.append("\nRispondi con il numero oppure scrivi una preferenza (es. “mercoledì dopo le 18”).")
            return "\n".join(lines)
        except Exception:
            return "Ho un problema tecnico nel controllare l’agenda. Riprova tra poco."

    # fallback: se booking ma non ho abbastanza info
    return "Ok 👍 Dimmi per che giorno e che ora preferisci (es. “mercoledì 18:00” oppure “domani sera dopo le 17:30”)."


# =========================
# ROUTES
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "").strip()  # es: whatsapp:+39...
    body = request.form.get("Body", "").strip()

    if not body:
        body = "ciao"

    try:
        reply = handle_message(phone, body)
    except Exception as e:
        reply = f"Ho avuto un problema tecnico ({type(e).__name__}). Riprova tra poco."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
