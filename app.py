from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "Chatbot parrucchiere attivo ✅"

@app.route("/test")
def test():
    phone = request.args.get("phone", "+393000000000")
    msg = request.args.get("msg", "ciao")
    return jsonify({
        "phone": phone,
        "message_in": msg,
        "bot_reply": f"Echo: {msg}"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
