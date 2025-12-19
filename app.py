from __future__ import annotations

import os
import re
import json
import difflib
import datetime as dt
import unicodedata
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
        _sheets = build("sheets", "v4", credentials=_creds(), cache_discovery=False)
    return _sheets


def calendar():
    global _calendar
    if _calendar is None:
        _calendar = build("calendar", "v3", credentials=_creds(), cache_discovery=False)
    return _calendar


# ============================================================
# Small cache (reduce API calls)
# ============================================================
_CACHE: Dict[str, Dict[str, Any]] = {}  # key -> {"ts": datetime, "data": ...}


def cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    if (dt.datetime.utcnow() - item["ts"]).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return item["data"]


def cache_set(key: str, data: Any):
    _CACHE[key] = {"ts": dt.datetime.utcnow(), "data": data}


def cache_del(key: str):
    if key in _CACHE:
        del _CACHE[key]


# ============================================================
# Helpers: normalization / phone
# ============================================================
def norm_phone(p: str) -> str:
    if not p:
        return ""
    p = p.strip().lower()
    p = p.replace("whatsapp:", "").strip()
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


def _strip_accents(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join([c for c in s if not unicodedata.combining(c)])


def norm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = _strip_accents(s)
    s = re.sub(r"[^a-z0-9\s\+\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ============================================================
# Time / Date parsing (IT + frasi vaghe)
# ============================================================
WEEKDAYS_IT = {
    "lunedi": 0, "lun": 0, "lunedì": 0,
    "martedi": 1, "mar": 1, "martedì": 1,
    "mercoledi": 2, "mer": 2, "mercoledì": 2,
    "giovedi": 3, "gio": 3, "giovedì": 3,
    "venerdi": 4, "ven": 4, "venerdì": 4,
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
    t = norm_text(text)
    today = now_local(shop_tz).date()

    if "oggi" in t:
        return today
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    if "domani" in t:
        return today + dt.timedelta(days=1)

    # "questa settimana" -> non è una data, ma segnala range
    # qui ritorniamo None e lo gestiamo nel planner con flag
    # (vedi parse_flags)
    # "stasera" -> oggi
    if "stasera" in t or "staser" in t:
        return today

    # dd/mm(/yyyy)
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

    # weekday (prossimo)
    for k, wd in WEEKDAYS_IT.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + dt.timedelta(days=delta)

    return None


def parse_time(text: str) -> Optional[dt.time]:
    t = norm_text(text)

    # 17:30 / 17.30
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # 1730
    m = re.search(r"\b([01]\d|2[0-3])([0-5]\d)\b", t)
    if m:
        return dt.time(int(m.group(1)), int(m.group(2)))

    # "alle 18" / "ore 18"
    m = re.search(r"\b(?:alle|ore)\s*([01]?\d|2[0-3])\b", t)
    if m:
        return dt.time(int(m.group(1)), 0)

    # solo "18" se c'è contesto
    m = re.search(r"\b([01]?\d|2[0-3])\b", t)
    if m and any(x in t for x in ["alle", "ore", "dopo", "prima", "verso"]):
        return dt.time(int(m.group(1)), 0)

    return None


def parse_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    t = norm_text(text)
    after = None
    before = None

    # dopo le 18
    m = re.search(r"\bdopo\s+le?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        after = parse_time(m.group(1))

    # prima delle 17
    m = re.search(r"\bprima\s+delle?\s+([0-2]?\d(?:[:\.][0-5]\d)?)\b", t)
    if m:
        before = parse_time(m.group(1))

    # fasce classiche + sinonimi
    if any(x in t for x in ["mattina", "al mattino"]):
        after = after or dt.time(9, 0)
        before = before or dt.time(12, 0)

    if any(x in t for x in ["pomeriggio", "nel pomeriggio", "tardo pomeriggio"]):
        after = after or dt.time(14, 0)
        before = before or dt.time(19, 0)

    if any(x in t for x in ["sera", "in serata", "verso sera", "stasera"]):
        after = after or dt.time(17, 30)
        before = before or dt.time(22, 0)

    return after, before


def parse_flags(text: str) -> Dict[str, bool]:
    t = norm_text(text)
    return {
        "this_week": ("questa settimana" in t or "in settimana" in t),
        "weekend": ("weekend" in t or "fine settimana" in t),
        "asap": ("prima possibile" in t or "appena puoi" in t or "appena potete" in t),
    }


# ============================================================
# Sheets helpers
# ============================================================
def _require_sheet_id():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")


def _col_to_a1(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA ..."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def load_tab(tab: str) -> List[Dict[str, str]]:
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

    headers = [str(h).strip() for h in values[0]]
    rows: List[Dict[str, str]] = []
    for row in values[1:]:
        obj: Dict[str, str] = {}
        for i, h in enumerate(headers):
            v = row[i] if i < len(row) else ""
            obj[h] = v.strip() if isinstance(v, str) else (str(v) if v is not None else "")
        rows.append(obj)

    cache_set(cache_key, rows)
    return rows


def _tab_headers(tab: str) -> List[str]:
    _require_sheet_id()
    rng = f"{tab}!1:1"
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=rng
    ).execute()
    vals = res.get("values", [[]])
    return [str(h).strip() for h in (vals[0] if vals else [])]


def _find_row_index(tab: str, predicate) -> Optional[int]:
    _require_sheet_id()

    rng = f"{tab}!A:Z"
    res = sheets().spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=rng
    ).execute()

    values = res.get("values", [])
    if not values:
        return None

    headers = [str(h).strip() for h in values[0]]
    for idx, row in enumerate(values[1:], start=2):
        obj: Dict[str, str] = {}
        for i, h in enumerate(headers):
            v = row[i] if i < len(row) else ""
            obj[h] = v.strip() if isinstance(v, str) else (str(v) if v is not None else "")
        if predicate(obj):
            return idx
    return None


def upsert_row(tab: str, key_predicate, data: Dict[str, Any]):
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
        end_col = _col_to_a1(len(headers))
        rng = f"{tab}!A{row_idx}:{end_col}{row_idx}"
        sheets().spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=rng,
            valueInputOption="RAW",
            body={"values": [row_values]},
        ).execute()

    cache_del(f"tab:{tab}")


# ============================================================
# Load shop config (multi-salone)
# ============================================================
def load_shop_by_phone(shop_number: str) -> Optional[Dict[str, str]]:
    shops = load_tab("shops")
    for s in shops:
        if phone_matches(shop_number, s.get("whatsapp_number", "")):
            return s
    return None


def load_hours(shop_id: str) -> Dict[int, List[Tuple[dt.time, dt.time]]]:
    rows = load_tab("hours")
    out: Dict[int, List[Tuple[dt.time, dt.time]]] = {i: [] for i in range(7)}
    for r in rows:
        if (r.get("shop_id") or "").strip() != (shop_id or "").strip():
            continue
        try:
            wd = int((r.get("weekday") or "").strip())
            st = dt.time.fromisoformat((r.get("start") or "09:00").strip())
            en = dt.time.fromisoformat((r.get("end") or "19:00").strip())
            out[wd].append((st, en))
        except Exception:
            continue
    for wd in out:
        out[wd].sort(key=lambda x: x[0])
    return out


def load_services(shop_id: str) -> List[Dict[str, str]]:
    rows = load_tab("services")
    out: List[Dict[str, str]] = []
    for r in rows:
        if (r.get("shop_id") or "").strip() != (shop_id or "").strip():
            continue
        active_val = (r.get("active") or "TRUE").strip().lower()
        if active_val == "false":
            continue
        out.append(r)
    return out


# ============================================================
# Customers + Sessions (multi-cliente reale)
# ============================================================
def _session_key_matcher(shop_id: str, customer_phone: str):
    return lambda r: (r.get("shop_id") or "").strip() == (shop_id or "").strip() and phone_matches(customer_phone, r.get("phone", ""))


def get_customer(shop_id: str, customer_phone: str) -> Optional[Dict[str, str]]:
    rows = load_tab("customers")
    for r in rows:
        if (r.get("shop_id") or "").strip() == (shop_id or "").strip() and phone_matches(customer_phone, r.get("phone", "")):
            return r
    return None


def upsert_customer(shop_id: str, customer_phone: str, last_service: str):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    prev = get_customer(shop_id, customer_phone)
    total = 0
    if prev:
        try:
            total = int(prev.get("total_visits", "0") or "0")
        except Exception:
            total = 0
    total += 1

    upsert_row(
        "customers",
        key_predicate=lambda r: (r.get("shop_id") or "").strip() == (shop_id or "").strip()
                                and phone_matches(customer_phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(customer_phone),
            "last_service": last_service,
            "total_visits": str(total),
            "last_visit": now_iso,
        },
    )


def get_session(shop_id: str, customer_phone: str) -> Optional[Dict[str, Any]]:
    rows = load_tab("sessions")
    for r in rows:
        if (r.get("shop_id") or "").strip() == (shop_id or "").strip() and phone_matches(customer_phone, r.get("phone", "")):
            raw_data = r.get("data", "") or "{}"
            try:
                data_obj = json.loads(raw_data) if isinstance(raw_data, str) else {}
            except Exception:
                data_obj = {}
            return {
                "shop_id": r.get("shop_id", shop_id),
                "phone": r.get("phone", norm_phone(customer_phone)),
                "state": r.get("state", ""),
                "data": data_obj if isinstance(data_obj, dict) else {},
                "updated_at": r.get("updated_at", ""),
            }
    return None


def save_session(shop_id: str, customer_phone: str, state: str, data: Dict[str, Any]):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    upsert_row(
        "sessions",
        key_predicate=_session_key_matcher(shop_id, customer_phone),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(customer_phone),
            "state": state,
            "data": json.dumps(data, ensure_ascii=False),
            "updated_at": now_iso,
        },
    )


def reset_session(shop_id: str, customer_phone: str):
    save_session(shop_id, customer_phone, "", {})


def session_expired(sess: Dict[str, Any]) -> bool:
    raw = sess.get("updated_at") or ""
    if not raw:
        return True
    try:
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_min = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60
        return age_min > SESSION_TTL_MINUTES
    except Exception:
        return True


# ============================================================
# Google Calendar availability + capacity
# ============================================================
def count_overlaps(cal_id: str, start: dt.datetime, end: dt.datetime) -> int:
    svc = calendar()
    res = svc.events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250
    ).execute()
    return len(res.get("items", []) or [])


