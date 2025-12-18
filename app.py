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
# ✅ IMPORTANTISSIMO per Railway / gunicorn
# gunicorn app:app -> deve esistere variabile "app"
# ============================================================
app = Flask(__name__)


# ============================================================
# ENV
# ============================================================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "30"))

MAX_LOOKAHEAD_DAYS = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "20"))


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
        _sheets = build(
            "sheets",
            "v4",
            credentials=_creds(),
            cache_discovery=False,
        )
    return _sheets


def calendar():
    global _calendar
    if _calendar is None:
        _calendar = build(
            "calendar",
            "v3",
            credentials=_creds(),
            cache_discovery=False,
        )
    return _calendar


# ============================================================
# Cache semplice (riduce chiamate API)
# ============================================================
_CACHE: Dict[str, Dict[str, Any]] = {}


def cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    if (dt.datetime.utcnow() - item["ts"]).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return item["data"]


def cache_set(key: str, data: Any):
    _CACHE[key] = {
        "ts": dt.datetime.utcnow(),
        "data": data,
    }


def cache_del(key: str):
    if key in _CACHE:
        del _CACHE[key]


# ============================================================
# Helpers: phone normalization (robusto)
# ============================================================
def norm_phone(p: str) -> str:
    """
    Normalizza:
    - whatsapp:+39348... -> 39348...
    - +39 348...         -> 39348...
    - 0039...            -> 39...
    - 348...             -> 348...
    """
    if not p:
        return ""
    p = p.lower().strip()
    p = p.replace("whatsapp:", "")
    digits = re.sub(r"\D+", "", p)
    digits = digits.lstrip("0") or digits
    return digits


def phone_matches(a: str, b: str) -> bool:
    da = norm_phone(a)
    db = norm_phone(b)
    if not da or not db:
        return False
    if da == db:
        return True
    if da.startswith("39") and da[2:] == db:
        return True
    if db.startswith("39") and db[2:] == da:
        return True
    return False


# ============================================================
# Time / Date parsing (IT)
# ============================================================
WEEKDAYS_IT = {
    "lunedì": 0, "lunedi": 0, "lun": 0,
    "martedì": 1, "martedi": 1, "mar": 1,
    "mercoledì": 2, "mercoledi": 2, "mer": 2,
    "giovedì": 3, "giovedi": 3, "gio": 3,
    "venerdì": 4, "venerdi": 4, "ven": 4,
    "sabato": 5, "sab": 5,
    "domenica": 6, "dom": 6,
}


def tzinfo_for(shop_tz: str):
    if ZoneInfo:
        return ZoneInfo(shop_tz)
    return None


def now_local(shop_tz: str) -> dt.datetime:
    tz = tzinfo_for(shop_tz)
    return dt.datetime.now(tz) if tz else dt.datetime.now()


def parse_date(text: str, shop_tz: str) -> Optional[dt.date]:
    t = (text or "").lower()
    today = now_local(shop_tz).date()

    if "oggi" in t:
        return today
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    if "stasera" in t:
        return today

    m = re.search(r"\b(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        yraw = m.group(3)
        y = today.year if not yraw else (2000 + int(yraw) if int(yraw) < 100 else int(yraw))
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None

    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None


def parse_time(text: str) -> Optional[dt.time]:
    t = (text or "").lower().strip()

    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    m = re.search(r"\b(?:alle|ore)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return dt.time(int(m.group(1)), 0)

    return None


def parse_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = (text or "").lower()
    after = None
    before = None

    m = re.search(r"\bdopo\s+le?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        after = parse_time(m.group(1))

    m = re.search(r"\bprima\s+delle?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        before = parse_time(m.group(1))

    if "mattina" in t:
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)
    if "pomeriggio" in t:
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)
    if "sera" in t or "stasera" in t:
        after = after or dt.time(17, 30)
        before = before or dt.time(22, 0)

    return after, before
    # ============================================================
# Google Sheets helpers
# ============================================================
def _require_sheet_id():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")


def load_tab(tab: str) -> List[Dict[str, str]]:
    """
    Legge un foglio (A:Z) e lo converte in list[dict] usando la riga header.
    Usa cache breve.
    """
    _require_sheet_id()

    cache_key = f"tab:{tab}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    rng = f"{tab}!A:Z"
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=rng
    ).execute()

    values = res.get("values", [])
    if not values:
        cache_set(cache_key, [])
        return []

    headers = [h.strip() for h in values[0]]
    rows: List[Dict[str, str]] = []

    for row in values[1:]:
        obj: Dict[str, str] = {}
        for i, h in enumerate(headers):
            v = row[i] if i < len(row) else ""
            if isinstance(v, str):
                obj[h] = v.strip()
            else:
                obj[h] = str(v) if v is not None else ""
        rows.append(obj)

    cache_set(cache_key, rows)
    return rows


def _tab_headers(tab: str) -> List[str]:
    _require_sheet_id()
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!1:1"
    ).execute()
    vals = res.get("values", [[]])
    return [h.strip() for h in (vals[0] if vals else [])]


