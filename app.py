from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, json, re
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build

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

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip()  # consigliato: primary

# Se non vuoi caricare file, puoi mettere tutta la JSON nella variabile:
# GOOGLE_CREDENTIALS_JSON
creds_path = "credentials.json"
creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
if creds_env:
    with open(creds_path, "w", encoding="utf-8") as f:
        f.write(creds_env)

if not os.path.exists(creds_path):
    raise RuntimeError("Manca credentials.json (o GOOGLE_CREDENTIALS_JSON).")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
credentials = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
calendar = build("calendar", "v3", credentials=credentials)

SLOT_MINUTES = 30
SERVICE_NAME = "Taglio uomo"

# =========================
# ORARI PARRUCCHIERE (fissi)
# lun=0 ... dom=6
# =========================
OPENING = {
    1: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mar
    2: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mer
    3: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # gio
    4: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # ven
    5: [(time(8, 30), time(13, 0)), (time(15, 0), time(18, 0))],  # sab
}
# lun e dom chiuso (0 e 6 non presenti)

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}  # {phone: {"state":..., "proposed":[...], "chosen":..., "history":[...] }}

CONFIRM_WORDS = {"ok", "va bene", "perfetto", "si", "sì", "confermo", "confermiamo", "bene"}
CANCEL_WORDS = {"annulla", "no", "non va bene", "cancella"}

# =========================
# HELPERS ORARI / SLOTS
# =========================
def combine(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=TZ)

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

def create_calendar_event(phone: str, start_dt: datetime, end_dt: datetime):
    event = {
        "summary": SERVICE_NAME,
        "description": f"Cliente WhatsApp: {phone}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Rome"},
    }
    calendar.events().insert(calendarId=CALENDAR_ID, body=event).execute()

def find_next_free_slots(preferred_date: date | None, max_days=14, limit=6):
    slots = []
    start_day = preferred_date if preferred_date else datetime.now(TZ).date()
    for day_offset in range(0, max_days):
        d = start_day + timedelta(days=day_offset)
        for s, e in iter_slots_for_day(d):
            if s <= datetime.now(TZ) + timedelta(minutes=2):
                continue
            if is_free(s, e):
                slots.append((s, e))
                if len(slots) >= limit:
                    return slots
    return slots

def fmt_slot(s: datetime):
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[s.weekday()]} {s.strftime('%d/%m %H:%M')}"

# =========================
# GPT: SOLO ESTRAZIONE DATI
# =========================
SYSTEM_GPT = """
Sei un assistente WhatsApp per PARRUCCHIERE UOMO.
Obiettivo: prenotare TAGLIO UOMO (slot 30 minuti) e rispondere su disponibilità/orari.
Non parlare di altri servizi.
Devi estrarre SOLO:
- intent (book/info/other)
- preferred_day (today/tomorrow/date:YYYY-MM-DD/null)
- preferred_time (HH:MM/null)
Rispondi SOLO con JSON valido.
"""

def gpt_parse(user_text: str):
    prompt = f"""
Testo cliente: {user_text}

Regole:
- Se chiede appuntamento/prenotare/posto/orario -> intent="book"
- Se chiede solo orari -> intent="info"
- Altrimenti -> intent="other"

preferred_day:
- "today" se oggi
- "tomorrow" se domani
- "date:YYYY-MM-DD" se specifica data
- null se non specifica

preferred_time:
- "HH:MM" se specifica un orario (es. 18, 18:00, alle 15 e 30)
- null se non specifica

Rispondi SOLO JSON.
"""
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_GPT},
                      {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=120,
        )
        return json.loads(r.choices[0].message.content.strip())
    except Exception:
        return {"intent": "other", "preferred_day": None, "preferred_time": None}

def normalize_time(text: str) -> str | None:
    """
    Accetta: '18', '18:00', 'alle 18', 'ore 18', '18.30', '18 30'
    Ritorna 'HH:MM' o None
    """
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