def slot_has_capacity(cal_id: str, start: dt.datetime, end: dt.datetime, capacity: int) -> bool:
    overlaps = count_overlaps(cal_id, start, end)
    return overlaps < max(1, capacity)


def create_event(cal_id: str, start: dt.datetime, end: dt.datetime, tz: str, summary: str,
                 customer_phone: str, service_name: str, shop_name: str):
    svc = calendar()
    ev = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "description": (
            f"Prenotazione WhatsApp\n"
            f"Salone: {shop_name}\n"
            f"Telefono cliente: {norm_phone(customer_phone)}\n"
            f"Servizio: {service_name}"
        ),
        "extendedProperties": {
            "private": {
                "customer_phone": norm_phone(customer_phone),
                "service": service_name,
                "shop": shop_name
            }
        }
    }
    svc.events().insert(calendarId=cal_id, body=ev).execute()


# ============================================================
# Slot finding
# ============================================================
def round_up_to_slot(dtobj: dt.datetime, slot_minutes: int) -> dt.datetime:
    dtobj = dtobj.replace(second=0, microsecond=0)
    m = (dtobj.minute // slot_minutes) * slot_minutes
    base = dtobj.replace(minute=m)
    if base < dtobj:
        base += dt.timedelta(minutes=slot_minutes)
    return base


def in_open_hours(hours_map: Dict[int, List[Tuple[dt.time, dt.time]]], d: dt.date, start_t: dt.time, end_t: dt.time) -> bool:
    for st, en in hours_map.get(d.weekday(), []):
        if st <= start_t and end_t <= en:
            return True
    return False


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
    tz = shop.get("timezone", "Europe/Rome")
    tzinfo = tzinfo_for(tz)
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")
    capacity = int(shop.get("capacity", "1") or "1")
    cal_id = shop["calendar_id"]

    now_l = now_local(tz)
    today = now_l.date()

    base = preferred_date or today
    if base < today:
        base = today

    duration = dt.timedelta(minutes=service_duration_min)
    results: List[dt.datetime] = []

    # prova slot esatto
    if preferred_date and exact_time:
        start = dt.datetime.combine(preferred_date, exact_time)
        if tzinfo:
            start = start.replace(tzinfo=tzinfo)
        end = start + duration
        if in_open_hours(hours_map, preferred_date, start.time(), end.time()):
            if not (preferred_date == today and start < now_l):
                if slot_has_capacity(cal_id, start, end, capacity):
                    return [start]

    for day_off in range(0, max_days + 1):
        d = base + dt.timedelta(days=day_off)
        ranges = hours_map.get(d.weekday(), [])
        if not ranges:
            continue

        for st, en in ranges:
            start_dt = dt.datetime.combine(d, st)
            end_dt = dt.datetime.combine(d, en)
            if tzinfo:
                start_dt = start_dt.replace(tzinfo=tzinfo)
                end_dt = end_dt.replace(tzinfo=tzinfo)

            if after:
                tmp = dt.datetime.combine(d, after)
                if tzinfo:
                    tmp = tmp.replace(tzinfo=tzinfo)
                start_dt = max(start_dt, tmp)

            if before:
                tmp = dt.datetime.combine(d, before)
                if tzinfo:
                    tmp = tmp.replace(tzinfo=tzinfo)
                end_dt = min(end_dt, tmp)

            if end_dt <= start_dt:
                continue

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
    tzinfo = tzinfo_for(shop_tz)
    dd = d.astimezone(tzinfo) if (tzinfo and d.tzinfo) else d
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    return f"{giorni[dd.weekday()]} {dd.strftime('%d/%m')} {dd.strftime('%H:%M')}"


def render_slots(shop: Dict[str, str], title: str, slots: List[dt.datetime]) -> str:
    lines = [title]
    for i, sl in enumerate(slots, start=1):
        lines.append(f"{i}) {format_slot(shop.get('timezone', 'Europe/Rome'), sl)}")
    lines.append("\nRispondi con il numero (1,2,3...) oppure scrivi un giorno/orario diverso.")
    return "\n".join(lines)


def pick_better_alternatives(
    shop: Dict[str, str],
    hours_map: Dict[int, List[Tuple[dt.time, dt.time]]],
    duration_min: int,
    requested_date: dt.date,
    requested_time: dt.time,
    after: Optional[dt.time],
    before: Optional[dt.time],
    limit: int = 5,
) -> List[dt.datetime]:
    """
    Alternativa “furba” quando l’orario esatto non è disponibile:
    - prova prima slot vicini (dopo) nello stesso giorno
    - poi stesso orario nei giorni successivi
    - poi eventuali buchi in giornata (prima)
    """
    tz = shop.get("timezone", "Europe/Rome")
    tzinfo = tzinfo_for(tz)
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")

    # 1) stesso giorno, subito dopo (entro 2 ore)
    slots1 = find_slots(
        shop, hours_map, duration_min,
        preferred_date=requested_date,
        exact_time=None,
        after=after or requested_time,
        before=before,
        limit=10,
        max_days=0
    )
    # riordina: quelli più vicini dopo l’orario
    req_dt = dt.datetime.combine(requested_date, requested_time)
    if tzinfo:
        req_dt = req_dt.replace(tzinfo=tzinfo)
    slots1_sorted = sorted(slots1, key=lambda x: abs((x - req_dt).total_seconds()))
    out: List[dt.datetime] = []
    for s in slots1_sorted:
        if s >= req_dt and (s - req_dt) <= dt.timedelta(hours=2):
            out.append(s)
        if len(out) >= limit:
            return out

    # 2) stesso orario nei prossimi giorni (fino a 7)
    slots2 = find_slots(
        shop, hours_map, duration_min,
        preferred_date=requested_date + dt.timedelta(days=1),
        exact_time=requested_time,
        after=None,
        before=None,
        limit=10,
        max_days=7
    )
    for s in slots2:
        if len(out) >= limit:
            return out
        out.append(s)

    # 3) buchi prima (entro 2 ore)
    slots3 = find_slots(
        shop, hours_map, duration_min,
        preferred_date=requested_date,
        exact_time=None,
        after=after,
        before=before or requested_time,
        limit=10,
        max_days=0
    )
    slots3_sorted = sorted(slots3, key=lambda x: abs((x - req_dt).total_seconds()))
    for s in slots3_sorted:
        if s < req_dt and (req_dt - s) <= dt.timedelta(hours=2):
            out.append(s)
        if len(out) >= limit:
            return out

    # 4) fallback: prossimi disponibili generici
    if len(out) < limit:
        slots4 = find_slots(
            shop, hours_map, duration_min,
            preferred_date=requested_date,
            exact_time=None,
            after=None,
            before=None,
            limit=limit,
            max_days=7
        )
        for s in slots4:
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                break

    # pulizia duplicati
    uniq = []
    seen = set()
    for s in out:
        k = s.isoformat()
        if k not in seen:
            uniq.append(s)
            seen.add(k)
    return uniq[:limit]


# ============================================================
# Service selection (fuzzy) + upsell
# ============================================================
CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "si", "sì", "perfetto", "certo"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}