def _find_row_index(tab: str, predicate) -> Optional[int]:
    """
    Ritorna l'indice riga (2-based, header = riga 1)
    """
    _require_sheet_id()

    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{tab}!A:Z"
    ).execute()

    values = res.get("values", [])
    if not values:
        return None

    headers = [h.strip() for h in values[0]]
    for idx, row in enumerate(values[1:], start=2):
        obj: Dict[str, str] = {}
        for i, h in enumerate(headers):
            v = row[i] if i < len(row) else ""
            obj[h] = v.strip() if isinstance(v, str) else (str(v) if v else "")
        if predicate(obj):
            return idx

    return None


def upsert_row(tab: str, key_predicate, data: Dict[str, Any]):
    """
    UPSERT su Google Sheets:
    - se la riga esiste -> UPDATE
    - altrimenti -> APPEND
    """
    headers = _tab_headers(tab)
    if not headers:
        raise RuntimeError(f"Tab '{tab}' non ha header")

    row_values: List[str] = []
    for h in headers:
        v = data.get(h, "")
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        elif v is None:
            v = ""
        else:
            v = str(v)
        row_values.append(v)

    row_idx = _find_row_index(tab, key_predicate)

    if row_idx is None:
        sheets().spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{tab}!A:Z",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row_values]},
        ).execute()
    else:
        end_col = chr(ord("A") + len(headers) - 1)
        sheets().spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{tab}!A{row_idx}:{end_col}{row_idx}",
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()

    cache_del(f"tab:{tab}")


# ============================================================
# Load shop config (multi-cliente reale)
# ============================================================
def load_shop_by_phone(phone: str) -> Optional[Dict[str, str]]:
    for s in load_tab("shops"):
        if phone_matches(phone, s.get("whatsapp_number", "")):
            return s
    return None


def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    out: Dict[int, List[Tuple[dt.time, dt.time]]] = {i: [] for i in range(7)}

    for r in load_tab("hours"):
        if (r.get("shop_id") or "").strip() != (shop_id or "").strip():
            continue
        try:
            wd = int(r.get("weekday", ""))
            st = dt.time.fromisoformat(r.get("start", "09:00"))
            en = dt.time.fromisoformat(r.get("end", "19:00"))
            out[wd].append((st, en))
        except Exception:
            continue

    for wd in out:
        out[wd].sort(key=lambda x: x[0])

    return out


def load_services(shop_id: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in load_tab("services"):
        if (r.get("shop_id") or "").strip() != (shop_id or "").strip():
            continue
        active = (r.get("active") or "TRUE").strip().lower()
        if active == "false":
            continue
        out.append(r)
    return out


# ============================================================
# Customers (memoria lunga)
# ============================================================
def get_customer(shop_id: str, phone: str) -> Optional[Dict[str, str]]:
    for r in load_tab("customers"):
        if (r.get("shop_id") or "").strip() == (shop_id or "").strip() \
                and phone_matches(phone, r.get("phone", "")):
            return r
    return None


def upsert_customer(shop_id: str, phone: str, last_service: str):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    prev = get_customer(shop_id, phone)

    total = 0
    if prev:
        try:
            total = int(prev.get("total_visits", "0"))
        except Exception:
            total = 0
    total += 1

    upsert_row(
        "customers",
        key_predicate=lambda r: (r.get("shop_id") or "") == shop_id
                                and phone_matches(phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "last_service": last_service,
            "total_visits": str(total),
            "last_visit": now_iso,
        },
    )


# ============================================================
# Sessions (memoria breve con TTL)
# ============================================================
def get_session(shop_id: str, phone: str) -> Optional[Dict[str, Any]]:
    for r in load_tab("sessions"):
        if (r.get("shop_id") or "") == shop_id \
                and phone_matches(phone, r.get("phone", "")):
            raw = r.get("data") or "{}"
            try:
                data_obj = json.loads(raw)
            except Exception:
                data_obj = {}

            return {
                "shop_id": r.get("shop_id"),
                "phone": r.get("phone"),
                "state": r.get("state", ""),
                "data": data_obj,
                "updated_at": r.get("updated_at", ""),
            }
    return None


def save_session(shop_id: str, phone: str, state: str, data: Dict[str, Any]):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    upsert_row(
        "sessions",
        key_predicate=lambda r: (r.get("shop_id") or "") == shop_id
                                and phone_matches(phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "state": state,
            "data": json.dumps(data, ensure_ascii=False),
            "updated_at": now_iso,
        },
    )


def reset_session(shop_id: str, phone: str):
    save_session(shop_id, phone, "", {})


def session_expired(sess: Dict[str, Any]) -> bool:
    raw = sess.get("updated_at") or ""
    if not raw:
        return True
    try:
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60
        return age > SESSION_TTL_MINUTES
    except Exception:
        return True
# ============================================================
# Google Calendar availability + capacity
# ============================================================
def count_overlaps(cal_id: str, start: dt.datetime, end: dt.datetime) -> int:
    """
    Conta quanti eventi (single events) si sovrappongono a [start, end)
    """
    svc = calendar()
    res = svc.events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
    ).execute()
    items = res.get("items", []) or []
    return len(items)


