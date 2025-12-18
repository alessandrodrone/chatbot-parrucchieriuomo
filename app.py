from __future__ import annotations

import os
import json
import re
import datetime as dt
from flask import Flask, request, jsonify

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ======================
# APP
# ======================
app = Flask(__name__)

# ======================
# ENV
# ======================
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
    raise RuntimeError("Variabili GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_SHEET_ID mancanti")

# ======================
# GOOGLE SHEETS
# ======================
def sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def load_sheet(tab_name: str) -> list[dict]:
    service = sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=tab_name
    ).execute()

    values = result.get("values", [])
    if not values:
        return []

    headers = [h.strip() for h in values[0]]
    rows = []

    for row in values[1:]:
        item = {}
        for i, h in enumerate(headers):
            item[h] = row[i].strip() if i < len(row) else ""
        rows.append(item)

    return rows

# ======================
# UTILS
# ======================
def normalize_phone(p: str) -> str:
    """
    Rimuove tutto tranne numeri.
    +39 348 111111 → 393481111111
    whatsapp:+39348... → 39348...
    """
    if not p:
        return ""
    return re.sub(r"\D", "", p)

# ======================
# SHOP LOOKUP
# ======================
def load_shop(phone: str):
    phone_n = normalize_phone(phone)

    shops = load_sheet("shops")

    print("DEBUG phone ricevuto:", phone)
    print("DEBUG phone normalizzato:", phone_n)
    print("DEBUG shops:", shops)

    for s in shops:
        sheet_phone = normalize_phone(s.get("whatsapp_number", ""))
        print("CONFRONTO:", phone_n, "VS", sheet_phone)

        if phone_n == sheet_phone:
            return s

    return None

# ======================
# LOGICA CHAT (MINIMA)
# ======================
def handle_message(shop: dict, phone: str, msg: str) -> str:
    msg_l = msg.lower()

    if msg_l in {"ciao", "salve", "buongiorno", "buonasera"}:
        return (
            f"Ciao! 👋\n"
            f"Sei in contatto con *{shop['name']}* 💈\n\n"
            f"Dimmi quando vuoi prenotare 😊"
        )

    return "Perfetto 👍 Dimmi giorno e ora (es. domani alle 18)."

# ======================
# ROUTES
# ======================
@app.route("/")
def home():
    return "Chatbot Parrucchieri attivo ✅"

@app.route("/test", methods=["GET"])
def test():
    phone = request.args.get("phone", "")
    msg = request.args.get("msg", "")

    if not phone or not msg:
        return jsonify({"error": "parametri phone e msg richiesti"}), 400

    shop = load_shop(phone)

    if not shop:
        return jsonify({"error": "shop non trovato"}), 404

    reply = handle_message(shop, phone, msg)

    return jsonify({
        "shop": shop["name"],
        "phone": phone,
        "message_in": msg,
        "bot_reply": reply
    })

# ======================
# MAIN
# ======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
