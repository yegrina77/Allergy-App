import os
import sqlite3
import base64
import json
import secrets
import string
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "allergy.db"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)

# ---- NZ / AU Food Standards Code (Standard 1.2.3, PEAL) allergen list ----
ALLERGENS = [
    "Wheat", "Rye", "Barley", "Oats", "Spelt", "Triticale",
    "Crustacea", "Molluscs", "Fish", "Egg", "Milk",
    "Peanuts", "Sesame", "Soybeans", "Lupin",
    "Almond", "Brazil Nut", "Cashew", "Hazelnut", "Macadamia",
    "Pecan", "Pine Nut", "Pistachio", "Walnut",
    "Sulphites", "Royal Jelly",
]


# ------------------------------------------------------------------
# DB setup
# ------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            staff_code TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner','admin')),
            created_at TEXT NOT NULL,
            UNIQUE(company_id, email)
        );

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            name TEXT NOT NULL,
            raw_text TEXT,
            allergens_json TEXT NOT NULL DEFAULT '[]',
            overall_confidence TEXT,
            notes TEXT,
            created_by_id INTEGER REFERENCES users(id),
            created_by_name TEXT,
            updated_by_id INTEGER REFERENCES users(id),
            updated_by_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            name TEXT NOT NULL,
            ingredient_ids_json TEXT NOT NULL DEFAULT '[]',
            created_by_id INTEGER REFERENCES users(id),
            created_by_name TEXT,
            updated_by_id INTEGER REFERENCES users(id),
            updated_by_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id),
            user_id INTEGER,
            user_name TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_name TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_action(conn, company_id, user, action, target_type, target_name, detail=""):
    conn.execute(
        "INSERT INTO audit_log (company_id, user_id, user_name, action, target_type, target_name, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (company_id, user["id"], user["name"], action, target_type, target_name, detail, now_iso()),
    )


def make_slug(name):
    base = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    return base or "company"


def make_staff_code(length=6):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def login_required(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "로그인이 필요합니다."}), 401
            if role and user["role"] != role:
                return jsonify({"error": "권한이 없습니다."}), 403
            return fn(user, *args, **kwargs)
        return wrapper
    return decorator


# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------
@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(force=True)
    company_name = (data.get("companyName") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not company_name or not name or not email or len(password) < 8:
        return jsonify({"error": "모든 항목을 입력하고, 비밀번호는 8자 이상이어야 합니다."}), 400

    conn = get_db()
    slug = make_slug(company_name)
    suffix = 1
    original_slug = slug
    while conn.execute("SELECT id FROM companies WHERE slug = ?", (slug,)).fetchone():
        suffix += 1
        slug = f"{original_slug}-{suffix}"

    staff_code = make_staff_code()
    cur = conn.execute(
        "INSERT INTO companies (name, slug, staff_code, created_at) VALUES (?,?,?,?)",
        (company_name, slug, staff_code, now_iso()),
    )
    company_id = cur.lastrowid

    try:
        cur2 = conn.execute(
            "INSERT INTO users (company_id, name, email, password_hash, role, created_at) VALUES (?,?,?,?,?,?)",
            (company_id, name, email, generate_password_hash(password), "owner", now_iso()),
        )
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "이미 사용 중인 이메일입니다."}), 400

    user_id = cur2.lastrowid
    conn.commit()
    conn.close()

    session["user_id"] = user_id
    return jsonify({"ok": True, "companySlug": slug, "staffCode": staff_code})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    company_slug = (data.get("companySlug") or "").strip().lower()

    conn = get_db()
    company = conn.execute("SELECT * FROM companies WHERE slug = ?", (company_slug,)).fetchone()
    if not company:
        conn.close()
        return jsonify({"error": "회사를 찾을 수 없습니다."}), 400

    user = conn.execute(
        "SELECT * FROM users WHERE company_id = ? AND email = ?", (company["id"], email)
    ).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "이메일 또는 비밀번호가 올바르지 않습니다."}), 401

    session["user_id"] = user["id"]
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None})
    conn = get_db()
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (user["company_id"],)).fetchone()
    conn.close()
    return jsonify({
        "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]},
        "company": {"name": company["name"], "slug": company["slug"], "staffCode": company["staff_code"]},
    })


# ------------------------------------------------------------------
# Owner: manage admin accounts
# ------------------------------------------------------------------
@app.route("/api/admins", methods=["GET"])
@login_required()
def list_admins(user):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, email, role, created_at FROM users WHERE company_id = ? ORDER BY created_at",
        (user["company_id"],),
    ).fetchall()
    conn.close()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/admins", methods=["POST"])
