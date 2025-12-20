from __future__ import annotations
import os, re, json, datetime as dt
from typing import Optional, Dict, Any, List
from flask import Flask, request, jsonify

# ============================================================
# APP
# ============================================================
app = Flask(__name__)

SESSION_TTL_MIN = 30

# ============================================================
# MOCK / STORAGE (usa Sheets/DB nella tua versione prod)
# ============================================================
SESSIONS: Dict[str, Dict[str, Any]] = {}

# ============================================================
# HELPERS
# ============================================================
def now():
    return dt.datetime.now()

def normalize_phone(p: str) -> str:
    return re.sub(r"\D+", "", p or "")

def session_key(shop: str, customer: str) -> str:
    return f"{shop}:{customer}"

def get_session(shop: str, customer: str) -> Dict[str, Any]:
    key = session_key(shop, customer)
    s = SESSIONS.get(key)
    if not s:
        return {}
    if (now() - s["updated"]).total_seconds() / 60 > SESSION_TTL_MIN:
        del SESSIONS[key]
        return {}
    return s["data"]

def save_session(shop: str, customer: str, data: Dict[str, Any]):
    SESSIONS[session_key(shop, customer)] = {
        "data": data,
        "updated": now()
    }

def reset_session(shop: str, customer: str):
    SESSIONS.pop(session_key(shop, customer), None)

# ============================================================
# NLP SEMPLICE (robusto)
# ============================================================
def fuzzy_match(word: str, target: str) -> bool:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, word, target).ratio() > 0.7

def detect_service(text: str, services: List[str]) -> Optional[str]:
    words = re.findall(r"[a-zàèéìòù]+", text.lower())
    for w in words:
        for s in services:
            if fuzzy_match(w, s.lower()):
                return s
    return None

def detect_date_time(text: str):
    text = text.lower()
    today = now().date()

    date = None
    if "domani" in text:
        date = today + dt.timedelta(days=1)
    elif "oggi" in text:
        date = today

    time = None
    m = re.search(r"(\d{1,2})(?:[:\.](\d{2}))?", text)
    if m:
        h = int(m.group(1))
        mnt = int(m.group(2) or 0)
        if 0 <= h <= 23:
            time = dt.time(h, mnt)

    fascia = None
    if "sera" in text or "pomeriggio" in text:
        fascia = "sera"

    return date, time, fascia

# ============================================================
# AGENDA FURBA (PRUDENTE)
# ============================================================
def find_best_slot(requested_dt: Optional[dt.datetime]) -> List[dt.datetime]:
    base = requested_dt or now() + dt.timedelta(hours=2)
    slots = []

    # stesso giorno ±30 min
    for delta in [0, 30, -30]:
        s = base + dt.timedelta(minutes=delta)
        if s > now():
            slots.append(s)

    # stesso orario giorno dopo
    slots.append(base + dt.timedelta(days=1))

    # fallback: primo slot disponibile
    slots.append(now() + dt.timedelta(hours=3))

    # dedup
    seen = set()
    out = []
    for s in slots:
        k = s.strftime("%Y-%m-%d %H:%M")
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out[:3]

def fmt(dtobj: dt.datetime) -> str:
    giorni = ["Lun","Mar","Mer","Gio","Ven","Sab","Dom"]
    return f"{giorni[dtobj.weekday()]} {dtobj.strftime('%d/%m %H:%M')}"

# ============================================================
# CORE LOGIC
# ============================================================
@app.route("/test")
def test():
    shop = normalize_phone(request.args.get("phone"))
    customer = normalize_phone(request.args.get("customer"))
    msg = (request.args.get("msg") or "").strip()

    services = ["taglio", "barba", "taglio + barba", "piega", "colore", "ceretta"]

    sess = get_session(shop, customer)

    # CANCEL
    if msg.lower() in {"annulla", "cancella", "no"}:
        reset_session(shop, customer)
        return jsonify(reply(
            shop, customer,
            "Va bene 👍 se vuoi riprenotare sono qui."
        ))

    # GREETING
    if not sess and msg.lower() in {"ciao", "salve", "buongiorno"}:
        return jsonify(reply(
            shop, customer,
            "Ciao! 👋 Dimmi quando vorresti venire e per che servizio 😊"
        ))

    # PARSE
    service = sess.get("service") or detect_service(msg, services)
    date, time, fascia = detect_date_time(msg)

    if service:
        sess["service"] = service

    if date or time or fascia:
        sess["date"] = date.isoformat() if date else sess.get("date")
        sess["time"] = time.isoformat() if time else sess.get("time")

    # CHIEDI SERVIZIO SE MANCA
    if not sess.get("service"):
        save_session(shop, customer, sess)
        return jsonify(reply(
            shop, customer,
            "Perfetto 😊 che servizio desideri? (es. *taglio*, *taglio + barba*, *piega*)"
        ))

    # COSTRUISCI RICHIESTA
    req_dt = None
    if sess.get("date") and sess.get("time"):
        d = dt.date.fromisoformat(sess["date"])
        t = dt.time.fromisoformat(sess["time"])
        req_dt = dt.datetime.combine(d, t)

    # TROVA SLOT
    slots = find_best_slot(req_dt)

    # SE CLIENTE VAGO O RIFIUTA TUTTO
    if msg.lower() in {"non posso", "non va bene", "nessuno"}:
        best = slots[0]
        reset_session(shop, customer)
        return jsonify(reply(
            shop, customer,
            f"Va bene 👍 allora ti propongo la prima disponibilità utile:\n🕒 *{fmt(best)}*\nVa bene per te?"
        ))

    # CONFERMA
    if msg.lower() in {"ok", "va bene", "sì", "si"} and sess.get("pending"):
        reset_session(shop, customer)
        return jsonify(reply(
            shop, customer,
            f"✅ Appuntamento confermato!\n💈 *{sess['service']}*\n🕒 {sess['pending']}\nA presto 👋"
        ))

    # PROPOSTA
    best = slots[0]
    sess["pending"] = fmt(best)
    save_session(shop, customer, sess)

    return jsonify(reply(
        shop, customer,
        f"Purtroppo l’orario richiesto non è disponibile 😕\n"
        f"Posso però offrirti:\n"
        f"🕒 *{fmt(best)}*\n"
        f"Va bene per te?"
    ))

def reply(shop, customer, text):
    return {
        "shop_number": shop,
        "customer": customer,
        "bot_reply": text
    }

# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
