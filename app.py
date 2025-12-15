from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import json
import sqlite3
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

TZ = ZoneInfo("Europe/Rome")

# =========================
# OPENAI
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Manca OPENAI_API_KEY nelle variabili Railway.")
client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# =========================
# GOOGLE CALENDAR
# =========================
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip()  # consigliato: primary

# Se vuoi evitare il file, puoi mettere tutto il JSON in una variabile:
# GOOGLE_CREDENTIALS_JSON = {...}
creds_path = "credentials.json"
creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
if creds_env:
    # scrive una copia runtime (utile se non vuoi caricare file su GitHub)
    with open(creds_path, "w", encoding="utf-8") as f:
        f.write(creds_env)

if not os.path.exists(creds_path):
    raise RuntimeError("Manca credentials.json (o GOOGLE_CREDENTIALS_JSON).")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
credentials = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
calendar = build("calendar", "v3", credentials=credentials)

# =========================
# ORARI PARRUCCHIERE (fissi)
# Python weekday: lun=0 ... dom=6
# CHIUSO: lun(0), dom(6)
# =========================
OPENING = {
    1: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mar
    2: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # mer
    3: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # gio
    4: [(time(8, 30), time(12, 0)), (time(15, 0), time(18, 0))],  # ven
    5: [(time(8, 30), time(13, 0)), (time(15, 0), time(18, 0))],  # sab
}

SLOT_MINUTES = 30
SERVICE_NAME = "Taglio uomo"

# =========================
# MEMORIA BREVE (RAM)
# =========================
SESSIONS = {}  # {from_number: {"state":..., "history":[...], "proposed":[...], ...}}

