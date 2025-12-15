from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, json, re
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = Flask(__name__)
TZ = ZoneInfo("Europe/Rome")

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Manca OPENAI_API_KEY nelle variabili Railway.")
client = OpenAI(api_key=OPENAI_API_KEY)
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
if not CALENDAR_ID:
    raise RuntimeError("Manca GOOGLE_CALENDAR_ID nelle variabili Railway (metti l'ID @group.calendar.google.com).")

# credenziali Google: o file (credentials.json) oppure variabile GOOGLE_CREDENTIALS_JSON
creds_path = "credentials.json"
creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
if creds_env:
    # scrive il file ogni avvio (ok per Railway)
    with open(creds_path, "w", encoding="utf-8") as f:
        f.write(creds_env)

if not os.path.exists(creds_path):
    raise RuntimeError("Manca credentials.json (o GOOGLE_CREDENTIALS_JSON).")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
credentials = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
calendar = build("calendar", "v3", credentials=credentials)

SERVICE_NAME = "Taglio uomo"
SLOT_MINUTES = 30

# =========================
# ORARI PARRUCCHIERE
# lun=0 ... dom=6
# =========================
OPENING = {
    1: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mar
    2: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mer
    3: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # gio
    4: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # ven
    5: [(time(8, 30), time(13, 0)), (time(15, 0), time(18, 0))],  # sab
}

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}

# stati
IDLE = "IDLE"
ASK_DAY = "ASK_DAY"
ASK_TIME = "ASK_TIME"
OFFER_PICK = "OFFER_PICK"          # scegli 1/2/3
CONFIRM = "CONFIRM"                # conferma OK/annulla
ASK_ALTERNATIVE = "ASK_ALTERNATIVE" # "Vuoi che ti proponga altri orari?"
WAIT_CUSTOM_TIME = "WAIT_CUSTOM_TIME" # dopo che propone altri, l'utente può scrivere ora o scegliere 1/2/3

CONFIRM_WORDS = {"ok", "va bene", "perfetto", "si", "sì", "confermo", "confermiamo", "bene"}
NEGATIVE_WORDS = {"no", "non posso", "non va bene", "annulla", "cancella", "stop", "niente"}
YES_WORDS = {"si", "sì", "ok", "va bene", "certo", "perfetto", "dai", "y"}

# =========================
# HELPERS
# =========================
def now():
    return datetime.now(TZ)

def combine(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=TZ)

def fmt_slot(s: datetime):
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[s.weekday()]} {s.strftime('%d/%m %H:%M')}"

def normalize_time(text: str) -> str | None:
    t = text.lower().strip()
    # 18:30 o 18.30
    m = re.search(r'(\d{1,2})[:\.](\d{2})', t)
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    # solo ora: "18"
    m = re.search(r'\b(\d{1,2})\b', t)
    if m:
        hh = int(m.group(1))
        if 0 <= hh <= 23:
            return f"{hh:02d}:00"
    return None

def parse_day_simple(text: str) -> date | None:
    low = text.lower()
    today = now().date()
    if "oggi" in low:
        return today
    if "domani" in low:
        return today + timedelta(days=1)

    # dd/mm oppure dd-mm
    m = re.search(r'\b(\d{1,2})[\/\-](\d{1,2})\b', low)
    if m:
        dd = int(m.group(1)); mm = int(m.group(2))
        yy = today.year
        try:
            d = date(yy, mm, dd)
            # se già passato, prova anno prossimo
            if d < today:
                d = date(yy + 1, mm, dd)
            return d
        except Exception:
            return None
    return None

def iter_slots_for_day(d: date):
    wd = d.weekday()
    if wd not in OPENING:
        return
    for start_t, end_t in OPENING[wd]:
        start_dt = combine(d, start_t)
        end_dt = combine(d, end_t)
        cur = start_dt
        while cur + timedelta(minutes=SLOT_MINUTES) <= end_dt:
            yield cur, cur + timedelta(minutes=SLOT_MINUTES)
            cur += timedelta(minutes=SLOT_MINUTES)

def is_free(start_dt: datetime, end_dt: datetime) -> bool:
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": "Europe/Rome",
        "items": [{"id": CALENDAR_ID}],
    }
    fb = calendar.freebusy().query(body=body).execute()
    busy = fb.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    return len(busy) == 0

