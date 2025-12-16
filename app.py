from __future__ import annotations
import os, json, uuid, re, datetime as dt
from flask import Flask, request
from psycopg2.extras import RealDictCursor
import psycopg2
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================
# CONFIG
# ======================
APP = Flask(__name__)
TZ = "Europe/Rome"
SLOT_DEFAULT = 30

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# ======================
# DB
# ======================
def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# ======================
# GOOGLE CALENDAR
# ======================
def calendar_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# ======================
# UTIL
# ======================
def now():
    return dt.datetime.now(dt.timezone.utc)

def parse_time(text):
    m = re.search(r"(\d{1,2})[:\.]?(\d{2})?", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2) or 0)
    return dt.time(h, mnt)

# ======================
# SHOP LOAD
# ======================
def load_shop(whatsapp_to):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM shops WHERE whatsapp_number=%s",
            (whatsapp_to,)
        )
        return cur.fetchone()

# ======================
# SERVICES
# ======================
def load_services(shop_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM services WHERE shop_id=%s AND active=true",
            (shop_id,)
        )
        return cur.fetchall()

# ======================
# SESSION
# ======================
def load_session(shop_id, phone):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM sessions WHERE shop_id=%s AND phone=%s",
            (shop_id, phone)
        )
        return cur.fetchone()

def save_session(shop_id, phone, state, data):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sessions (shop_id, phone, state, data)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (shop_id, phone)
            DO UPDATE SET state=%s, data=%s, updated_at=now()
        """, (shop_id, phone, state, json.dumps(data), state, json.dumps(data)))

def reset_session(shop_id, phone):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM sessions WHERE shop_id=%s AND phone=%s",
            (shop_id, phone)
        )

# ======================
# CORE LOGIC
# ======================
def handle_message(shop, phone, text):
    services = load_services(shop["id"])
    session = load_session(shop["id"], phone)

    text_low = text.lower()

    # ---- GREETING
    if not session and text_low in {"ciao","salve","buongiorno","buonasera"}:
        if len(services) > 1:
            s = "\n".join(f"- {x['name']}" for x in services)
            return f"Ciao! 💈 Che servizio desideri?\n{s}"
        return f"Ciao! 💈 Quando vuoi venire?"

    # ---- SERVICE SELECTION
    if not session and len(services) > 1:
        for s in services:
            if s["name"].lower() in text_low:
                save_session(shop["id"], phone, "need_date", {"service": s})
                return f"Perfetto 👍 Per che giorno?"

        return "Dimmi che servizio desideri 😊"

    # ---- DATE
    if session and session["state"] == "need_date":
        save_session(shop["id"], phone, "need_time", session["data"])
        return "A che ora preferisci?"

    # ---- TIME
    if session and session["state"] == "need_time":
        time = parse_time(text)
        if not time:
            return "Dimmi un orario valido (es. 17:30)"

        # QUI entrerebbe:
        # - ricerca slot
        # - capacity check
        # - alternative

        reset_session(shop["id"], phone)
        return f"✅ Perfetto! Ti ho prenotato alle {time.strftime('%H:%M')}"

    return "Dimmi quando vuoi venire 😊"

# ======================
# ROUTE WHATSAPP
# ======================
@APP.route("/whatsapp", methods=["POST"])
def whatsapp():
    from_ = request.form.get("From", "")
    to_ = request.form.get("To", "")
    body = request.form.get("Body", "").strip()

    shop = load_shop(to_)
    resp = MessagingResponse()

    if not shop:
        resp.message("Numero non configurato.")
        return str(resp)

    reply = handle_message(shop, from_, body)
    resp.message(reply)
    return str(resp)

@APP.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