# =========================
# WEBHOOK WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From", "")
    user_message = (request.form.get("Body", "") or "").strip()

    sess = SESSIONS.get(from_number, {"state": "idle", "history": []})
    sess["history"].append(user_message)
    sess["history"] = sess["history"][-12:]

    resp = MessagingResponse()

    # --- Stato: in attesa di scelta 1/2/3 ---
    if sess.get("state") == "await_pick":
        low = user_message.strip().lower()
        if low in {"1", "2", "3"} and sess.get("proposed"):
            idx = int(low) - 1
            if 0 <= idx < len(sess["proposed"]):
                chosen = sess["proposed"][idx]
                start_dt = datetime.fromisoformat(chosen["start"])
                resp.message(
                    f"Confermi questo appuntamento?\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {fmt_slot(start_dt)}\n\n"
                    "Rispondi *OK* per confermare oppure *annulla*."
                )
                sess["state"] = "await_confirm"
                sess["chosen"] = chosen
                SESSIONS[from_number] = sess
                return str(resp)
        resp.message("Per favore rispondi con *1*, *2* o *3* 🙂")
        SESSIONS[from_number] = sess
        return str(resp)

    # --- Stato: in attesa conferma ---
    if sess.get("state") == "await_confirm":
        low = user_message.lower().strip()
        if any(w in low for w in CANCEL_WORDS):
            sess["state"] = "idle"
            sess.pop("chosen", None)
            SESSIONS[from_number] = sess
            resp.message("Ok 👍 annullato. Vuoi che ti proponga altri orari?")
            return str(resp)

        if any(w in low for w in CONFIRM_WORDS):
            chosen = sess.get("chosen")
            if chosen:
                start_dt = datetime.fromisoformat(chosen["start"])
                end_dt = datetime.fromisoformat(chosen["end"])
                # ultimo controllo
                if is_free(start_dt, end_dt):
                    create_calendar_event(from_number, start_dt, end_dt)
                    sess["state"] = "idle"
                    sess.pop("chosen", None)
                    SESSIONS[from_number] = sess
                    resp.message(
                        f"✅ Appuntamento confermato!\n"
                        f"💈 {SERVICE_NAME}\n"
                        f"🕒 {fmt_slot(start_dt)}\n\n"
                        "A presto 👋"
                    )
                    return str(resp)
                else:
                    sess["state"] = "idle"
                    sess.pop("chosen", None)
                    SESSIONS[from_number] = sess
                    resp.message("Ops, quello slot è appena stato occupato. Vuoi che ti proponga i prossimi disponibili?")
                    return str(resp)

        resp.message("Vuoi confermare lo slot? Rispondi *OK* oppure *annulla*.")
        SESSIONS[from_number] = sess
        return str(resp)

    # --- Se l'utente manda solo un orario tipo "alle 18" ---
    # e prima aveva chiesto domani/posto -> usiamo il contesto salvato
    maybe_time = normalize_time(user_message)
    if maybe_time and sess.get("pending_day"):
        # prova a cercare esattamente quell'orario nel giorno pending_day
        preferred_date = sess["pending_day"]
        slots = find_next_free_slots(preferred_date=preferred_date, max_days=1, limit=30)
        exact = []
        for s, e in slots:
            if s.date() == preferred_date and s.strftime("%H:%M") == maybe_time:
                exact.append((s, e))
                break
        if exact:
            s, e = exact[0]
            sess["state"] = "await_confirm"
            sess["chosen"] = {"start": s.isoformat(), "end": e.isoformat()}
            sess.pop("pending_day", None)
            SESSIONS[from_number] = sess
            resp.message(
                f"Perfetto! Confermi:\n"
                f"💈 {SERVICE_NAME}\n"
                f"🕒 {fmt_slot(s)}\n\n"
                "Rispondi *OK* per confermare oppure *annulla*."
            )
            return str(resp)
        else:
            # non esiste quello slot libero
            sess.pop("pending_day", None)
            SESSIONS[from_number] = sess
            resp.message("A quell’ora non ho disponibilità. Vuoi che ti proponga i prossimi orari liberi?")
            return str(resp)

    # --- Parsing intent con GPT (solo per capire cosa vuole) ---
    parsed = gpt_parse(user_message)
    intent = (parsed.get("intent") or "other").lower()

    if intent == "info":
        resp.message(
            "Orari:\n"
            "- Mar–Ven: 08:30–12:00 e 15:00–18:00\n"
            "- Sab: 08:30–13:00 e 15:00–18:00\n"
            "- Lun/Dom: chiuso\n\n"
            "Vuoi prenotare un *taglio uomo*? Dimmi quando preferisci 😊"
        )
        SESSIONS[from_number] = sess
        return str(resp)

    if intent != "book":
        resp.message(
            "Ciao! Io gestisco solo le *prenotazioni per taglio uomo* 💈\n"
            "Scrivimi: *“Vorrei prenotare un taglio”* oppure *“Hai posto domani?”*."
        )
        SESSIONS[from_number] = sess
        return str(resp)

    # booking: calcolo preferred_date
    pref_day = parsed.get("preferred_day")
    today = datetime.now(TZ).date()
    preferred_date = None
    if pref_day == "today":
        preferred_date = today
    elif pref_day == "tomorrow":
        preferred_date = today + timedelta(days=1)
    elif isinstance(pref_day, str) and pref_day.startswith("date:"):
        try:
            preferred_date = datetime.strptime(pref_day.split("date:")[1], "%Y-%m-%d").date()
        except Exception:
            preferred_date = None

    # Se chiede "domani" ma non dà orario, chiediamo l'ora (e memorizziamo il giorno)
    if preferred_date and not parsed.get("preferred_time"):
        sess["pending_day"] = preferred_date
        SESSIONS[from_number] = sess
        resp.message("Certo 👍 A che ora preferisci? (es. 15:00, 17:30, 18:00)")
        return str(resp)

    # Se ha dato anche un orario, proviamo a proporre slot coerenti
    preferred_time = parsed.get("preferred_time")
    if preferred_time and preferred_date:
        # Proviamo a vedere se quello slot esiste ed è libero
        slots = find_next_free_slots(preferred_date=preferred_date, max_days=1, limit=30)
        for s, e in slots:
            if s.date() == preferred_date and s.strftime("%H:%M") == preferred_time:
                sess["state"] = "await_confirm"
                sess["chosen"] = {"start": s.isoformat(), "end": e.isoformat()}
                SESSIONS[from_number] = sess
                resp.message(
                    f"Perfetto! Confermi:\n"
                    f"💈 {SERVICE_NAME}\n"
                    f"🕒 {fmt_slot(s)}\n\n"
                    "Rispondi *OK* per confermare oppure *annulla*."
                )
                return str(resp)

        resp.message("A quell’ora non ho disponibilità. Vuoi che ti proponga i prossimi orari liberi?")
        SESSIONS[from_number] = sess
        return str(resp)

    # Altrimenti proponi i prossimi 3 slot disponibili
    slots = find_next_free_slots(preferred_date=preferred_date, max_days=14, limit=6)
    if not slots:
        resp.message("Non trovo disponibilità nei prossimi giorni. Vuoi indicarmi un giorno specifico?")
        SESSIONS[from_number] = sess
        return str(resp)

    propose = slots[:3]
    lines = ["Perfetto 💈 Ecco i primi orari disponibili (slot 30 minuti):"]
    for i, (s, e) in enumerate(propose, start=1):
        lines.append(f"{i}) {fmt_slot(s)}")
    lines.append("\nRispondi con *1*, *2* o *3* per scegliere.")
    resp.message("\n".join(lines))

    sess["state"] = "await_pick"
    sess["proposed"] = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in propose]
    SESSIONS[from_number] = sess
    return str(resp)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