def create_event(phone: str, start_dt: datetime, end_dt: datetime):
    event = {
        "summary": SERVICE_NAME,
        "description": f"Cliente WhatsApp: {phone}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Rome"},
    }
    calendar.events().insert(calendarId=CALENDAR_ID, body=event).execute()

def find_free_slots(preferred_date: date | None, max_days=14, limit=6):
    slots = []
    start_day = preferred_date if preferred_date else now().date()
    for day_offset in range(0, max_days):
        d = start_day + timedelta(days=day_offset)
        for s, e in iter_slots_for_day(d):
            # evita slot nel passato / troppo imminenti
            if s <= now() + timedelta(minutes=2):
                continue
            if is_free(s, e):
                slots.append((s, e))
                if len(slots) >= limit:
                    return slots
    return slots

def first_free_at_time(d: date, hhmm: str):
    # cerca esattamente slot HH:MM nel giorno d
    try:
        hh, mm = hhmm.split(":")
        s = combine(d, time(int(hh), int(mm)))
        e = s + timedelta(minutes=SLOT_MINUTES)
        # verifica che sia dentro orario di apertura
        ok_open = False
        for a, b in OPENING.get(d.weekday(), []):
            if combine(d, a) <= s and e <= combine(d, b):
                ok_open = True
                break
        if not ok_open:
            return None
        if s <= now() + timedelta(minutes=2):
            return None
        return (s, e) if is_free(s, e) else None
    except Exception:
        return None

# =========================
# GPT: solo per capire intent generale
# =========================
SYSTEM_GPT = """
Sei un assistente WhatsApp per PARRUCCHIERE UOMO.
Devi capire SOLO l'intento:
- book: vuole prenotare / disponibilità / appuntamento / posto / quando sei libero
- info: chiede orari/indirizzo/regole
- other: altro
Rispondi SOLO JSON: {"intent":"book|info|other"}
"""

def gpt_intent(text: str) -> str:
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_GPT},
                {"role": "user", "content": f"Testo: {text}\nRispondi SOLO JSON."},
            ],
            temperature=0.0,
            max_tokens=40,
        )
        obj = json.loads(r.choices[0].message.content.strip())
        return (obj.get("intent") or "other").lower()
    except Exception:
        # fallback semplice
        low = text.lower()
        if any(k in low for k in ["prenot", "appunt", "posto", "dispon", "quando", "domani", "oggi", "taglio"]):
            return "book"
        if any(k in low for k in ["orari", "aperto", "chiuso"]):
            return "info"
        return "other"

# =========================
# MESSAGGI UTILI
# =========================
def msg_welcome():
    return (
        "Ciao! Io gestisco solo le prenotazioni per *taglio uomo* 💈\n"
        "Scrivimi ad esempio: *“Vorrei prenotare un taglio”* oppure *“Hai posto domani?”*."
    )

def msg_hours():
    return (
        "Orari:\n"
        "- Mar–Ven: 08:30–12:00 e 15:00–18:00\n"
        "- Sab: 08:30–13:00 e 15:00–18:00\n"
        "- Lun/Dom: chiuso\n\n"
        "Vuoi prenotare un *taglio uomo*? Dimmi quando preferisci 😊"
    )

