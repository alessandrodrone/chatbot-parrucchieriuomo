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
# ✅ IMPORTANTISSIMO per Railway / gunicorn:
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
        del _CACHE[key)


# ============================================================
# Helpers: phone normalization (robusto)
# ============================================================
def norm_phone(p: str) -> str:
    """
    Normalizza:
      - 'whatsapp:+39348...' -> '39348...'
      - '+39 348...' -> '39348...'
      - '0039...' -> '3939...' (poi ripulito)
      - '348...' -> '348...'
    """
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
    zi = tzinfo_for(shop_tz)
    return dt.datetime.now(zi) if zi else dt.datetime.now()


def parse_date(text: str, shop_tz: str) -> Optional[dt.date]:
    t = (text or "").lower()
    today = now_local(shop_tz).date()

    if "oggi" in t:
        return today
    if "dopodomani" in t:
        return today + dt.timedelta(days=2)
    if "domani" in t:
        return today + dt.timedelta(days=1)
    if "stasera" in t:
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
    t = (text or "").lower().strip()

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

    return None


def parse_window(text: str) -> Tuple[Optional[dt.time], Optional[dt.time]]:
    """
    Estrae:
      - "dopo le 18" -> after
      - "prima delle 17" -> before
      - fasce: mattina/pomeriggio/sera
    """
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
# Sheets helpers
# ============================================================
def _require_sheet_id():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")