def slot_has_capacity(cal_id: str, start: dt.datetime, end: dt.datetime, capacity: int) -> bool:
    overlaps = count_overlaps(cal_id, start, end)
    return overlaps < max(1, int(capacity or 1))


def create_event(
    cal_id: str,
    start: dt.datetime,
    end: dt.datetime,
    tz: str,
    summary: str,
    phone: str,
    service_name: str,
    shop_name: str,
):
    """
    Crea evento su Google Calendar con metadata utili.
    """
    svc = calendar()
    ev = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "description": (
            f"Prenotazione WhatsApp\n"
            f"Salone: {shop_name}\n"
            f"Telefono: {norm_phone(phone)}\n"
            f"Servizio: {service_name}"
        ),
        "extendedProperties": {
            "private": {
                "phone": norm_phone(phone),
                "service": service_name,
                "shop": shop_name,
            }
        },
    }
    svc.events().insert(calendarId=cal_id, body=ev).execute()


# ============================================================
# Slot finding (orari, durata, capacity, alternative)
# ============================================================
def round_up_to_slot(dtobj: dt.datetime, slot_minutes: int) -> dt.datetime:
    dtobj = dtobj.replace(second=0, microsecond=0)
    sm = max(1, int(slot_minutes or 30))
    m = (dtobj.minute // sm) * sm
    base = dtobj.replace(minute=m)
    if base < dtobj:
        base += dt.timedelta(minutes=sm)
    return base


def in_open_hours(
    hours_map: Dict[int, List[Tuple[dt.time, dt.time]]],
    d: dt.date,
    start_t: dt.time,
    end_t: dt.time,
) -> bool:
    for st, en in hours_map.get(d.weekday(), []):
        if st <= start_t and end_t <= en:
            return True
    return False


def _combine_local(d: dt.date, t: dt.time, tz: str) -> dt.datetime:
    """
    Costruisce un datetime timezone-aware se ZoneInfo è disponibile,
    altrimenti naive.
    """
    zi = tzinfo_for(tz)
    x = dt.datetime.combine(d, t)
    return x.replace(tzinfo=zi) if zi else x


def find_slots(
    shop: Dict[str, str],
    hours_map: Dict[int, List[Tuple[dt.time, dt.time]]],
    service_duration_min: int,
    preferred_date: Optional[dt.date],
    exact_time: Optional[dt.time],
    after: Optional[dt.time],
    before: Optional[dt.time],
    limit: int = 5,
    max_days: int = 14,
) -> List[dt.datetime]:
    """
    Trova i prossimi slot disponibili considerando:
    - orari apertura in 'hours'
    - durata servizio
    - slot_minutes
    - capacity (eventi sovrapposti < capacity)
    - vincoli after/before (fasce)
    """
    tz = shop.get("timezone", "Europe/Rome")
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")
    capacity = int(shop.get("capacity", "1") or "1")
    cal_id = shop.get("calendar_id", "")

    now_l = now_local(tz)
    today = now_l.date()

    base = preferred_date or today
    if base < today:
        base = today

    duration = dt.timedelta(minutes=max(1, int(service_duration_min or 30)))
    results: List[dt.datetime] = []

    # 1) se ho data + orario preciso -> provo prima quello
    if preferred_date and exact_time:
        start = _combine_local(preferred_date, exact_time, tz)
        end = start + duration

        if in_open_hours(hours_map, preferred_date, start.time(), end.time()):
            if not (preferred_date == today and start < now_l):
                if slot_has_capacity(cal_id, start, end, capacity):
                    return [start]

    # 2) scan generale
    max_days = max(0, int(max_days or 14))
    for day_off in range(0, max_days + 1):
        d = base + dt.timedelta(days=day_off)
        ranges = hours_map.get(d.weekday(), [])
        if not ranges:
            continue

        for st, en in ranges:
            start_dt = _combine_local(d, st, tz)
            end_dt = _combine_local(d, en, tz)

            # vincoli after/before
            if after:
                tmp = _combine_local(d, after, tz)
                if tmp > start_dt:
                    start_dt = tmp
            if before:
                tmp = _combine_local(d, before, tz)
                if tmp < end_dt:
                    end_dt = tmp

            if end_dt <= start_dt:
                continue

            # oggi: dal prossimo slot >= now
            if d == today:
                start_dt = max(start_dt, round_up_to_slot(now_l, slot_minutes))

            cur = round_up_to_slot(start_dt, slot_minutes)
            while cur + duration <= end_dt:
                if slot_has_capacity(cal_id, cur, cur + duration, capacity):
                    results.append(cur)
                    if len(results) >= limit:
                        return results
                cur += dt.timedelta(minutes=slot_minutes)

    return results


def format_slot(shop_tz: str, d: dt.datetime) -> str:
    """
    Esempio: "Mer 18/12 18:30"
    """
    zi = tzinfo_for(shop_tz)
    dd = d.astimezone(zi) if (zi and d.tzinfo) else d
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[dd.weekday()]} {dd.strftime('%d/%m')} {dd.strftime('%H:%M')}"