def service_duration(svc: Dict[str, str]) -> int:
    try:
        return int((svc.get("duration") or svc.get("duration_minutes") or "30").strip())
    except Exception:
        return 30


def _service_phrases(s: Dict[str, str]) -> List[str]:
    """
    Genera possibili etichette del servizio:
    - name
    - eventuali alias/keywords nel foglio (facoltativi): aliases, keywords
      (separati da virgola)
    """
    out = []
    name = (s.get("name") or "").strip()
    if name:
        out.append(norm_text(name))
    aliases = (s.get("aliases") or s.get("alias") or "").strip()
    if aliases:
        for a in aliases.split(","):
            a = norm_text(a)
            if a:
                out.append(a)
    keywords = (s.get("keywords") or "").strip()
    if keywords:
        for k in keywords.split(","):
            k = norm_text(k)
            if k:
                out.append(k)
    return list(dict.fromkeys(out))


def pick_service_from_text(services: List[Dict[str, str]], text: str) -> Optional[Dict[str, str]]:
    t = norm_text(text)
    if not t or not services:
        return None

    # 1) match diretto (contiene nome/alias)
    for s in services:
        for ph in _service_phrases(s):
            if ph and ph in t:
                return s

    # 2) keyword “classiche”
    keywords = [
        ("barba", ["barba", "rasatura"]),
        ("taglio", ["taglio", "capelli"]),
        ("colore", ["colore", "tinta", "meches", "balayage"]),
        ("piega", ["piega", "phon", "brushing"]),
        ("ceretta", ["ceretta", "wax"]),
        ("manicure", ["manicure", "unghie"]),
        ("pedicure", ["pedicure"]),
        ("pulizia viso", ["pulizia viso", "viso"]),
        ("massaggio", ["massaggio"]),
    ]
    for _, kws in keywords:
        if any(k in t for k in kws):
            # prova a scegliere un servizio che contenga una delle kws nel nome
            for s in services:
                n = norm_text(s.get("name", ""))
                if any(k in n for k in kws):
                    return s

    # 3) fuzzy match su nome servizio (tgalio -> taglio)
    names = [(s, norm_text(s.get("name", ""))) for s in services if (s.get("name") or "").strip()]
    candidates = [n for _, n in names]
    # prendiamo le parole principali dal testo
    tokens = [w for w in t.split() if len(w) >= 4]
    for tok in tokens[:6]:
        close = difflib.get_close_matches(tok, candidates, n=1, cutoff=0.80)
        if close:
            best = close[0]
            for s, nn in names:
                if nn == best:
                    return s

    return None