def load_tab(tab: str) -> List[Dict[str, str]]:
    """
    Legge tab completo (A:Z) e lo trasforma in list of dict usando header.
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
    return [h.strip() for h in (vals[0] if vals else [])]


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

    headers = [h.strip() for h in values[0]]
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
        end_col = chr(ord("A") + max(0, len(headers) - 1))
        rng = f"{tab}!A{row_idx}:{end_col}{row_idx}"
        sheets().spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=rng,
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
    rows = load_tab("hours")
    out: Dict[int, List[Tuple[dt.time, dt.time]]] = {i: [] for i in range(7)}
    for r in rows:
        if (r.get("shop_id") or "").strip() != (shop_id or "").strip():
            continue
        try:
            wd = int(r.get("weekday", "").strip())
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
# Customers (memoria lunga) + Sessions (memoria breve)
# ============================================================
def get_customer(shop_id: str, phone: str) -> Optional[Dict[str, str]]:
    for r in load_tab("customers"):
        if (r.get("shop_id") or "").strip() == (shop_id or "").strip() and phone_matches(phone, r.get("phone", "")):
            return r
    return None


def upsert_customer(shop_id: str, phone: str, last_service: str):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    prev = get_customer(shop_id, phone)
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
                                and phone_matches(phone, r.get("phone", "")),
        data={
            "shop_id": shop_id,
            "phone": norm_phone(phone),
            "last_service": last_service,
            "total_visits": str(total),
            "last_visit": now_iso,
        },
    )


def get_session(shop_id: str, phone: str) -> Optional[Dict[str, Any]]:
    for r in load_tab("sessions"):
        if (r.get("shop_id") or "").strip() == (shop_id or "").strip() and phone_matches(phone, r.get("phone", "")):
            raw_data = r.get("data", "") or "{}"
            try:
                data_obj = json.loads(raw_data) if isinstance(raw_data, str) else {}
            except Exception:
                data_obj = {}
            return {
                "shop_id": r.get("shop_id", shop_id),
                "phone": r.get("phone", norm_phone(phone)),
                "state": r.get("state", ""),
                "data": data_obj if isinstance(data_obj, dict) else {},
                "updated_at": r.get("updated_at", ""),
            }
    return None


def save_session(shop_id: str, phone: str, state: str, data: Dict[str, Any]):
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    upsert_row(
        "sessions",
        key_predicate=lambda r: (r.get("shop_id") or "").strip() == (shop_id or "").strip()
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
    raw = (sess or {}).get("updated_at") or ""
    if not raw:
        return True
    try:
        ts = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age_min = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60
        return age_min > SESSION_TTL_MINUTES
    except Exception:
        return True


# ============================================================
# Calendar availability + capacity
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


def create_event(cal_id: str, start: dt.datetime, end: dt.datetime, tz: str,
                 summary: str, phone: str, service_name: str, shop_name: str):
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


def in_open_hours(hours_map: Dict[int, List[Tuple[dt.time, dt.time]]],
                 d: dt.date, start_t: dt.time, end_t: dt.time) -> bool:
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

    # 1) data+orario preciso: prova prima quello
    if preferred_date and exact_time:
        start = dt.datetime.combine(preferred_date, exact_time)
        if tzinfo:
            start = start.replace(tzinfo=tzinfo)
        end = start + duration

        if in_open_hours(hours_map, preferred_date, start.time(), end.time()):
            if not (preferred_date == today and start < now_l):
                if slot_has_capacity(cal_id, start, end, capacity):
                    return [start]

    # 2) scan
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


# ============================================================
# Services selection + durations + upsell
# ============================================================
CONFIRM_WORDS = {"ok", "va bene", "confermo", "conferma", "sì", "si", "perfetto", "certo", "ok!", "okay"}
CANCEL_WORDS = {"annulla", "cancella", "stop", "no", "non va bene", "non confermo"}

TAGLIO_ONLY_WORDS = {"solo taglio", "taglio", "taglio uomo", "taglio capelli"}
TAGLIO_E_BARBA_WORDS = {"taglio e barba", "taglio + barba", "taglio barba", "taglio con barba"}


def service_duration(svc: Dict[str, str]) -> int:
    try:
        return int((svc.get("duration") or svc.get("duration_minutes") or "30").strip())
    except Exception:
        return 30


def pick_service_from_text(services: List[Dict[str, str]], text: str) -> Optional[Dict[str, str]]:
    t = (text or "").lower()

    # match per nome completo
    for s in services:
        name = (s.get("name", "") or "").strip()
        if name and name.lower() in t:
            return s

    # keyword: barba
    if "barba" in t:
        for s in services:
            if "barba" in (s.get("name", "").lower()):
                return s

    # keyword: taglio
    if "taglio" in t:
        for s in services:
            if "taglio" in (s.get("name", "").lower()):
                return s

    # keyword: piega / tinta / colore (se unisex/donna)
    if "piega" in t:
        for s in services:
            if "piega" in (s.get("name", "").lower()):
                return s
    if "tinta" in t or "colore" in t:
        for s in services:
            n = (s.get("name", "").lower())
            if "tinta" in n or "colore" in n:
                return s

    return None


def has_service_with_keyword(services: List[Dict[str, str]], kw: str) -> bool:
    kw = kw.lower()
    return any(kw in (s.get("name", "").lower()) for s in services)


def find_combo_taglio_barba(services: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for s in services:
        n = (s.get("name", "").lower())
        if "taglio" in n and "barba" in n:
            return s
    return None


def find_best_taglio(services: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    # preferisci servizi con "taglio" nel nome
    for s in services:
        if "taglio" in (s.get("name", "").lower()):
            return s
    return services[0] if services else None


# ============================================================
# ✅ MERGE STATE: non perdere mai info
# ============================================================
def merge_session_data(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(old or {})
    for k, v in (new or {}).items():
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        merged[k] = v
    return merged


def extract_entities(text: str, tz: str, services: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Estrae entità da testo libero, senza distruggere lo stato.
    """
    t = (text or "").strip()
    tlow = t.lower().strip()

    d = parse_date(t, tz)
    tm = parse_time(t)
    after, before = parse_window(t)

    svc = pick_service_from_text(services, t) if services else None

    # intent speciali taglio/barba scritti "a modo"
    wants_taglio_only = any(w in tlow for w in TAGLIO_ONLY_WORDS)
    wants_taglio_barba = any(w in tlow for w in TAGLIO_E_BARBA_WORDS)

    return {
        "date": d.isoformat() if d else None,
        "time": tm.isoformat() if tm else None,
        "after": after.isoformat() if after else None,
        "before": before.isoformat() if before else None,
        "service": svc,  # dict (se trovato)
        "wants_taglio_only": True if wants_taglio_only else None,
        "wants_taglio_barba": True if wants_taglio_barba else None,
    }


# ============================================================
# Alternative strategy:
# - se (data+ora) occupato: proponi stesso giorno +30/+60 (slot_minutes)
# - e stesso orario nei prossimi giorni
# ============================================================
def alternative_slots_same_day(shop: Dict[str, str], hours_map, dur_min: int, d: dt.date, t: dt.time,
                               steps: int = 4) -> List[dt.datetime]:
    tz = shop.get("timezone", "Europe/Rome")
    tzinfo = tzinfo_for(tz)
    slot_minutes = int(shop.get("slot_minutes", "30") or "30")

    base = dt.datetime.combine(d, t)
    if tzinfo:
        base = base.replace(tzinfo=tzinfo)

    alts: List[dt.datetime] = []
    for i in range(1, steps + 1):
        cand = base + dt.timedelta(minutes=slot_minutes * i)
        end = cand + dt.timedelta(minutes=dur_min)
        if in_open_hours(hours_map, d, cand.time(), end.time()):
            alts.append(cand)

    # filtra per capacity
    capacity = int(shop.get("capacity", "1") or "1")
    cal_id = shop["calendar_id"]
    ok: List[dt.datetime] = []
    for s in alts:
        if slot_has_capacity(cal_id, s, s + dt.timedelta(minutes=dur_min), capacity):
            ok.append(s)
    return ok