# =========================
# MEMORIA LUNGA (SQLite)
# =========================
DB_PATH = os.getenv("DB_PATH", "barber_bot.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS customer (
        phone TEXT PRIMARY KEY,
        created_at TEXT,
        last_seen_at TEXT,
        notes TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS booking (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        start_iso TEXT,
        end_iso TEXT,
        summary TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    return conn

def upsert_customer(phone: str):
    now = datetime.now(TZ).isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT phone FROM customer WHERE phone=?", (phone,))
    if cur.fetchone():
        cur.execute("UPDATE customer SET last_seen_at=? WHERE phone=?", (now, phone))
    else:
        cur.execute("INSERT INTO customer(phone, created_at, last_seen_at, notes) VALUES(?,?,?,?)",
                    (phone, now, now, ""))
    conn.commit()
    conn.close()

def add_booking(phone: str, start_dt: datetime, end_dt: datetime):
    conn = db()
    conn.execute(
        "INSERT INTO booking(phone, start_iso, end_iso, summary, created_at) VALUES(?,?,?,?,?)",
        (phone, start_dt.isoformat(), end_dt.isoformat(), SERVICE_NAME, datetime.now(TZ).isoformat())
    )
    conn.commit()
    conn.close()

def get_customer_notes(phone: str) -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT notes FROM customer WHERE phone=?", (phone,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else ""

def set_customer_notes(phone: str, notes: str):
    conn = db()
    conn.execute("UPDATE customer SET notes=? WHERE phone=?", (notes, phone))
    conn.commit()
    conn.close()

# =========================
# CALENDAR HELPERS
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
    # Freebusy è il modo più affidabile per controllare disponibilità
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

def find_next_free_slots(preferred_date: date | None, max_days=14, limit=5):
    slots = []
    start_day = preferred_date if preferred_date else datetime.now(TZ).date()
    # se è oggi e siamo oltre orario, si va avanti lo stesso, i controlli li fa freebusy
    for day_offset in range(0, max_days):
        d = start_day + timedelta(days=day_offset)
        # lun/dom chiuso: OPENING non li contiene
        for s, e in iter_slots_for_day(d):
            # non proporre slot nel passato
            if s <= datetime.now(TZ) + timedelta(minutes=2):
                continue
            if is_free(s, e):
                slots.append((s, e))
                if len(slots) >= limit:
                    return slots
    return slots

# =========================
# GPT PARSING (solo intenti)
# =========================
SYSTEM_GPT = """
Sei un assistente WhatsApp per PARRUCCHIERE UOMO.
Obiettivo: prenotare TAGLIO UOMO (slot 30 minuti) e rispondere solo su prenotazioni/orari.
Non parlare di altri servizi (colore, barba, estetica, giardinaggio, ecc.).
Se il cliente chiede altro, riporta al taglio uomo e alla prenotazione.
Devi estrarre l'intento e (se presente) una preferenza data/periodo/orario.
Rispondi SOLO con JSON valido.
"""

def gpt_intent(user_text: str):
    """
    Ritorna dict:
    {
      "intent": "book"|"change"|"cancel"|"info"|"other",
      "preferred_day": "today"|"tomorrow"|"date:YYYY-MM-DD"|null,
      "preferred_period": "morning"|"afternoon"|"evening"|null,
      "preferred_time": "HH:MM"|null
    }
    """
    prompt = f"""
Testo cliente: {user_text}

Regole:
- Se chiede appuntamento/prenotare/venire/slot/orari -> intent="book"
- Se chiede spostare -> "change"
- Se chiede cancellare -> "cancel"
- Se chiede solo info orari -> "info"
- Altro -> "other"

preferred_day:
- "today" se oggi
- "tomorrow" se domani
- "date:YYYY-MM-DD" se specifica una data (anche 'sabato' -> calcola la prossima occorrenza)
- null se non specifica

preferred_period:
- morning/afternoon/evening se indicato, altrimenti null

preferred_time:
- "HH:MM" se indicato, altrimenti null

Rispondi SOLO con JSON.
"""
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_GPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=200,
        )
        txt = r.choices[0].message.content.strip()
        data = json.loads(txt)
        return data
    except Exception:
        return {"intent": "other", "preferred_day": None, "preferred_period": None, "preferred_time": None}

# =========================
# UTILS
# =========================
CONFIRM_WORDS = {"ok", "va bene", "perfetto", "si", "sì", "confermo", "confermiamo", "bene"}
CANCEL_WORDS = {"annulla", "no", "non va bene", "cancella"}

def fmt_slot(s: datetime):
    # es: Mar 16/12 15:30
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[s.weekday()]} {s.strftime('%d/%m %H:%M')}"