def default_service_if_single(services: List[Dict[str, str]], gender: str) -> Optional[Dict[str, str]]:
    if len(services) == 1:
        return services[0]
    if gender == "uomo":
        preferred = {"taglio", "taglio uomo", "taglio capelli", "taglio uomo (30m)", "taglio uomo 30m"}
        for s in services:
            if norm_text(s.get("name", "")) in preferred:
                return s
    return None


def has_service_with_keyword(services: List[Dict[str, str]], kw: str) -> bool:
    kw = norm_text(kw)
    return any(kw in norm_text(s.get("name", "")) for s in services)


def find_combo_taglio_barba(services: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for s in services:
        n = norm_text(s.get("name", ""))
        if "taglio" in n and "barba" in n:
            return s
    return None


def services_bullets(services: List[Dict[str, str]], limit: int = 10) -> str:
    out = []
    for s in services[:limit]:
        nm = (s.get("name") or "").strip()
        if nm:
            out.append(f"• {nm}")
    return "\n".join(out)


# ============================================================
# ✅ CORE BOT LOGIC v2
# ============================================================
def handle_message(shop: Dict[str, str], shop_number: str, customer_phone: str, text: str) -> str:
    tz = shop.get("timezone", "Europe/Rome")
    shop_id = shop.get("shop_id", "") or shop.get("id", "") or ""
    shop_name = shop.get("name", "il salone")
    gender = norm_text(shop.get("gender", "unisex") or "unisex")
    capacity = int(shop.get("capacity", "1") or "1")

    services = load_services(shop_id)
    hours_map = load_hours(shop_id)

    t_raw = (text or "").strip()
    tlow = norm_text(t_raw)

    # session
    sess = get_session(shop_id, customer_phone)
    if sess and session_expired(sess):
        reset_session(shop_id, customer_phone)
        sess = None

    state = (sess.get("state") if sess else "") or ""
    data = (sess.get("data") if sess else {}) or {}
    if not isinstance(data, dict):
        data = {}

    # cancel
    if any(w in tlow for w in [norm_text(x) for x in CANCEL_WORDS]):
        reset_session(shop_id, customer_phone)
        return "Ok 👍 Nessun problema. Se vuoi riprenotare, dimmi pure giorno e orario (es. “domani alle 18”)."

    # greeting
    if tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"} and not state:
        cust = get_customer(shop_id, customer_phone)
        if cust and cust.get("last_service"):
            return (
                f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
                f"Ultima volta hai fatto: *{cust.get('last_service')}*.\n\n"
                f"Quando vuoi prenotare?"
            )
        return (
            f"Ciao! 👋 Sei in contatto con *{shop_name}* 💈\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    # parse request
    flags = parse_flags(t_raw)
    date_ = parse_date(t_raw, tz)
    exact_time = parse_time(t_raw)
    after, before = parse_window(t_raw)

    # se frase tipo “questa settimana dopo le 18”
    if flags.get("this_week") and not date_:
        # ci interessa il range; partiamo da oggi, max fino a domenica o lookahead
        date_ = now_local(tz).date()  # base per partire
        if not after and not exact_time:
            after = dt.time(18, 0)

    # se frase tipo “verso sera”
    if ("verso sera" in tlow or "in serata" in tlow) and not after and not exact_time:
        after = dt.time(18, 0)

    wants_booking = (
        any(x in tlow for x in ["prenot", "appunt", "posto", "disponib", "libero", "buco", "passo", "riesco"]) or
        bool(date_) or bool(exact_time) or bool(after) or bool(before) or flags.get("this_week") or flags.get("asap")
    )

    # determine chosen service
    chosen_service: Optional[Dict[str, str]] = pick_service_from_text(services, t_raw) if services else None
    if data.get("service"):
        chosen_service = data.get("service")

    if not chosen_service and services:
        default = default_service_if_single(services, gender)
        if default:
            chosen_service = default

    # --------------------------------------------------------
    # STATE: choose slot by number
    # --------------------------------------------------------
    if state == "choose":
        options = data.get("options") or []
        service = data.get("service") or chosen_service

        m = re.search(r"\b(\d{1,2})\b", tlow)
        if m and options:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(options):
                start = dt.datetime.fromisoformat(options[idx])
                save_session(shop_id, customer_phone, "confirm", {
                    "service": service,
                    "slot_iso": start.isoformat(),
                })
                return (
                    f"Confermi questo appuntamento?\n"
                    f"💈 *{service.get('name', 'Servizio')}*\n"
                    f"🕒 {format_slot(tz, start)}\n\n"
                    f"Rispondi *OK* per confermare oppure *annulla*."
                )

        # se scrive altro, non lo blocchiamo: ripartiamo interpretando testo
        reset_session(shop_id, customer_phone)
        state = ""
        data = {}
        # e continuiamo sotto

    # --------------------------------------------------------
    # STATE: confirm
    # --------------------------------------------------------
    if state == "confirm":
        if tlow in {norm_text(x) for x in CONFIRM_WORDS}:
            slot_iso = data.get("slot_iso")
            service = data.get("service")
            if not slot_iso or not service:
                reset_session(shop_id, customer_phone)
                return "Ops, ho perso i dettagli 😅 Ripartiamo: quando vuoi venire?"
            start = dt.datetime.fromisoformat(slot_iso)
            dur = service_duration(service)
            end = start + dt.timedelta(minutes=dur)

            if not slot_has_capacity(shop["calendar_id"], start, end, capacity):
                reset_session(shop_id, customer_phone)
                alt = find_slots(shop, hours_map, dur, start.date(), None, None, None, limit=5, max_days=7)
                if not alt:
                    return "Quello slot è appena stato preso 😅 Vuoi indicarmi un’altra fascia?"
                save_session(shop_id, customer_phone, "choose", {"service": service, "options": [x.isoformat() for x in alt]})
                return render_slots(shop, "Quell’orario non è più disponibile. Ecco alcune alternative:", alt)

            create_event(
                cal_id=shop["calendar_id"],
                start=start,
                end=end,
                tz=tz,
                summary=f"{shop_name} - {service.get('name', 'Servizio')}",
                customer_phone=customer_phone,
                service_name=service.get("name", ""),
                shop_name=shop_name
            )
            upsert_customer(shop_id, customer_phone, service.get("name", ""))
            reset_session(shop_id, customer_phone)

            return (
                f"✅ Prenotazione confermata da *{shop_name}*.\n"
                f"💈 *{service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"A presto 👋"
            )

        # se non conferma, riparte
        reset_session(shop_id, customer_phone)
        state = ""
        data = {}
        # e continuiamo sotto

    # --------------------------------------------------------
    # Se non è booking -> risposta guida
    # --------------------------------------------------------
    if not wants_booking:
        if not chosen_service and len(services) > 1:
            return (
                f"Dimmi pure quando vuoi venire 😊 (es. “domani alle 18”, “sabato mattina”, “questa settimana dopo le 18”).\n\n"
                f"Se vuoi, dimmi anche il servizio:\n{services_bullets(services, 10)}"
            )
        return "Dimmi pure quando vuoi venire (es. *domani alle 18*, *venerdì pomeriggio*)."

    # --------------------------------------------------------
    # Upsell “barba” (ma senza perdere la data/ora già detta)
    # --------------------------------------------------------
    if gender == "uomo" and chosen_service:
        chosen_name = norm_text(chosen_service.get("name", ""))
        barba_available = has_service_with_keyword(services, "barba")
        combo = find_combo_taglio_barba(services)

        # se sta chiedendo taglio e non ha citato barba, proponi (una volta)
        if "taglio" in chosen_name and barba_available and not data.get("upsell_barba_done") and "barba" not in tlow:
            # salva la richiesta “quando” in pending per non perdere contesto
            pending = {
                "date": date_.isoformat() if date_ else None,
                "time": exact_time.isoformat() if exact_time else None,
                "after": after.isoformat() if after else None,
                "before": before.isoformat() if before else None,
                "flags": flags,
            }
            save_session(shop_id, customer_phone, "upsell_barba", {
                "service": chosen_service,
                "upsell_barba_done": True,
                "pending": pending
            })
            return (
                "Perfetto 👍 Vuoi aggiungere anche la *barba* oppure solo *taglio*?\n"
                "• Scrivi “solo taglio” oppure “taglio e barba”."
            )

        if state == "upsell_barba":
            pending = data.get("pending") or {}
            # ripristina preferenze di quando
            if not date_ and pending.get("date"):
                try:
                    date_ = dt.date.fromisoformat(pending["date"])
                except Exception:
                    pass
            if not exact_time and pending.get("time"):
                try:
                    exact_time = dt.time.fromisoformat(pending["time"])
                except Exception:
                    pass
            if not after and pending.get("after"):
                try:
                    after = dt.time.fromisoformat(pending["after"])
                except Exception:
                    pass
            if not before and pending.get("before"):
                try:
                    before = dt.time.fromisoformat(pending["before"])
                except Exception:
                    pass

            # scelta upsell
            if ("taglio e barba" in tlow or ("taglio" in tlow and "barba" in tlow)) and combo:
                chosen_service = combo
            # “solo taglio” -> resta com’è, ma prosegui
            # chiudi stato upsell e vai avanti
            state = ""
            data = {"service": chosen_service, "upsell_barba_done": True}

    # --------------------------------------------------------
    # Se manca servizio e ci sono più servizi -> chiedi servizio
    # (MA se l'utente ha già dato data/ora, possiamo intanto proporre slot indicativi)
    # --------------------------------------------------------
    if not chosen_service and len(services) > 1:
        # se ha dato una preferenza temporale, proponiamo 5 slot con durata default 30
        default_dur = 30
        base_date = date_ or now_local(tz).date()
        slots_hint = find_slots(
            shop, hours_map, default_dur,
            preferred_date=base_date,
            exact_time=exact_time,
            after=after,
            before=before,
            limit=5,
            max_days=min(MAX_LOOKAHEAD_DAYS, 14)
        )
        if slots_hint:
            save_session(shop_id, customer_phone, "need_service", {
                "pending": {
                    "date": date_.isoformat() if date_ else None,
                    "time": exact_time.isoformat() if exact_time else None,
                    "after": after.isoformat() if after else None,
                    "before": before.isoformat() if before else None,
                    "flags": flags,
                },
                "options_hint": [x.isoformat() for x in slots_hint]
            })
            return (
                f"Perfetto 👍 Ho trovato queste disponibilità:\n" +
                "\n".join([f"{i+1}) {format_slot(tz, dt.datetime.fromisoformat(slots_hint[i].isoformat()))}" for i in range(len(slots_hint))]) +
                "\n\nDimmi anche che servizio desideri così confermiamo 👌"
            )

        save_session(shop_id, customer_phone, "need_service", {
            "pending": {
                "date": date_.isoformat() if date_ else None,
                "time": exact_time.isoformat() if exact_time else None,
                "after": after.isoformat() if after else None,
                "before": before.isoformat() if before else None,
                "flags": flags,
            }
        })
        return (
            f"Perfetto 😊 Per che servizio vuoi prenotare da *{shop_name}*?\n"
            f"{services_bullets(services, 10)}\n\n"
            f"Puoi scrivere ad esempio: “{services[0].get('name','Taglio')}”."
        )

    if state == "need_service":
        svc = pick_service_from_text(services, t_raw)
        if not svc:
            return (
                "Ok 😊 dimmi il servizio desiderato (anche scritto in modo semplice):\n"
                f"{services_bullets(services, 10)}"
            )
        chosen_service = svc
        pending = data.get("pending") or {}
        # ripristina preferenze temporali già date
        if not date_ and pending.get("date"):
            try:
                date_ = dt.date.fromisoformat(pending["date"])
            except Exception:
                pass
        if not exact_time and pending.get("time"):
            try:
                exact_time = dt.time.fromisoformat(pending["time"])
            except Exception:
                pass
        if not after and pending.get("after"):
            try:
                after = dt.time.fromisoformat(pending["after"])
            except Exception:
                pass
        if not before and pending.get("before"):
            try:
                before = dt.time.fromisoformat(pending["before"])
            except Exception:
                pass
        # continua sotto

    # --------------------------------------------------------
    # Se manca quando -> chiedi
    # --------------------------------------------------------
    if chosen_service and not date_ and not exact_time and not after and not before:
        save_session(shop_id, customer_phone, "need_when", {"service": chosen_service})
        return "Quando preferisci venire? (es. “domani alle 18”, “sabato mattina”, “questa settimana dopo le 18”)."

    # solo data
    if chosen_service and date_ and not exact_time and not after and not before:
        save_session(shop_id, customer_phone, "need_time", {"service": chosen_service, "date": date_.isoformat()})
        return f"Ok 👍 {date_.strftime('%d/%m')} a che ora preferisci? (es. 18:00) oppure una fascia (es. “dopo le 18”)."

    # solo orario/fascia
    if chosen_service and (exact_time or after or before) and not date_:
        payload = {"service": chosen_service}
        if exact_time:
            payload["time"] = exact_time.isoformat()
        if after:
            payload["after"] = after.isoformat()
        if before:
            payload["before"] = before.isoformat()
        save_session(shop_id, customer_phone, "need_date", payload)
        return "Perfetto 👍 Per che giorno? (es. “domani”, “sabato”, “20/12”)."

    # need_date / need_time
    if state == "need_date":
        d = parse_date(t_raw, tz)
        if not d:
            return "Ok 😊 Dimmi il giorno (es. “domani”, “sabato”, “20/12”)."
        date_ = d
        if data.get("time") and not exact_time:
            try:
                exact_time = dt.time.fromisoformat(data["time"])
            except Exception:
                pass
        if data.get("after") and not after:
            try:
                after = dt.time.fromisoformat(data["after"])
            except Exception:
                pass
        if data.get("before") and not before:
            try:
                before = dt.time.fromisoformat(data["before"])
            except Exception:
                pass

    if state == "need_time":
        if data.get("date"):
            try:
                date_ = dt.date.fromisoformat(data["date"])
            except Exception:
                pass
        if not exact_time:
            exact_time = parse_time(t_raw)
        a2, b2 = parse_window(t_raw)
        after = after or a2
        before = before or b2
        if not exact_time and not after and not before:
            return "Dimmi un orario valido (es. 18:00) oppure una fascia (es. “dopo le 18”)."

    # fallback servizio
    if not chosen_service:
        chosen_service = services[0] if services else {"name": "Appuntamento", "duration": "30"}

    dur = service_duration(chosen_service)

    # --------------------------------------------------------
    # BOOKING: data + ora precisa
    # --------------------------------------------------------
    if date_ and exact_time:
        # 1) prova esatto
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=date_,
            exact_time=exact_time,
            after=None, before=None,
            limit=5,
            max_days=min(MAX_LOOKAHEAD_DAYS, 14)
        )
        if slots and slots[0].date() == date_ and slots[0].time() == exact_time:
            start = slots[0]
            save_session(shop_id, customer_phone, "confirm", {"service": chosen_service, "slot_iso": start.isoformat()})
            # copy più umano: spiega il giorno
            return (
                f"Perfetto 👍 Confermi?\n"
                f"💈 *{chosen_service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"Rispondi *OK* per confermare oppure *annulla*."
            )

        # 2) non disponibile -> alternative “furbe”
        alt = pick_better_alternatives(shop, hours_map, dur, date_, exact_time, after=None, before=None, limit=5)
        if not alt:
            return "Non vedo disponibilità in quel momento 😕 Vuoi indicarmi un’altra fascia (es. “dopo le 18”) o un altro giorno?"

        # spiega perché
        explanation = (
            f"Domani alle {exact_time.strftime('%H:%M')} purtroppo è già occupato 😕\n"
            f"Posso però offrirti questi orari vicini / alternativi:"
            if "domani" in norm_text(t_raw) else
            f"A quell’ora non riesco 😅 Posso proporti questi orari vicini / alternativi:"
        )

        save_session(shop_id, customer_phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in alt]})
        return render_slots(shop, explanation, alt)

    # --------------------------------------------------------
    # BOOKING: data + fascia
    # --------------------------------------------------------
    if date_ and (after or before) and not exact_time:
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=date_,
            exact_time=None,
            after=after, before=before,
            limit=5,
            max_days=min(MAX_LOOKAHEAD_DAYS, 14)
        )
        if not slots:
            # rete di sicurezza: prima disponibilità generale
            fallback = find_slots(shop, hours_map, dur, preferred_date=date_, exact_time=None, after=None, before=None, limit=5, max_days=7)
            if not fallback:
                return "In quella fascia non vedo posti liberi 😕 Vuoi provare un altro giorno?"
            save_session(shop_id, customer_phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in fallback]})
            return render_slots(shop, "In quella fascia non vedo posti 😕 Ti propongo la prima disponibilità utile:", fallback)

        save_session(shop_id, customer_phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in slots]})
        return render_slots(shop, "Perfetto 👍 Ecco alcune disponibilità:", slots)

    # --------------------------------------------------------
    # BOOKING: richiesta generica / “questa settimana dopo le 18”
    # --------------------------------------------------------
    base_date = date_ or now_local(tz).date()

    # se this_week: restringi max_days fino a domenica
    max_days = min(MAX_LOOKAHEAD_DAYS, 14)
    if flags.get("this_week"):
        today = now_local(tz).date()
        # giorni fino a domenica
        max_days = min(max_days, (6 - today.weekday()))

    slots = find_slots(
        shop, hours_map, dur,
        preferred_date=base_date,
        exact_time=None,
        after=after,
        before=before,
        limit=5,
        max_days=max_days
    )

    if not slots:
        # rete di sicurezza: prima disponibilità assoluta
        fallback = find_slots(shop, hours_map, dur, preferred_date=now_local(tz).date(), exact_time=None, after=None, before=None, limit=5, max_days=7)
        if not fallback:
            return "Non vedo disponibilità a breve 😕 Dimmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)."
        save_session(shop_id, customer_phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in fallback]})
        return render_slots(shop, "Va bene 👍 allora ti propongo la prima disponibilità che va bene a entrambi:", fallback)

    save_session(shop_id, customer_phone, "choose", {"service": chosen_service, "options": [x.isoformat() for x in slots]})
    return render_slots(shop, "Ecco i prossimi orari liberi:", slots)


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "RispondiTu attivo ✅"


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/test", methods=["GET"])
def test():
    """
    TEST consigliato (multi-cliente):
      /test?phone=<numero_salone>&customer=<numero_cliente>&msg=ciao
      /test?phone=<numero_salone>&customer=<numero_cliente>&msg=domani%20alle%2018

    Backward-compat:
      se customer non c'è, usa phone anche come customer (non ideale).
    """
    shop_number = request.args.get("phone", "")
    customer_phone = request.args.get("customer", "") or shop_number
    msg = request.args.get("msg", "ciao")

    try:
        shop = load_shop_by_phone(shop_number)
        if not shop:
            return jsonify({"error": "shop non trovato", "phone": shop_number}), 404

        reply = handle_message(shop, shop_number=shop_number, customer_phone=customer_phone, text=msg)
        return jsonify({
            "shop": shop.get("name"),
            "shop_number": norm_phone(shop_number),
            "customer": norm_phone(customer_phone),
            "message_in": msg,
            "bot_reply": reply
        })
    except HttpError as e:
        return jsonify({"error": "google_api_error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "server_error", "details": str(e)}), 500


# Placeholder WhatsApp Cloud API (lo attiviamo quando Meta sblocca)
@app.route("/wa", methods=["POST"])
def wa_placeholder():
    return jsonify({"ok": True, "note": "Webhook WhatsApp Cloud API non configurato in questa fase."})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