# =========================
# WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From", "")
    user_message = (request.form.get("Body", "") or "").strip()
    text = user_message.lower().strip()

    sess = SESSIONS.get(from_number, {"state": IDLE})
    resp = MessagingResponse()

    # 0) comandi veloci
    if text in {"reset", "ricomincia"}:
        SESSIONS[from_number] = {"state": IDLE}
        resp.message("Ok 👍 ricominciamo. " + msg_welcome())
        return str(resp)

    # 1) stato: scelta 1/2/3
    if sess.get("state") == OFFER_PICK:
        if text in {"1", "2", "3"} and sess.get("proposed"):
            idx = int(text) - 1
            if 0 <= idx < len(sess["proposed"]):
                chosen = sess["proposed"][idx]
                start_dt = datetime.fromisoformat(chosen["start"])
                sess["chosen"] = chosen
                sess["state"] = CONFIRM
                SESSIONS[from_number] = sess
                resp.message(
                    f"Confermi questo appuntamento?\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {fmt_slot(start_dt)}\n\n"
                    "Rispondi *OK* per confermare oppure *annulla*."
                )
                return str(resp)
        resp.message("Per favore rispondi con *1*, *2* o *3* 🙂")
        SESSIONS[from_number] = sess
        return str(resp)

    # 2) stato: conferma finale
    if sess.get("state") == CONFIRM:
        if any(w in text for w in NEGATIVE_WORDS):
            sess["state"] = ASK_ALTERNATIVE
            sess.pop("chosen", None)
            SESSIONS[from_number] = sess
            resp.message("Ok 👍 Nessun problema. Vuoi che ti proponga altri orari disponibili?")
            return str(resp)

        if any(w in text for w in CONFIRM_WORDS):
            chosen = sess.get("chosen")
            if not chosen:
                sess["state"] = IDLE
                SESSIONS[from_number] = sess
                resp.message("Mi sono perso lo slot 😅 Vuoi che ti proponga di nuovo gli orari disponibili?")
                return str(resp)

            start_dt = datetime.fromisoformat(chosen["start"])
            end_dt = datetime.fromisoformat(chosen["end"])

            try:
                # ultimo controllo (evita doppie prenotazioni)
                if not is_free(start_dt, end_dt):
                    sess["state"] = ASK_ALTERNATIVE
                    sess.pop("chosen", None)
                    SESSIONS[from_number] = sess
                    resp.message("Ops, quello slot è appena stato occupato. Vuoi che ti proponga altri orari?")
                    return str(resp)

                create_event(from_number, start_dt, end_dt)

                sess["state"] = IDLE
                sess.pop("chosen", None)
                SESSIONS[from_number] = sess
                resp.message(
                    f"✅ Appuntamento confermato!\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {fmt_slot(start_dt)}\n\n"
                    "A presto 👋"
                )
                return str(resp)

            except HttpError as e:
                sess["state"] = IDLE
                SESSIONS[from_number] = sess
                resp.message("Ho un problema tecnico con il calendario. Riprova tra poco, per favore.")
                return str(resp)

        resp.message("Vuoi confermare lo slot? Rispondi *OK* oppure *annulla*.")
        SESSIONS[from_number] = sess
        return str(resp)

    # 3) stato: vuoi alternative?
    if sess.get("state") == ASK_ALTERNATIVE:
        if any(w in text for w in YES_WORDS):
            # proponi 3 slot prossimi
            preferred_date = sess.get("preferred_date")
            slots = []
            try:
                slots = find_free_slots(preferred_date=preferred_date, max_days=14, limit=6)
            except HttpError:
                sess["state"] = IDLE
                SESSIONS[from_number] = sess
                resp.message("Ho un problema tecnico con il calendario. Riprova tra poco.")
                return str(resp)

            if not slots:
                sess["state"] = IDLE
                SESSIONS[from_number] = sess
                resp.message("Non trovo disponibilità nei prossimi giorni. Vuoi indicarmi un giorno specifico?")
                return str(resp)

            propose = slots[:3]
            lines = ["Perfetto 💈 Ecco i prossimi orari liberi (30 minuti):"]
            for i, (s, e) in enumerate(propose, start=1):
                lines.append(f"{i}) {fmt_slot(s)}")
            lines.append("\nRispondi con *1*, *2* o *3* oppure scrivimi un orario (es. 17:30).")
            resp.message("\n".join(lines))

            sess["state"] = OFFER_PICK
            sess["proposed"] = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in propose]
            SESSIONS[from_number] = sess
            return str(resp)

        if any(w in text for w in NEGATIVE_WORDS):
            sess["state"] = IDLE
            SESSIONS[from_number] = sess
            resp.message("Ok 👍 Dimmi tu un giorno e un orario che preferisci e vediamo la disponibilità.")
            return str(resp)

        resp.message("Vuoi che ti proponga altri orari? Rispondi *OK* oppure *no*.")
        SESSIONS[from_number] = sess
        return str(resp)

    # 4) stato: sto aspettando il giorno
    if sess.get("state") == ASK_DAY:
        d = parse_day_simple(user_message)
        if not d:
            resp.message("Perfetto 👍 Che giorno preferisci? (es. *domani*, *oggi*, oppure *16/12*)")
            SESSIONS[from_number] = sess
            return str(resp)

        sess["preferred_date"] = d
        sess["state"] = ASK_TIME
        SESSIONS[from_number] = sess
        resp.message("A che ora preferisci? (es. 15:00, 17:30, 18:00)")
        return str(resp)

    # 5) stato: sto aspettando l'orario
    if sess.get("state") == ASK_TIME:
        hhmm = normalize_time(user_message)
        if not hhmm:
            resp.message("Dimmi un orario valido (es. 15:00, 17:30, 18:00).")
            SESSIONS[from_number] = sess
            return str(resp)

        d = sess.get("preferred_date")
        if not d:
            sess["state"] = ASK_DAY
            SESSIONS[from_number] = sess
            resp.message("Ok 👍 Per che giorno?")
            return str(resp)

        try:
            exact = first_free_at_time(d, hhmm)
        except HttpError:
            sess["state"] = IDLE
            SESSIONS[from_number] = sess
            resp.message("Ho un problema tecnico con il calendario. Riprova tra poco.")
            return str(resp)

        if exact:
            s, e = exact
            sess["chosen"] = {"start": s.isoformat(), "end": e.isoformat()}
            sess["state"] = CONFIRM
            SESSIONS[from_number] = sess
            resp.message(
                f"Perfetto! Confermi:\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {fmt_slot(s)}\n\n"
                "Rispondi *OK* per confermare oppure *annulla*."
            )
            return str(resp)
        else:
            # non disponibile: proponi alternative
            sess["state"] = ASK_ALTERNATIVE
            SESSIONS[from_number] = sess
            resp.message("A quell’ora non ho disponibilità. Vuoi che ti proponga i prossimi orari liberi?")
            return str(resp)

    # =========================
    # STATO IDLE: capiamo cosa vuole
    # =========================
    intent = gpt_intent(user_message)

    if intent == "info":
        resp.message(msg_hours())
        sess["state"] = IDLE
        SESSIONS[from_number] = sess
        return str(resp)

    if intent != "book":
        resp.message(msg_welcome())
        sess["state"] = IDLE
        SESSIONS[from_number] = sess
        return str(resp)

    # intent = booking: prova a capire se ha già indicato giorno e/o ora
    d = parse_day_simple(user_message)
    hhmm = normalize_time(user_message)

    # Caso A: ha scritto solo "domani" o una data
    if d and not hhmm:
        sess["preferred_date"] = d
        sess["state"] = ASK_TIME
        SESSIONS[from_number] = sess
        resp.message("Certo 👍 A che ora preferisci? (es. 15:00, 17:30, 18:00)")
        return str(resp)

    # Caso B: ha scritto solo un orario (senza giorno)
    if hhmm and not d:
        sess["state"] = ASK_DAY
        SESSIONS[from_number] = sess
        resp.message("Perfetto 👍 Per che giorno? (es. domani, 16/12)")
        return str(resp)

    # Caso C: ha scritto giorno + ora insieme
    if d and hhmm:
        sess["preferred_date"] = d
        sess["state"] = ASK_TIME  # riusiamo la stessa logica
        SESSIONS[from_number] = sess
        # simuliamo come se stesse rispondendo all'orario
        # (chiamiamo ricorsivamente la parte ASK_TIME)
        # più semplice: settiamo e facciamo check subito
        try:
            exact = first_free_at_time(d, hhmm)
        except HttpError:
            sess["state"] = IDLE
            SESSIONS[from_number] = sess
            resp.message("Ho un problema tecnico con il calendario. Riprova tra poco.")
            return str(resp)

        if exact:
            s, e = exact
            sess["chosen"] = {"start": s.isoformat(), "end": e.isoformat()}
            sess["state"] = CONFIRM
            SESSIONS[from_number] = sess
            resp.message(
                f"Perfetto! Confermi:\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {fmt_slot(s)}\n\n"
                "Rispondi *OK* per confermare oppure *annulla*."
            )
            return str(resp)
        else:
            sess["preferred_date"] = d
            sess["state"] = ASK_ALTERNATIVE
            SESSIONS[from_number] = sess
            resp.message("A quell’ora non ho disponibilità. Vuoi che ti proponga i prossimi orari liberi?")
            return str(resp)

    # Caso D: booking generico ("vorrei prenotare")
    sess["state"] = ASK_DAY
    SESSIONS[from_number] = sess
    resp.message("Perfetto 💈 Che giorno preferisci? (es. domani, 16/12)")
    return str(resp)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