def alternative_slots_same_time_next_days(shop: Dict[str, str], hours_map, dur_min: int,
                                         d: dt.date, t: dt.time, days: int = 7) -> List[dt.datetime]:
    tz = shop.get("timezone", "Europe/Rome")
    tzinfo = tzinfo_for(tz)
    capacity = int(shop.get("capacity", "1") or "1")
    cal_id = shop["calendar_id"]

    out: List[dt.datetime] = []
    for i in range(1, days + 1):
        nd = d + dt.timedelta(days=i)
        start = dt.datetime.combine(nd, t)
        if tzinfo:
            start = start.replace(tzinfo=tzinfo)
        end = start + dt.timedelta(minutes=dur_min)
        if not in_open_hours(hours_map, nd, start.time(), end.time()):
            continue
        if slot_has_capacity(cal_id, start, end, capacity):
            out.append(start)
            if len(out) >= 3:
                break
    return out


# ============================================================
# ✅ CORE BOT LOGIC (definitiva)
# ============================================================
def handle_message(shop: Dict[str, str], phone: str, text: str) -> str:
    tz = shop.get("timezone", "Europe/Rome")
    shop_id = (shop.get("shop_id") or "").strip()
    shop_name = shop.get("name", "il salone")
    gender = (shop.get("gender", "unisex") or "unisex").lower()
    capacity = int(shop.get("capacity", "1") or "1")

    services = load_services(shop_id)
    hours_map = load_hours(shop_id)

    t = (text or "").strip()
    tlow = t.lower().strip()

    # session
    sess = get_session(shop_id, phone)
    if sess and session_expired(sess):
        reset_session(shop_id, phone)
        sess = None

    state = (sess.get("state") if sess else "") or ""
    data = (sess.get("data") if sess else {}) or {}
    if not isinstance(data, dict):
        data = {}

    # CANCEL
    if any(w in tlow for w in CANCEL_WORDS):
        reset_session(shop_id, phone)
        return "Ok 👍 Ho annullato. Se vuoi riprenotare dimmi giorno e orario (es. “domani alle 18”)."

    # GREETING
    if tlow in {"ciao", "salve", "buongiorno", "buonasera", "hey"} and not state:
        cust = get_customer(shop_id, phone)
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

    # ✅ Merge entità dal messaggio (NON PERDERE MAI)
    extracted = extract_entities(t, tz, services)
    data = merge_session_data(data, extracted)

    # Se l’utente scrive “solo taglio” senza specificare il servizio preciso, scegli il miglior taglio
    if data.get("wants_taglio_only") and not data.get("service"):
        best = find_best_taglio(services)
        if best:
            data["service"] = best

    # Se dice “taglio e barba”, scegli combo se esiste
    if data.get("wants_taglio_barba"):
        combo = find_combo_taglio_barba(services)
        if combo:
            data["service"] = combo

    # CONFIRM STATE
    if state == "confirm":
        if tlow in CONFIRM_WORDS:
            slot_iso = data.get("slot_iso")
            service = data.get("service")
            if not slot_iso or not isinstance(service, dict):
                reset_session(shop_id, phone)
                return "Ops 😅 ho perso i dettagli. Ripartiamo: che giorno e a che ora vuoi venire?"

            start = dt.datetime.fromisoformat(slot_iso)
            dur = service_duration(service)
            end = start + dt.timedelta(minutes=dur)

            # re-check capacity
            if not slot_has_capacity(shop["calendar_id"], start, end, capacity):
                # alternative
                alt = find_slots(shop, hours_map, dur, start.date(), None, None, None, limit=5, max_days=7)
                if not alt:
                    reset_session(shop_id, phone)
                    return "Quello slot è appena stato preso 😅 Vuoi indicarmi un’altra fascia?"
                save_session(shop_id, phone, "choose", {"service": service, "options": [x.isoformat() for x in alt]})
                return render_slots(shop, "Quell’orario non è più disponibile. Ecco alcune alternative:", alt)

            # create event
            create_event(
                cal_id=shop["calendar_id"],
                start=start,
                end=end,
                tz=tz,
                summary=f"{shop_name} - {service.get('name', 'Servizio')}",
                phone=phone,
                service_name=service.get("name", ""),
                shop_name=shop_name
            )

            upsert_customer(shop_id, phone, service.get("name", ""))
            reset_session(shop_id, phone)

            return (
                f"✅ Prenotazione confermata da *{shop_name}*.\n"
                f"💈 *{service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"A presto 👋"
            )

        # se non è ok/annulla, trattalo come messaggio normale (ma NON perdere lo stato)
        state = ""

    # CHOOSE STATE (sceglie 1/2/3)
    if state == "choose":
        options = data.get("options") or []
        service = data.get("service")

        m = re.search(r"\b(\d{1,2})\b", tlow)
        if m and options:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(options):
                start = dt.datetime.fromisoformat(options[idx])
                save_session(shop_id, phone, "confirm", {
                    **data,
                    "service": service,
                    "slot_iso": start.isoformat(),
                })
                return (
                    f"Confermi questo appuntamento?\n"
                    f"💈 *{(service or {}).get('name','Servizio')}*\n"
                    f"🕒 {format_slot(tz, start)}\n\n"
                    f"Rispondi *OK* per confermare oppure *annulla*."
                )

        # se non ha scelto un numero valido, continua come nuova richiesta senza resettare tutto:
        state = ""

    # --------------------------------------------------------
    # A questo punto: abbiamo data[] con possibili chiavi:
    # date, time, after, before, service (dict), ...
    # --------------------------------------------------------

    # Se manca servizio: chiedilo (ma senza perdere date/time)
    if not data.get("service"):
        if services:
            s_list = "\n".join([f"• {s.get('name')}" for s in services[:10]])
            save_session(shop_id, phone, "need_service", data)
            return (
                f"Perfetto 😊 Per che servizio vuoi prenotare da *{shop_name}*?\n"
                f"{s_list}\n\n"
                f"Puoi scrivere ad esempio: “taglio uomo” oppure “taglio + barba”."
            )
        else:
            # fallback
            data["service"] = {"name": "Appuntamento", "duration": "30"}

    # Se stiamo aspettando il servizio e adesso è arrivato, continuiamo
    if state == "need_service":
        # se ancora non c'è, ripeti
        if not data.get("service"):
            return "Dimmi il servizio desiderato (es. “taglio uomo”, “barba”, “taglio + barba”)."
        state = ""

    service = data.get("service")
    if not isinstance(service, dict):
        # sicurezza
        service = {"name": "Appuntamento", "duration": "30"}
        data["service"] = service

    dur = service_duration(service)

    # Upsell soft barber: se taglio e c’è barba e non ha scelto ancora
    if gender == "uomo":
        chosen_name = (service.get("name", "") or "").lower()
        if ("taglio" in chosen_name) and has_service_with_keyword(services, "barba") and not data.get("upsell_barba_done"):
            # SOLO se l'utente non ha già espresso "solo taglio" o "taglio e barba"
            if not data.get("wants_taglio_only") and not data.get("wants_taglio_barba") and "barba" not in tlow:
                data["upsell_barba_done"] = True
                save_session(shop_id, phone, "upsell_barba", data)
                return (
                    "Perfetto 👍 Vuoi aggiungere anche la *barba* oppure solo *taglio*?\n"
                    "• Scrivi “solo taglio” oppure “taglio e barba”."
                )

    # Se eravamo in upsell e ora ha risposto, lo abbiamo già “mergiato” sopra.
    if state == "upsell_barba":
        state = ""

    # Se manca quando: chiedi (ma non resettare nulla)
    has_date = bool(data.get("date"))
    has_time = bool(data.get("time"))
    has_window = bool(data.get("after") or data.get("before"))

    if not has_date and not has_time and not has_window:
        save_session(shop_id, phone, "need_when", data)
        return "Quando preferisci venire? (es. “domani alle 18”, “venerdì pomeriggio”, “dopo le 19”)."

    # Se ha solo data
    if has_date and not has_time and not has_window:
        save_session(shop_id, phone, "need_time", data)
        d = dt.date.fromisoformat(data["date"])
        return f"Ok 👍 {d.strftime('%d/%m')} a che ora preferisci? (es. 18:00) oppure una fascia (es. “dopo le 18”)."

    # Se ha solo orario/fascia senza data
    if (has_time or has_window) and not has_date:
        save_session(shop_id, phone, "need_date", data)
        return "Perfetto 👍 Per che giorno? (es. “domani”, “venerdì”, “17/12”)."

    # --------------------------------------------------------
    # BOOKING
    # --------------------------------------------------------
    d = dt.date.fromisoformat(data["date"]) if data.get("date") else None
    tm = dt.time.fromisoformat(data["time"]) if data.get("time") else None
    after = dt.time.fromisoformat(data["after"]) if data.get("after") else None
    before = dt.time.fromisoformat(data["before"]) if data.get("before") else None

    # Caso: data + ora precisa
    if d and tm:
        # prova slot preciso
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=d,
            exact_time=tm,
            after=None, before=None,
            limit=5,
            max_days=min(MAX_LOOKAHEAD_DAYS, 30)
        )

        tzinfo = tzinfo_for(tz)
        start_req = dt.datetime.combine(d, tm)
        if tzinfo:
            start_req = start_req.replace(tzinfo=tzinfo)

        # se disponibile esattamente
        if slots and slots[0].date() == d and slots[0].time() == tm:
            start = slots[0]
            save_session(shop_id, phone, "confirm", {**data, "slot_iso": start.isoformat()})
            return (
                f"Perfetto 👍 Confermi?\n"
                f"💈 *{service.get('name','Servizio')}*\n"
                f"🕒 {format_slot(tz, start)}\n\n"
                f"Rispondi *OK* per confermare oppure *annulla*."
            )

        # non disponibile: alternative “vicine” + stesso orario prossimi giorni
        same_day = alternative_slots_same_day(shop, hours_map, dur, d, tm, steps=4)
        next_days = alternative_slots_same_time_next_days(shop, hours_map, dur, d, tm, days=7)

        alts: List[dt.datetime] = []
        alts.extend(same_day)
        alts.extend(next_days)

        # se finder generale aveva proposte, aggiungi le prime (evitando duplicati)
        for s in (slots or []):
            if all(s.isoformat() != x.isoformat() for x in alts):
                alts.append(s)
            if len(alts) >= 5:
                break

        if not alts:
            save_session(shop_id, phone, "need_when", data)
            return "A quell’ora non riesco 😅 Mi dai un’altra fascia (es. “dopo le 18”) o un altro giorno?"

        save_session(shop_id, phone, "choose", {**data, "options": [x.isoformat() for x in alts]})
        return render_slots(shop, "A quell’ora non riesco 😅 Posso proporti questi orari:", alts[:5])

    # Caso: data + fascia
    if d and (after or before) and not tm:
        slots = find_slots(
            shop, hours_map, dur,
            preferred_date=d,
            exact_time=None,
            after=after, before=before,
            limit=5,
            max_days=min(MAX_LOOKAHEAD_DAYS, 30)
        )
        if not slots:
            save_session(shop_id, phone, "need_when", data)
            return "In quella fascia non vedo posti liberi 😕 Vuoi provare un altro orario o un altro giorno?"
        save_session(shop_id, phone, "choose", {**data, "options": [x.isoformat() for x in slots]})
        return render_slots(shop, "Perfetto 👍 Ecco alcune disponibilità:", slots)

    # Caso: generico (data senza ora già gestito sopra; qui resta: data+qualcosa o solo window con data)
    base_date = d or now_local(tz).date()
    slots = find_slots(
        shop, hours_map, dur,
        preferred_date=base_date,
        exact_time=None,
        after=after, before=before,
        limit=5,
        max_days=min(MAX_LOOKAHEAD_DAYS, 30)
    )
    if not slots:
        save_session(shop_id, phone, "need_when", data)
        return "Non vedo disponibilità a breve 😕 Dimmi un giorno preciso o una fascia (es. “mercoledì dopo le 18”)."

    save_session(shop_id, phone, "choose", {**data, "options": [x.isoformat() for x in slots]})
    return render_slots(shop, "Ecco i prossimi orari liberi:", slots)


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"


@app.route("/test", methods=["GET"])
def test():
    """
    Esempi:
      /test?phone=393481111111&msg=ciao
      /test?phone=393481111111&msg=domani%20alle%2018
      /test?phone=393481111111&msg=solo%20taglio
    """
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "ciao")

    try:
        shop = load_shop_by_phone(phone)
        if not shop:
            return jsonify({"error": "shop non trovato", "phone": phone}), 404

        reply = handle_message(shop, phone, msg)
        return jsonify({
            "shop": shop.get("name"),
            "phone": norm_phone(phone),
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