def render_slots(shop: Dict[str, str], title: str, slots: List[dt.datetime]) -> str:
    lines = [title]
    for i, sl in enumerate(slots, start=1):
        lines.append(f"{i}) {format_slot(shop.get('timezone', 'Europe/Rome'), sl)}")
    lines.append("\nRispondi con il numero (1,2,3...) oppure scrivi un giorno/orario diverso.")
    return "\n".join(lines)
# ============================================================
# Services selection + upsell helpers
# ============================================================
CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}


def service_duration(svc: Dict[str, str]) -> int:
    try:
        return int((svc.get("duration") or svc.get("duration_minutes") or "30").strip())
    except Exception:
        return 30


def pick_service_from_text(services: List[Dict[str, str]], text: str) -> Optional[Dict[str, str]]:
    t = (text or "").lower()

    for s in services:
        name = (s.get("name", "") or "").lower()
        if name and name in t:
            return s

    if "barba" in t:
        for s in services:
            if "barba" in (s.get("name", "").lower()):
                return s

    if "taglio" in t:
        for s in services:
            if "taglio" in (s.get("name", "").lower()):
                return s

    return None


def find_combo_taglio_barba(services: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for s in services:
        n = (s.get("name", "").lower())
        if "taglio" in n and "barba" in n:
            return s
    return None


def has_barba_service(services: List[Dict[str, str]]) -> bool:
    return any("barba" in (s.get("name", "").lower()) for s in services)


# ============================================================
# ✅ CORE BOT LOGIC (FIX DEFINITIVO)
# ============================================================
def handle_message(shop: Dict[str, str], phone: str, text: str) -> str:
    tz = shop.get("timezone", "Europe/Rome")
    shop_id = shop.get("shop_id", "")
    shop_name = shop.get("name", "il salone")
    gender = (shop.get("gender", "unisex") or "unisex").lower()
    capacity = int(shop.get("capacity", "1") or "1")

    services = load_services(shop_id)
    hours_map = load_hours(shop_id)

    t = (text or "").strip()
    tlow = t.lower().strip()

    # =========================
    # Session
    # =========================
    sess = get_session(shop_id, phone)
    if sess and session_expired(sess):
        reset_session(shop_id, phone)
        sess = None

    state = (sess.get("state") if sess else "") or ""
    data = (sess.get("data") if sess else {}) or {}
    if not isinstance(data, dict):
        data = {}

    # =========================
    # CANCEL
    # =========================
    if any(w in tlow for w in CANCEL_WORDS):
        reset_session(shop_id, phone)
        return "Ok 👍 Nessun problema. Se vuoi riprenotare dimmi pure giorno e orario."

    # =========================
    # GREETING
    # =========================
    if tlow in {"ciao", "salve", "buongiorno", "buonasera"} and not sess:
        return (
            f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    # =========================
    # PARSING
    # =========================
    date_ = parse_date(t, tz)
    exact_time = parse_time(t)
    after, before = parse_window(t)

    # =========================
    # SERVICE
    # =========================
    chosen_service = data.get("service") or pick_service_from_text(services, t)
    if not chosen_service and services:
        if len(services) == 1:
            chosen_service = services[0]

    # =========================
    # UPSALE BARBA (FIX)
    # =========================
    if state == "upsell_barba":
        # 👉 FIX QUI
        if "solo taglio" in tlow:
            chosen_service = data["service"]
        elif ("taglio e barba" in tlow or ("taglio" in tlow and "barba" in tlow)):
            combo = find_combo_taglio_barba(services)
            if combo:
                chosen_service = combo
        # continuiamo senza perdere data/orario
        state = ""
        save_session(shop_id, phone, "", {
            **data,
            "service": chosen_service,
            "upsell_barba_done": True
        })

    # =========================
    # UPSALE (trigger)
    # =========================
    if gender == "uomo" and isinstance(chosen_service, dict):
    chosen_name = (chosen_service.get("name") or "").lower()
        and has_barba_service(services)
        and not data.get("upsell_barba_done")
        and "barba" not in tlow
    ):
        save_session(shop_id, phone, "upsell_barba", {
            **data,
            "service": chosen_service,
            "upsell_barba_done": True
        })
        return (
            "Perfetto 👍 Vuoi aggiungere anche la *barba* oppure solo *taglio*?\n"
            "• Scrivi “solo taglio” oppure “taglio e barba”."
        )

    # =========================
    # WHEN missing
    # =========================
    if chosen_service and not date_ and not exact_time and not after and not before:
        save_session(shop_id, phone, "need_when", {"service": chosen_service})
        return "Quando preferisci venire? (es. “domani alle 18”)."

    if chosen_service and date_ and not exact_time and not after and not before:
        save_session(shop_id, phone, "need_time", {
            "service": chosen_service,
            "date": date_.isoformat()
        })
        return f"Ok 👍 {date_.strftime('%d/%m')} a che ora?"

    if chosen_service and (exact_time or after or before) and not date_:
        save_session(shop_id, phone, "need_date", {
            "service": chosen_service,
            "time": exact_time.isoformat() if exact_time else "",
            "after": after.isoformat() if after else "",
            "before": before.isoformat() if before else "",
        })
        return "Perfetto 👍 Per che giorno?"

    # =========================
    # RECOVER STATE
    # =========================
    if state == "need_date":
        date_ = parse_date(t, tz)
        if not date_:
            return "Dimmi il giorno (es. domani, mercoledì, 17/12)."

    if state == "need_time":
        exact_time = parse_time(t)
        if not exact_time:
            return "Dimmi un orario valido (es. 18:00)."

    # =========================
    # FIND SLOTS
    # =========================
    dur = service_duration(chosen_service)
    slots = find_slots(
        shop,
        hours_map,
        dur,
        preferred_date=date_,
        exact_time=exact_time,
        after=after,
        before=before,
        limit=5,
        max_days=14
    )

    if not slots:
        return "Non vedo disponibilità 😕 Vuoi un altro giorno o fascia?"

    start = slots[0]

    save_session(shop_id, phone, "confirm", {
        "service": chosen_service,
        "slot_iso": start.isoformat()
    })

    return (
        f"Perfetto 👍 Confermi questo appuntamento?\n"
        f"💈 *{chosen_service.get('name')}*\n"
        f"🕒 {format_slot(tz, start)}\n\n"
        f"Rispondi *OK* per confermare oppure *annulla*."
    )


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"


@app.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "ciao")

    try:
        shop = load_shop_by_phone(phone)
        if not shop:
            return jsonify({"error": "shop non trovato"}), 404

        reply = handle_message(shop, phone, msg)
        return jsonify({
            "shop": shop.get("name"),
            "phone": norm_phone(phone),
            "message_in": msg,
            "bot_reply": reply
        })
    except Exception as e:
        return jsonify({"error": "server_error", "details": str(e)}), 500


@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True})
    