@login_required(role="owner")
def create_admin(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or len(password) < 8:
        return jsonify({"error": "이름/이메일을 입력하고, 비밀번호는 8자 이상이어야 합니다."}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (company_id, name, email, password_hash, role, created_at) VALUES (?,?,?,?,?,?)",
            (user["company_id"], name, email, generate_password_hash(password), "admin", now_iso()),
        )
        log_action(conn, user["company_id"], user, "created", "admin_account", email)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "이미 사용 중인 이메일입니다."}), 400
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admins/<int:admin_id>", methods=["DELETE"])
@login_required(role="owner")
def delete_admin(user, admin_id):
    conn = get_db()
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND company_id = ?", (admin_id, user["company_id"])
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "찾을 수 없습니다."}), 404
    if target["role"] == "owner":
        conn.close()
        return jsonify({"error": "오너 계정은 삭제할 수 없습니다."}), 400
    conn.execute("DELETE FROM users WHERE id = ?", (admin_id,))
    log_action(conn, user["company_id"], user, "deleted", "admin_account", target["email"])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# AI analysis (does not save anything, just returns a suggestion)
# ------------------------------------------------------------------
@app.route("/api/ingredients/analyze", methods=["POST"])
@login_required()
def analyze_ingredient(user):
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "서버에 ANTHROPIC_API_KEY가 설정되어 있지 않습니다."}), 500

    data = request.get_json(force=True)
    image_b64 = data.get("imageBase64")
    if not image_b64:
        return jsonify({"error": "이미지가 없습니다."}), 400

    system_prompt = f"""당신은 뉴질랜드 레스토랑의 알러지 관리 시스템을 위해 식품 성분표 사진을 분석하는 어시스턴트입니다.

작업:
1. 사진에서 성분표(ingredient list) 텍스트를 최대한 정확히 그대로 읽어냅니다. 읽을 수 없는 부분은 억지로 추측하지 말고 생략합니다.
2. 읽어낸 성분들 중, 아래 뉴질랜드/호주 식품기준코드(Standard 1.2.3, PEAL) 알러지원 목록에 해당하는 것이 있으면 표시합니다. 성분명이 목록 단어와 정확히 일치하지 않아도(예: whey, casein → Milk), 그 알러지원에서 유래된 성분이면 판단해서 태그하세요:
{", ".join(ALLERGENS)}
3. 사진이 흐릿하거나 일부만 보여서 확신이 낮으면 반드시 confidence를 "low"로 표시하세요. 절대 사진에 없는 내용을 지어내지 마세요.

반드시 아래 JSON 형식으로만 답하세요. 마크다운, 설명, 코드블록 없이 순수 JSON만 출력합니다:
{{
  "raw_text": "사진에서 읽은 성분표 원문",
  "allergens": [{{"name": "목록에 있는 정확한 영문명", "confidence": "high|medium|low", "source_ingredient": "이 알러지원을 유발한 원문 성분 표현"}}],
  "overall_confidence": "high|medium|low",
  "notes": "사진 품질이나 판독상 특이사항 (없으면 빈 문자열)"
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1200,
                "system": system_prompt,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": "이 성분표 사진을 분석해서 지정된 JSON 형식으로만 답해주세요."},
                    ],
                }],
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["content"]
        text_block = next(b["text"] for b in content if b["type"] == "text")
        clean = text_block.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
    except Exception as e:
        return jsonify({"error": f"AI 분석 중 오류: {e}"}), 502

    return jsonify(parsed)


# ------------------------------------------------------------------
# Ingredients CRUD (admin/owner)
# ------------------------------------------------------------------
@app.route("/api/ingredients", methods=["GET"])
@login_required()
def list_ingredients(user):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM ingredients WHERE company_id = ? ORDER BY name", (user["company_id"],)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["allergens"] = json.loads(d.pop("allergens_json"))
        out.append(d)
    return jsonify({"ingredients": out})


@app.route("/api/ingredients", methods=["POST"])
@login_required()
def create_ingredient(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "재료 이름을 입력해주세요."}), 400

    allergens = data.get("allergens", [])
    conn = get_db()
    conn.execute(
        "INSERT INTO ingredients (company_id, name, raw_text, allergens_json, overall_confidence, notes, "
        "created_by_id, created_by_name, updated_by_id, updated_by_name, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["company_id"], name, data.get("rawText", ""), json.dumps(allergens),
         data.get("overallConfidence", ""), data.get("notes", ""),
         user["id"], user["name"], user["id"], user["name"], now_iso(), now_iso()),
    )
    log_action(conn, user["company_id"], user, "created", "ingredient", name)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ingredients/<int:ing_id>", methods=["PUT"])
@login_required()
def update_ingredient(user, ing_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM ingredients WHERE id = ? AND company_id = ?", (ing_id, user["company_id"])
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "찾을 수 없습니다."}), 404

    data = request.get_json(force=True)
    name = (data.get("name") or existing["name"]).strip()
    allergens = data.get("allergens", json.loads(existing["allergens_json"]))

    conn.execute(
        "UPDATE ingredients SET name=?, raw_text=?, allergens_json=?, overall_confidence=?, notes=?, "
        "updated_by_id=?, updated_by_name=?, updated_at=? WHERE id=?",
        (name, data.get("rawText", existing["raw_text"]), json.dumps(allergens),
         data.get("overallConfidence", existing["overall_confidence"]), data.get("notes", existing["notes"]),
         user["id"], user["name"], now_iso(), ing_id),
    )
    log_action(conn, user["company_id"], user, "updated", "ingredient", name)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ingredients/<int:ing_id>", methods=["DELETE"])
@login_required()
def delete_ingredient(user, ing_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM ingredients WHERE id = ? AND company_id = ?", (ing_id, user["company_id"])
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "찾을 수 없습니다."}), 404
    conn.execute("DELETE FROM ingredients WHERE id = ?", (ing_id,))
    log_action(conn, user["company_id"], user, "deleted", "ingredient", existing["name"])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Menu items CRUD (admin/owner) — allergens are computed from linked ingredients
# ------------------------------------------------------------------
def compute_menu_allergens(conn, ingredient_ids):
    if not ingredient_ids:
        return []
    placeholders = ",".join("?" for _ in ingredient_ids)
    rows = conn.execute(
        f"SELECT allergens_json FROM ingredients WHERE id IN ({placeholders})", ingredient_ids
    ).fetchall()
    merged = {}
    for r in rows:
        for a in json.loads(r["allergens_json"]):
            key = a["name"]
            if key not in merged or a.get("confidence") == "high":
                merged[key] = a
    return list(merged.values())


@app.route("/api/menu-items", methods=["GET"])
@login_required()
def list_menu_items(user):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM menu_items WHERE company_id = ? ORDER BY name", (user["company_id"],)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ing_ids = json.loads(d.pop("ingredient_ids_json"))
        d["ingredientIds"] = ing_ids
        d["allergens"] = compute_menu_allergens(conn, ing_ids)
        out.append(d)
    conn.close()
    return jsonify({"menuItems": out})


@app.route("/api/menu-items", methods=["POST"])
@login_required()
def create_menu_item(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    ingredient_ids = data.get("ingredientIds", [])
    if not name:
        return jsonify({"error": "메뉴 이름을 입력해주세요."}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO menu_items (company_id, name, ingredient_ids_json, created_by_id, created_by_name, "
        "updated_by_id, updated_by_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (user["company_id"], name, json.dumps(ingredient_ids), user["id"], user["name"],
         user["id"], user["name"], now_iso(), now_iso()),
    )
    log_action(conn, user["company_id"], user, "created", "menu_item", name)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/menu-items/<int:item_id>", methods=["PUT"])
@login_required()
def update_menu_item(user, item_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM menu_items WHERE id = ? AND company_id = ?", (item_id, user["company_id"])
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "찾을 수 없습니다."}), 404

    data = request.get_json(force=True)
    name = (data.get("name") or existing["name"]).strip()
    ingredient_ids = data.get("ingredientIds", json.loads(existing["ingredient_ids_json"]))

    conn.execute(
        "UPDATE menu_items SET name=?, ingredient_ids_json=?, updated_by_id=?, updated_by_name=?, updated_at=? WHERE id=?",
        (name, json.dumps(ingredient_ids), user["id"], user["name"], now_iso(), item_id),
    )
    log_action(conn, user["company_id"], user, "updated", "menu_item", name)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/menu-items/<int:item_id>", methods=["DELETE"])
@login_required()
def delete_menu_item(user, item_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM menu_items WHERE id = ? AND company_id = ?", (item_id, user["company_id"])
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "찾을 수 없습니다."}), 404
    conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    log_action(conn, user["company_id"], user, "deleted", "menu_item", existing["name"])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Audit log (owner only)
# ------------------------------------------------------------------
@app.route("/api/audit-log", methods=["GET"])
@login_required(role="owner")
def audit_log(user):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE company_id = ? ORDER BY created_at DESC LIMIT 200",
        (user["company_id"],),
    ).fetchall()
    conn.close()
    return jsonify({"log": [dict(r) for r in rows]})


# ------------------------------------------------------------------
# Staff-facing public read-only view (no individual login — shared access code)
# ------------------------------------------------------------------
@app.route("/api/staff/<slug>/menu", methods=["GET"])
def staff_menu(slug):
    code = request.args.get("code", "")
    conn = get_db()
    company = conn.execute("SELECT * FROM companies WHERE slug = ?", (slug,)).fetchone()
    if not company or company["staff_code"] != code:
        conn.close()
        return jsonify({"error": "접근 코드가 올바르지 않습니다."}), 403

    rows = conn.execute(
        "SELECT * FROM menu_items WHERE company_id = ? ORDER BY name", (company["id"],)
    ).fetchall()
    out = []
    for r in rows:
        ing_ids = json.loads(r["ingredient_ids_json"])
        out.append({
            "id": r["id"],
            "name": r["name"],
            "allergens": compute_menu_allergens(conn, ing_ids),
        })
    conn.close()
    return jsonify({"companyName": company["name"], "menuItems": out})


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


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
