import os

from flask import Flask, send_from_directory

import auth
import ingredients
import suppliers
import invoices
import subrecipes
import menu
import audit
import staff

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)

app.register_blueprint(auth.bp)
app.register_blueprint(ingredients.bp)
app.register_blueprint(suppliers.bp)
app.register_blueprint(invoices.bp)
app.register_blueprint(subrecipes.bp)
app.register_blueprint(menu.bp)
app.register_blueprint(audit.bp)
app.register_blueprint(staff.bp)


# ------------------------------------------------------------------
# Serve frontend
# ------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/staff/<slug>")
def staff_page(slug):
    return send_from_directory(FRONTEND_DIR, "staff.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND_DIR, path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
