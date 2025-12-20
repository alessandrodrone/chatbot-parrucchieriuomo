from flask import Flask, request, jsonify
import datetime as dt
import re
import json
import difflib
import os

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SESSION_TTL_MIN = 30
SLOT_MINUTES = 30
MAX_LOOKAHEAD_DAYS = 14

# =========================
# SESSION STORAGE (in-memory semplice)
# =========================
SESSIONS = {}

def now():
    return dt.datetime.now()

def get_session(key):
    s = SESSIONS.get(key)
    if not s:
        return {}
    if (now() - s["updated"]).total_seconds() / 60 > SESSION_TTL_MIN:
        del SESSIONS[key]
        return {}
    return s["data"]

def save_session(key, data):
    SESSIONS[key] = {
        "data": data,
        "updated": now()
    }

def reset_session(key):
    if key in SESSIONS:
        del SESSIONS[key]

# =========================
# NLP UTILS
# =========================
def normalize(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower())

def fuzzy_match(word, choices):
    matches = difflib.get_close_matches(word, choices, n=1, cutoff=0.6)
    return matches[0] if matches else None

# =========================
# DATE / TIME PARSER
# =========================
def parse_date(text):
    t = normalize(text)
    today = dt.date.today()

    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    return None

def parse_time(text):
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]?([0-5]\d)?\b", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2)) if m.group(2) else 0
    return dt.time(h, mnt)

# =========================
# SERVICES
# =========================
SERVICES = [
    {"name": "taglio uomo", "duration": 30},
    {"name": "barba", "duration": 20},
    {"name": "taglio e barba", "duration": 50},
    {"name": "piega", "duration": 30},
    {"name": "colore", "duration": 90},
    {"name": "ceretta", "duration": 20},
]

SERVICE_NAMES = [s["name"] for s in SERVICES]

def detect_service(text):
    words = normalize(text).split()
    for w in words:
        m = fuzzy_match(w, SERVICE_NAMES)
        if m:
            return next(s for s in SERVICES if s["name"] == m)
    return None

# =========================
# AVAILABILITY MOCK
# =========================
def get_available_slots(date, duration):
    slots = []
    start = dt.datetime.combine(date, dt.time(9, 0))
    end = dt.datetime.combine(date, dt.time(19, 0))

    cur = start
    while cur + dt.timedelta(minutes=duration) <= end:
        slots.append(cur)
        cur += dt.timedelta(minutes=30)

    return slots[:5]

# =========================
# CORE BOT
# =========================
def handle_message(session_key, text):
    t = text.lower()
    s = get_session(session_key)

    # ---- cancel
    if any(x in t for x in ["annulla", "stop", "no"]):
        reset_session(session_key)
        return "Va bene 👍 Quando vuoi riprenotare?"

    # ---- detect service
    service = detect_service(text)
    if service:
        s["service"] = service

    # ---- detect date/time
    date = parse_date(text)
    time = parse_time(text)

    if date:
        s["date"] = date.isoformat()
    if time:
        s["time"] = time.strftime("%H:%M")

    save_session(session_key, s)

    # ---- if missing service
    if "service" not in s:
        return (
            "Perfetto 😊 Che servizio desideri?\n"
            "• Taglio uomo\n"
            "• Barba\n"
            "• Taglio e barba\n"
            "• Piega\n"
            "• Colore"
        )

    # ---- if missing date or time
    if "date" not in s or "time" not in s:
        return "Quando preferisci venire? (es. domani alle 18)"

    # ---- check availability
    date_obj = dt.date.fromisoformat(s["date"])
    time_obj = dt.time.fromisoformat(s["time"])
    service = s["service"]

    slots = get_available_slots(date_obj, service["duration"])

    exact = dt.datetime.combine(date_obj, time_obj)

    if exact in slots:
        s["confirm"] = exact.isoformat()
        save_session(session_key, s)
        return (
            f"Perfetto 👍 Confermi?\n"
            f"💈 {service['name']}\n"
            f"🕒 {exact.strftime('%d/%m %H:%M')}\n\n"
            f"Rispondi OK per confermare"
        )

    # ---- alternatives (smart)
    alt = slots[:2]
    msg = (
        f"Domani alle {time_obj.strftime('%H:%M')} è già occupato 😕\n"
        f"Posso però offrirti:\n"
    )
    for a in alt:
        msg += f"• {a.strftime('%H:%M')}\n"
    msg += "\nDimmi cosa preferisci 😊"

    s["options"] = [a.isoformat() for a in alt]
    save_session(session_key, s)
    return msg

# =========================
# ROUTES
# =========================
@app.route("/test")
def test():
    shop = request.args.get("phone")
    customer = request.args.get("customer")
    msg = request.args.get("msg", "")

    key = f"{shop}:{customer}"
    reply = handle_message(key, msg)

    return jsonify({
        "shop_number": shop,
        "customer": customer,
        "message_in": msg,
        "bot_reply": reply
    })

@app.route("/")
def home():
    return "RispondiTu v2 attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
