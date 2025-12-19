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
# APP (Railway / Gunicorn)
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
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
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
    if not p:
        return ""
    p = p.replace("whatsapp:", "")
    return re.sub(r"\D+", "", p)


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
# Parsing IT
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
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)

    m = re.search(r"(\d{1,2})/(\d{1,2})", t)
    if m:
        d, mth = int(m.group(1)), int(m.group(2))
        try:
            return dt.date(today.year, mth, d)
        except Exception:
            return None

    for k, wd in WEEKDAYS_IT.items():
        if k in t:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None


def parse_time(text: str) -> Optional[dt.time]:
    t = text.lower()

    m = re.search(r"(\d{1,2})[:\.](\d{2})", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b(\d{1,2})\b", t)
    if m and any(x in t for x in ["alle", "verso", "ore"]):
        return dt.time(int(m.group(1)), 0)

    return None


def parse_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = text.lower()
    after, before = None, None

    if "mattina" in t:
        after, before = dt.time(9, 0), dt.time(12, 0)
    if "pomeriggio" in t:
        after, before = dt.time(14, 0), dt.time(19, 0)
    if "sera" in t:
        after, before = dt.time(17, 30), dt.time(22, 0)

    return after, before


# ============================================================
# Sheets helpers
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
# Load config
# ============================================================
def load_shop_by_phone(phone: str) -> Optional[Dict[str, str]]:
    for s in load_tab("shops"):
        if phone_matches(phone, s.get("whatsapp_number", "")):
            return s
    return None


def load_services(shop_id: str) -> List[Dict[str, str]]:
    out = []
    for r in load_tab("services"):
        if r.get("shop_id") == shop_id and r.get("active", "TRUE") != "FALSE":
            out.append(r)
    return out


def load_hours(shop_id: str):
    out = {i: [] for i in range(7)}
    for r in load_tab("hours"):
        if r.get("shop_id") != shop_id:
            continue
        out[int(r["weekday"])].append(
            (dt.time.fromisoformat(r["start"]), dt.time.fromisoformat(r["end"]))
        )
    return out


# ============================================================
# Sessions (robuste)
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
                json.dumps(data),
                dt.datetime.utcnow().isoformat()
            ]]
        }
    ).execute()


def clear_session(shop_id: str, phone: str):
    save_session(shop_id, phone, "", {})


# ============================================================
# Slot engine
# ============================================================
def service_duration_fallback(shop: Dict[str, str]) -> int:
    if shop.get("gender", "").lower() == "uomo":
        return 30
    return 45


def find_slots(
    shop: Dict[str, str],
    hours_map,
    duration_min: int,
    date: dt.date,
    after: Optional[dt.time],
    before: Optional[dt.time],
    limit=5
):
    tz = shop.get("timezone", "Europe/Rome")
    zi = tzinfo_for(tz)
    cal_id = shop["calendar_id"]

    slots = []
    for st, en in hours_map.get(date.weekday(), []):
        cur = dt.datetime.combine(date, st).replace(tzinfo=zi)
        end = dt.datetime.combine(date, en).replace(tzinfo=zi)

        if after:
            cur = max(cur, dt.datetime.combine(date, after).replace(tzinfo=zi))
        if before:
            end = min(end, dt.datetime.combine(date, before).replace(tzinfo=zi))

        while cur + dt.timedelta(minutes=duration_min) <= end:
            slots.append(cur)
            if len(slots) >= limit:
                return slots
            cur += dt.timedelta(minutes=30)

    return slots


def format_slot(tz: str, d: dt.datetime) -> str:
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[d.weekday()]} {d.strftime('%d/%m')} {d.strftime('%H:%M')}"


# ============================================================
# CORE LOGIC — DEFINITIVA
# ============================================================
def handle_message(shop: Dict[str, str], phone: str, text: str) -> str:
    tz = shop.get("timezone", "Europe/Rome")
    shop_id = shop["shop_id"]
    name = shop["name"]

    services = load_services(shop_id)
    hours = load_hours(shop_id)

    t = text.lower()
    date = parse_date(t, tz)
    time = parse_time(t)
    after, before = parse_window(t)

    sess = get_session(shop_id, phone)

    # GREETING
    if t in {"ciao", "salve", "buongiorno", "buonasera"}:
        clear_session(shop_id, phone)
        return f"Ciao! 👋 Sei in contatto con *{name}* 💈\nDimmi quando vuoi prenotare 😊"

    # TIME FIRST (anche senza servizio)
    if date:
        duration = service_duration_fallback(shop)
        slots = find_slots(shop, hours, duration, date, after, before)

        if not slots:
            return "In quella fascia non vedo posti liberi 😕 Vuoi provare un altro orario o un altro giorno?"

        save_session(shop_id, phone, "choose_slot", {
            "date": date.isoformat(),
            "slots": [s.isoformat() for s in slots]
        })

        lines = ["Perfetto 👍 Ho trovato queste disponibilità:"]
        for i, s in enumerate(slots, 1):
            lines.append(f"{i}) {format_slot(tz, s)}")
        lines.append("\nDimmi anche che servizio desideri così confermiamo 👌")
        return "\n".join(lines)

    return "Dimmi pure quando vuoi venire (es. *domani alle 18*, *venerdì pomeriggio*)."


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"


@app.route("/test")
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "")

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
    except HttpError as e:
        return jsonify({"error": "google_api_error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "server_error", "details": str(e)}), 500


@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
