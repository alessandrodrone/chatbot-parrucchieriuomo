from __future__ import annotations

import os, json, re, datetime as dt
from flask import Flask, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================
# APP
# ======================
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DEFAULT_TZ = "Europe/Rome"

# ======================
# DB
# ======================
def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# ======================
# GOOGLE CALENDAR
# ======================
def calendar_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None

    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# ======================
# UTILS
# ======================
def parse_time(text: str):
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]?([0-5]\d)?\b", text)
    if not m:
        return None
    return dt.time(int(m.group(1)), int(m.group(2) or 0))

# ======================
# LOADERS
# ======================
def load_shop(whatsapp_number: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM shops WHERE whatsapp_number=%s",
            (whatsapp_number,)
        )
        return cur.fetchone()

def load_services(shop_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM services WHERE shop_id=%s AND active=true",
            (shop_id,)
        )
        return cur.fetchall()

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
    text_low = text.lower()
    services = load_services(shop["id"])
    session = load_session(shop["id"], phone)

    # ---- GREETING
    if not session and text_low in {"ciao", "salve", "buongiorno", "buonasera"}:
        if len(services) > 1:
            lst = "\n".join(f"- {s['name']}" for s in services)
            return f"Ciao! 💈 Che servizio desideri?\n{lst}"
        return "Ciao! 💈 Quando vuoi venire?"

    # ---- SERVICE
    if not session and len(services) > 1:
        for s in services:
            if s["name"].lower() in text_low:
                save_session(shop["id"], phone, "need_date", {"service_id": s["id"]})
                return "Perfetto 👍 Per che giorno?"
        return "Dimmi che servizio desideri 😊"

    # ---- DATE
    if session and session["state"] == "need_date":
        save_session(shop["id"], phone, "need_time", session["data"])
        return "A che ora preferisci?"

    # ---- TIME
    if session and session["state"] == "need_time":
        t = parse_time(text)
        if not t:
            return "Dimmi un orario valido (es. 17:30)"

        # 👉 qui entrerà:
        # - capacity
        # - disponibilità
        # - Google Calendar

        reset_session(shop["id"], phone)
        return f"✅ Perfetto! Ti ho prenotato alle {t.strftime('%H:%M')}"

    return "Dimmi quando vuoi venire 😊"

# ======================
# TEST ENDPOINT (NO WHATSAPP)
# ======================
@app.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone", "test")
    msg = request.args.get("msg", "ciao")
    shop_number = request.args.get("to")

    if not shop_number:
        return jsonify({"error": "Missing ?to=whatsapp_number"}), 400

    shop = load_shop(shop_number)
    if not shop:
        return jsonify({"error": "Shop not found"}), 404

    reply = handle_message(shop, phone, msg)

    return jsonify({
        "phone": phone,
        "shop": shop["name"],
        "message_in": msg,
        "bot_reply": reply
    })

# ======================
# HOME
# ======================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

# ======================
# MAIN
# ======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