# =========================
# MAIN WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_number = request.form.get("From", "")
    user_message = (request.form.get("Body", "") or "").strip()

    upsert_customer(from_number)

    # session init
    sess = SESSIONS.get(from_number, {"state": "idle", "history": []})
    sess["history"].append({"role": "user", "content": user_message})
    sess["history"] = sess["history"][-10:]  # memoria breve: ultimi 10 messaggi

    resp = MessagingResponse()

    # 1) Se stavamo aspettando conferma su uno slot
    if sess.get("state") == "await_confirm":
        low = user_message.lower().strip()
        if any(w in low for w in CANCEL_WORDS):
            sess["state"] = "idle"
            sess.pop("chosen", None)
            SESSIONS[from_number] = sess
            resp.message("Ok 👍 annullato. Vuoi un altro orario per il taglio uomo?")
            return str(resp)

        if any(w in low for w in CONFIRM_WORDS):
            chosen = sess.get("chosen")
            if chosen:
                start_dt = datetime.fromisoformat(chosen["start"])
                end_dt = datetime.fromisoformat(chosen["end"])
                # ricontrollo ultimo secondo
                if is_free(start_dt, end_dt):
                    create_calendar_event(from_number, start_dt, end_dt)
                    add_booking(from_number, start_dt, end_dt)

                    # aggiorna memoria lunga (nota sintetica)
                    prev = get_customer_notes(from_number)
                    note = f"Ultimo appuntamento: {start_dt.strftime('%d/%m/%Y %H:%M')} (taglio uomo)."
                    set_customer_notes(from_number, (prev + "\n" + note).strip())

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

        # se non è né conferma né annullo, lo interpreto come richiesta di cambio
        resp.message("Perfetto — vuoi confermare lo slot proposto? Rispondi *OK* oppure dimmi un altro giorno/orario.")
        SESSIONS[from_number] = sess
        return str(resp)

    # 2) intent parsing con GPT (solo per capire cosa vuole)
    intent = gpt_intent(user_message)
    it = (intent.get("intent") or "other").lower()

    # 3) Risposte fuori ambito
    if it == "other":
        resp.message(
            "Ciao! Io gestisco solo le *prenotazioni per taglio uomo* 💈\n"
            "Scrivimi ad esempio: *“Vorrei prenotare un taglio”* oppure *“Hai posto domani?”*."
        )
        SESSIONS[from_number] = sess
        return str(resp)

    if it == "info":
        resp.message(
            "Orari:\n"
            "- Mar–Ven: 08:30–12:00 e 15:00–18:00\n"
            "- Sab: 08:30–13:00 e 15:00–18:00\n"
            "- Lun/Dom: chiuso\n\n"
            "Vuoi prenotare un *taglio uomo*? Dimmi quando preferisci 😊"
        )
        SESSIONS[from_number] = sess
        return str(resp)

    # 4) booking flow
    if it in {"book", "change"}:
        # preferenza giorno
        pref_day = intent.get("preferred_day")
        preferred_date = None

        today = datetime.now(TZ).date()
        if pref_day == "today":
            preferred_date = today
        elif pref_day == "tomorrow":
            preferred_date = today + timedelta(days=1)
        elif isinstance(pref_day, str) and pref_day.startswith("date:"):
            try:
                preferred_date = datetime.strptime(pref_day.split("date:")[1], "%Y-%m-%d").date()
            except Exception:
                preferred_date = None

        # Cerca slot reali (Google Calendar) nei prossimi giorni
        slots = find_next_free_slots(preferred_date=preferred_date, max_days=14, limit=6)

        if not slots:
            resp.message("Al momento non trovo disponibilità nei prossimi giorni. Vuoi indicarmi un giorno specifico?")
            SESSIONS[from_number] = sess
            return str(resp)

        # Se cliente ha dato un orario preciso HH:MM, proviamo a proporre lo slot più vicino in quel giorno
        preferred_time = intent.get("preferred_time")
        if preferred_time and preferred_date:
            # filtra slot per quella data e orario uguale
            target = None
            try:
                hh, mm = preferred_time.split(":")
                target = time(int(hh), int(mm))
            except Exception:
                target = None

            if target:
                exact = []
                for s, e in slots:
                    if s.date() == preferred_date and s.time().hour == target.hour and s.time().minute == target.minute:
                        exact.append((s, e))
                if exact:
                    slots = exact + [x for x in slots if x not in exact]

        # Proponiamo i primi 3, e chiediamo scelta
        propose = slots[:3]
        lines = ["Perfetto 💈 Ecco i primi orari disponibili (slot 30 minuti):"]
        for i, (s, e) in enumerate(propose, start=1):
            lines.append(f"{i}) {fmt_slot(s)}")
        lines.append("\nRispondi con *1*, *2* o *3* per confermare.")
        resp.message("\n".join(lines))

        sess["state"] = "await_pick"
        sess["proposed"] = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in propose]
        SESSIONS[from_number] = sess
        return str(resp)

    if it == "cancel":
        resp.message("Ok 👍 Per cancellare/sistemare un appuntamento dimmi data e ora, oppure scrivi *prenota* per rifissarlo.")
        SESSIONS[from_number] = sess
        return str(resp)

    # 5) se siamo in attesa scelta 1/2/3 (gestione robusta)
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

    # fallback
    resp.message("Ciao! Vuoi prenotare un *taglio uomo*? Dimmi giorno/orario preferito 😊")
    SESSIONS[from_number] = sess
    return str(resp)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
