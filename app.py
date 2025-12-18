from __future__ import annotations

import os
import re
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# ============================================================
# APP (Railway / gunicorn)
# ============================================================
app = Flask(__name__)


# ============================================================
# ENV
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))
MAX_LOOKAHEAD_DAYS = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))


# ============================================================
# Google clients (lazy)
# ============================================================
_sheets = None
_calendar = None


def _creds():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar",
    ]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def sheets():
    global _sheets
    if _sheets is None:
        _sheets = build("sheets", "v4", credentials=_creds(), cache_discovery=False)
    return _sheets


def calendar():
    global _calendar
    if _calendar is None:
        _calendar = build("calendar", "v3", credentials=_creds(), cache_discovery=False)
    return _calendar


# ============================================================
# Utils
# ============================================================
def norm_phone(p: str) -> str:
    return re.sub(r"\D+", "", (p or "").replace("whatsapp:", ""))


def phone_matches(a: str, b: str) -> bool:
    da, db = norm_phone(a), norm_phone(b)
    if da == db:
        return True
    if da.startswith("39") and da[2:] == db:
        return True
    if db.startswith("39") and db[2:] == da:
        return True
    return False


def tzinfo_for(tz: str):
    return ZoneInfo(tz) if ZoneInfo else None


def now_local(tz: str):
    zi = tzinfo_for(tz)
    return dt.datetime.now(zi) if zi else dt.datetime.now()


# ============================================================
# Date / Time parsing (IT)
# ============================================================
WEEKDAYS_IT = {
    "lunedi": 0, "lunedì": 0,
    "martedi": 1, "martedì": 1,
    "mercoledi": 2, "mercoledì": 2,
    "giovedi": 3, "giovedì": 3,
    "venerdi": 4, "venerdì": 4,
    "sabato": 5,
    "domenica": 6,
}


def parse_date(text: str, tz: str) -> Optional[dt.date]:
    t = text.lower()
    today = now_local(tz).date()

    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)

    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
    if m:
        return dt.date(today.year, int(m.group(2)), int(m.group(1)))

    for k, wd in WEEKDAYS_IT.items():
        if k in t:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None


def parse_time(text: str) -> Optional[dt.time]:
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]?([0-5]\d)?\b", text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        return dt.time(h, mi)
    return None


# ============================================================
# Google Sheets helpers
# ============================================================
def load_tab(tab: str) -> List[Dict[str, str]]:
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()

    values = res.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = []
    for r in values[1:]:
        obj = {}
        for i, h in enumerate(headers):
            obj[h] = r[i] if i < len(r) else ""
        rows.append(obj)
    return rows


# ============================================================
# Load shop / hours / services
# ============================================================
def load_shop_by_phone(phone: str) -> Optional[Dict[str, str]]:
    for s in load_tab("shops"):
        if phone_matches(phone, s.get("whatsapp_number", "")):
            return s
    return None


def load_hours(shop_id: str):
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") != shop_id:
            continue
        out[int(r["weekday"])].append(
            (dt.time.fromisoformat(r["start"]), dt.time.fromisoformat(r["end"]))
        )
    return out


def load_services(shop_id: str):
    out = []
    for r in load_tab("services"):
        if r.get("shop_id") == shop_id and r.get("active", "TRUE") != "FALSE":
            out.append(r)
    return out


# ============================================================
# Sessions (memoria breve)
# ============================================================
def get_session(shop_id: str, phone: str):
    for r in load_tab("sessions"):
        if r.get("shop_id") == shop_id and phone_matches(phone, r.get("phone", "")):
            try:
                data = json.loads(r.get("data", "{}"))
            except Exception:
                data = {}
            return {
                "state": r.get("state", ""),
                "data": data,
                "updated_at": r.get("updated_at", "")
            }
    return None


def save_session(shop_id: str, phone: str, state: str, data: dict):
    sheets().spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="sessions!A:E",
        valueInputOption="RAW",
        body={
            "values": [[
                shop_id,
                norm_phone(phone),
                state,
                json.dumps(data, ensure_ascii=False),
                dt.datetime.utcnow().isoformat()
            ]]
        }
    ).execute()


def reset_session(shop_id: str, phone: str):
    save_session(shop_id, phone, "", {})


# ============================================================
# CORE BOT LOGIC (FIX DEFINITIVO)
# ============================================================
def handle_message(shop: Dict[str, str], phone: str, text: str) -> str:
    shop_id = shop["shop_id"]
    shop_name = shop["name"]
    tz = shop.get("timezone", "Europe/Rome")

    services = load_services(shop_id)
    sess = get_session(shop_id, phone)

    t = text.lower().strip()

    # ===== GREETING =====
    if t in {"ciao", "salve", "buongiorno", "buonasera"}:
        reset_session(shop_id, phone)
        return (
            f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    # ===== PARSE =====
    date_ = parse_date(t, tz)
    time_ = parse_time(t)

    # ===== SERVICE DETECTION =====
    chosen_service = None
    for s in services:
        if s["name"].lower() in t:
            chosen_service = s
            break

    # =====================================================
    # 🔒 FIX CHIAVE: se NON c'è servizio → fermati qui
    # =====================================================
    if not chosen_service:
        save_session(shop_id, phone, "need_service", {})
        s_list = "\n".join([f"• {s['name']}" for s in services])
        return (
            f"Perfetto 😊 Per che servizio vuoi prenotare da *{shop_name}*?\n"
            f"{s_list}\n\n"
            f"Poi dimmi anche giorno e orario."
        )

    # ===== SE C'È SERVIZIO MA NON DATA =====
    if not date_:
        save_session(shop_id, phone, "need_date", {"service": chosen_service})
        return "Quando preferisci venire? (es. *domani alle 18*, *venerdì pomeriggio*)."

    # ===== SE C'È DATA MA NON ORA =====
    if not time_:
        save_session(shop_id, phone, "need_time", {"service": chosen_service, "date": date_.isoformat()})
        return "A che ora preferisci? (es. *18:00*)"

    # ===== TUTTO OK → SIMULAZIONE CHECK =====
    return (
        f"Perfetto 👍 Sto controllando la disponibilità per "
        f"{date_.strftime('%d/%m')} alle {time_.strftime('%H:%M')}.\n"
        f"Un attimo ⏳"
    )


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"


@app.route("/test")
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "ciao")

    try:
        shop = load_shop_by_phone(phone)
        if not shop:
            return jsonify({"error": "shop non trovato"}), 404

        reply = handle_message(shop, phone, msg)
        return jsonify({
            "shop": shop["name"],
            "phone": phone,
            "message_in": msg,
            "bot_reply": reply
        })
    except Exception as e:
        return jsonify({"error": "server_error", "details": str(e)}), 500


# ============================================================
# WhatsApp placeholder
# ============================================================
@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
