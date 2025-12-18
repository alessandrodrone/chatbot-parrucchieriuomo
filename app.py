from __future__ import annotations
import os, json, re
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =====================
# CONFIG
# =====================
APP = Flask(__name__)

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")  # es: 1BsS-P9rmxmsn11uAIwUjZ2xShdtsdmBssUNeb06p8cU

# =====================
# GOOGLE SHEETS
# =====================
def sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def load_sheet(sheet_name: str):
    svc = sheets_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=sheet_name,
    ).execute()

    values = res.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = values[1:]

    out = []
    for r in rows:
        row = {}
        for i, h in enumerate(headers):
            row[h] = r[i] if i < len(r) else ""
        out.append(row)
    return out

# =====================
# PHONE NORMALIZATION (FONDAMENTALE)
# =====================
def normalize_phone(raw: str) -> str:
    """
    Accetta:
    +393481111111
    whatsapp:+393481111111
    3481111111
    00393481111111

    Ritorna SOLO numeri, senza prefissi
    """
    if not raw:
        return ""

    p = raw.lower()
    p = p.replace("whatsapp:", "")
    p = p.replace("+", "")
    p = p.replace(" ", "")
    p = p.replace("-", "")
    p = p.replace("(", "").replace(")", "")

    # se inizia con 00 (es: 0039)
    if p.startswith("00"):
        p = p[2:]

    return p

# =====================
# SHOP LOOKUP
# =====================
def load_shop(phone_raw: str):
    phone = normalize_phone(phone_raw)

    shops = load_sheet("shops")

    for s in shops:
        sheet_phone = normalize_phone(s.get("whatsapp_number", ""))

        # MATCH PRINCIPALE
        if phone == sheet_phone:
            return s

        # MATCH SENZA 39 (caso italiano)
        if phone.endswith(sheet_phone) or sheet_phone.endswith(phone):
            return s

    return None

# =====================
# TEST ENDPOINT
# =====================
@APP.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "")

    shop = load_shop(phone)

    if not shop:
        return jsonify({
            "error": "shop non trovato",
            "phone_received": phone,
            "phone_normalized": normalize_phone(phone),
        }), 404

    return jsonify({
        "reply": f"Ciao! 💈 Benvenuto da {shop.get('name')}. Dimmi quando vuoi venire.",
        "shop": shop,
        "phone_received": phone,
        "phone_normalized": normalize_phone(phone),
    })

# =====================
# HOME
# =====================
@APP.route("/")
def home():
    return "SaaS Parrucchieri attivo ✅"

if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
