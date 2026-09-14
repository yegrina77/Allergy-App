import os
import json
import secrets
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from upstash_redis import Redis

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
)

redis = Redis.from_env()  # UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 환경변수 사용

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
# Redis-backed storage helpers
# ------------------------------------------------------------------
def _raw_get(key, default):
    val = redis.get(key)
    if val is None:
        return default
    return json.loads(val)


def _raw_set(key, value):
    redis.set(key, json.dumps(value))


def _raw_delete(key):
    redis.delete(key)


def new_id():
    return secrets.token_hex(6)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_auth():
    return _raw_get("allergy_auth", {"companies": {}, "users": {}, "slug_index": {}})


def save_auth(auth):
    _raw_set("allergy_auth", auth)


def list_supplier_ids(company_id):
    return _raw_get(f"allergy_supplier_ids:{company_id}", [])


def load_supplier(supplier_id):
    return _raw_get(f"allergy_supplier:{supplier_id}", None)


def list_ingredient_ids(company_id):
    return _raw_get(f"allergy_ingredient_ids:{company_id}", [])


def load_ingredient(ing_id):
    return _raw_get(f"allergy_ingredient:{ing_id}", None)


def list_menu_ids(company_id):
    return _raw_get(f"allergy_menu_ids:{company_id}", [])


def load_menu(menu_id):
    return _raw_get(f"allergy_menu:{menu_id}", None)


def load_audit(company_id):
    return _raw_get(f"allergy_audit:{company_id}", [])


def log_action(company_id, user, action, target_type, target_name, detail=""):
    log = load_audit(company_id)
    log.append({
        "id": new_id(), "user_id": user["id"], "user_name": user["name"],
        "action": action, "target_type": target_type, "target_name": target_name,
        "detail": detail, "created_at": now_iso(),
    })
    log = log[-200:]  # 최근 200건만 유지
    _raw_set(f"allergy_audit:{company_id}", log)


def make_slug(name):
    base = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    return base or "company"


def make_staff_code(length=6):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    auth = load_auth()
    return auth["users"].get(uid)


def login_required(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "Please log in."}), 401
            if role and user["role"] != role:
                return jsonify({"error": "You do not have permission to do this."}), 403
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
        return jsonify({"error": "Please fill in all fields. Password must be at least 8 characters."}), 400

    auth = load_auth()

    slug = make_slug(company_name)
    original_slug = slug
    suffix = 1
    while slug in auth["slug_index"]:
        suffix += 1
        slug = f"{original_slug}-{suffix}"

    for u in auth["users"].values():
        if u["email"] == email:
            return jsonify({"error": "This email is already in use."}), 400

    company_id = new_id()
    staff_code = make_staff_code()
    auth["companies"][company_id] = {
        "id": company_id, "name": company_name, "slug": slug,
        "staff_code": staff_code, "created_at": now_iso(),
    }
    auth["slug_index"][slug] = company_id

    user_id = new_id()
    auth["users"][user_id] = {
        "id": user_id, "company_id": company_id, "name": name, "email": email,
        "password_hash": generate_password_hash(password), "role": "owner", "created_at": now_iso(),
    }
    save_auth(auth)

    session["user_id"] = user_id
    return jsonify({"ok": True, "companySlug": slug, "staffCode": staff_code})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    company_slug = (data.get("companySlug") or "").strip().lower()

    auth = load_auth()
    company_id = auth["slug_index"].get(company_slug)
    if not company_id:
        return jsonify({"error": "Company not found."}), 400

    user = next(
        (u for u in auth["users"].values() if u["company_id"] == company_id and u["email"] == email),
        None,
    )
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password."}), 401

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
    auth = load_auth()
    company = auth["companies"].get(user["company_id"])
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
    auth = load_auth()
    users = [u for u in auth["users"].values() if u["company_id"] == user["company_id"]]
    users.sort(key=lambda u: u["created_at"])
    return jsonify({"users": [{"id": u["id"], "name": u["name"], "email": u["email"],
                                "role": u["role"], "created_at": u["created_at"]} for u in users]})


@app.route("/api/admins", methods=["POST"])
@login_required(role="owner")
def create_admin(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email or len(password) < 8:
        return jsonify({"error": "Please enter name and email. Password must be at least 8 characters."}), 400

    auth = load_auth()
    if any(u["email"] == email for u in auth["users"].values()):
        return jsonify({"error": "This email is already in use."}), 400

    admin_id = new_id()
    auth["users"][admin_id] = {
        "id": admin_id, "company_id": user["company_id"], "name": name, "email": email,
        "password_hash": generate_password_hash(password), "role": "admin", "created_at": now_iso(),
    }
    save_auth(auth)
    log_action(user["company_id"], user, "created", "admin_account", email)
    return jsonify({"ok": True})


@app.route("/api/admins/<admin_id>", methods=["DELETE"])
@login_required(role="owner")
def delete_admin(user, admin_id):
    auth = load_auth()
    target = auth["users"].get(admin_id)
    if not target or target["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    if target["role"] == "owner":
        return jsonify({"error": "The owner account cannot be deleted."}), 400
    del auth["users"][admin_id]
    save_auth(auth)
    log_action(user["company_id"], user, "deleted", "admin_account", target["email"])
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# AI analysis (does not save anything, just returns a suggestion)
# ------------------------------------------------------------------
@app.route("/api/ingredients/analyze", methods=["POST"])
@login_required()
def analyze_ingredient(user):
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on the server."}), 500

    data = request.get_json(force=True)
    image_b64 = data.get("imageBase64")
    if not image_b64:
        return jsonify({"error": "No image provided."}), 400

    system_prompt = f"""You are an assistant analyzing food ingredient label photos for a New Zealand restaurant's allergen management system.

Tasks:
1. Read the ingredient list text from the photo as accurately as possible, exactly as printed. If part of it is unreadable, omit it rather than guessing.
2. Among the ingredients you read, flag any that match the allergens below, from the New Zealand/Australia Food Standards Code (Standard 1.2.3, PEAL). Even if the wording doesn't exactly match the list (e.g. whey, casein -> Milk), flag it if it is derived from that allergen:
{", ".join(ALLERGENS)}
3. If the photo is blurry or only partially visible and you are not confident, set confidence to "low". Never invent content that is not in the photo.

Respond ONLY in the following JSON format. No markdown, no explanation, no code block — pure JSON only:
{{
  "raw_text": "the ingredient list exactly as read from the photo",
  "allergens": [{{"name": "exact English name from the list above", "confidence": "high|medium|low", "source_ingredient": "the original ingredient text that triggered this allergen"}}],
  "overall_confidence": "high|medium|low",
  "notes": "any notes on photo quality or reading difficulty, in English (empty string if none)"
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
                        {"type": "text", "text": "Analyze this ingredient label photo and respond only in the specified JSON format."},
                    ],
                }],
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"AI analysis error ({resp.status_code}): {resp.text[:500]}"}), 502
        content = resp.json()["content"]
        text_block = next(b["text"] for b in content if b["type"] == "text")
        clean = text_block.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
    except Exception as e:
        return jsonify({"error": f"AI analysis error: {e}"}), 502

    return jsonify(parsed)


# ------------------------------------------------------------------
# Suppliers
# ------------------------------------------------------------------
@app.route("/api/suppliers", methods=["GET"])
@login_required()
def list_suppliers(user):
    ids = list_supplier_ids(user["company_id"])
    suppliers = [load_supplier(i) for i in ids]
    suppliers = [s for s in suppliers if s]
    suppliers.sort(key=lambda s: s["name"])
    return jsonify({"suppliers": suppliers})


@app.route("/api/suppliers", methods=["POST"])
@login_required()
def create_supplier(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Please enter a supplier name."}), 400

    ids = list_supplier_ids(user["company_id"])
    for i in ids:
        existing = load_supplier(i)
        if existing and existing["name"] == name:
            return jsonify({"supplier": existing})

    supplier_id = new_id()
    supplier = {"id": supplier_id, "company_id": user["company_id"], "name": name, "created_at": now_iso()}
    _raw_set(f"allergy_supplier:{supplier_id}", supplier)
    ids.append(supplier_id)
    _raw_set(f"allergy_supplier_ids:{user['company_id']}", ids)
    return jsonify({"supplier": supplier})


# ------------------------------------------------------------------
# Ingredients CRUD (admin/owner)
# ------------------------------------------------------------------
def _ingredient_public(ing):
    d = dict(ing)
    d["has_photo"] = bool(d.get("photo_base64"))
    d.pop("photo_base64", None)  # 목록 조회에는 사진 원본을 안 실어서 응답을 가볍게 유지
    return d


@app.route("/api/ingredients", methods=["GET"])
@login_required()
def list_ingredients(user):
    ids = list_ingredient_ids(user["company_id"])
    ingredients = [load_ingredient(i) for i in ids]
    ingredients = [i for i in ingredients if i]
    ingredients.sort(key=lambda i: i["name"])
    return jsonify({"ingredients": [_ingredient_public(i) for i in ingredients]})


@app.route("/api/ingredients/<ing_id>/photo", methods=["GET"])
@login_required()
def get_ingredient_photo(user, ing_id):
    ing = load_ingredient(ing_id)
    if not ing or ing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    return jsonify({"photoBase64": ing.get("photo_base64")})


@app.route("/api/ingredients/lookup", methods=["GET"])
@login_required()
def lookup_ingredient_by_code(user):
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"ingredient": None})
    ids = list_ingredient_ids(user["company_id"])
    for i in ids:
        ing = load_ingredient(i)
        if ing and ing.get("product_code") == code:
            return jsonify({"ingredient": _ingredient_public(ing)})
    return jsonify({"ingredient": None})


@app.route("/api/ingredients", methods=["POST"])
@login_required()
def create_ingredient(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Please enter an ingredient name."}), 400

    product_code = (data.get("productCode") or "").strip() or None
    if product_code:
        for i in list_ingredient_ids(user["company_id"]):
            existing = load_ingredient(i)
            if existing and existing.get("product_code") == product_code:
                return jsonify({"error": f"Product code '{product_code}' is already registered."}), 400

    ing_id = new_id()
    ingredient = {
        "id": ing_id, "company_id": user["company_id"], "name": name,
        "raw_text": data.get("rawText", ""),
        "allergens": data.get("allergens", []),
        "overall_confidence": data.get("overallConfidence", ""),
        "notes": data.get("notes", ""),
        "supplier_id": data.get("supplierId"),
        "supplier_name": None,
        "product_code": product_code,
        "price": data.get("price"),
        "photo_base64": data.get("photoBase64"),
        "created_by_id": user["id"], "created_by_name": user["name"],
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    if ingredient["supplier_id"]:
        sup = load_supplier(ingredient["supplier_id"])
        ingredient["supplier_name"] = sup["name"] if sup else None

    _raw_set(f"allergy_ingredient:{ing_id}", ingredient)
    ids = list_ingredient_ids(user["company_id"])
    ids.append(ing_id)
    _raw_set(f"allergy_ingredient_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "created", "ingredient", name)
    return jsonify({"ok": True})


@app.route("/api/ingredients/<ing_id>", methods=["PUT"])
@login_required()
def update_ingredient(user, ing_id):
    existing = load_ingredient(ing_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404

    data = request.get_json(force=True)
    name = (data.get("name") or existing["name"]).strip()
    product_code = data.get("productCode", existing.get("product_code"))
    if product_code:
        for i in list_ingredient_ids(user["company_id"]):
            if i == ing_id:
                continue
            other = load_ingredient(i)
            if other and other.get("product_code") == product_code:
                return jsonify({"error": f"Product code '{product_code}' is already registered to another ingredient."}), 400

    existing.update({
        "name": name,
        "raw_text": data.get("rawText", existing.get("raw_text")),
        "allergens": data.get("allergens", existing.get("allergens", [])),
        "overall_confidence": data.get("overallConfidence", existing.get("overall_confidence")),
        "notes": data.get("notes", existing.get("notes")),
        "supplier_id": data.get("supplierId", existing.get("supplier_id")),
        "product_code": product_code,
        "price": data.get("price", existing.get("price")),
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "updated_at": now_iso(),
    })
    if data.get("photoBase64"):
        existing["photo_base64"] = data.get("photoBase64")
    if existing.get("supplier_id"):
        sup = load_supplier(existing["supplier_id"])
        existing["supplier_name"] = sup["name"] if sup else None
    else:
        existing["supplier_name"] = None

    _raw_set(f"allergy_ingredient:{ing_id}", existing)
    log_action(user["company_id"], user, "updated", "ingredient", name)
    return jsonify({"ok": True})


@app.route("/api/ingredients/<ing_id>", methods=["DELETE"])
@login_required()
def delete_ingredient(user, ing_id):
    existing = load_ingredient(ing_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    _raw_delete(f"allergy_ingredient:{ing_id}")
    ids = [i for i in list_ingredient_ids(user["company_id"]) if i != ing_id]
    _raw_set(f"allergy_ingredient_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "deleted", "ingredient", existing["name"])
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Menu items CRUD (admin/owner) — allergens are computed from linked ingredients
# ------------------------------------------------------------------
GLUTEN_SOURCES = {"Wheat", "Rye", "Barley", "Oats", "Spelt", "Triticale"}
DAIRY_SOURCES = {"Milk"}


def compute_menu_allergens(ingredient_ids):
    merged = {}
    contributors = {}
    for i in ingredient_ids:
        ing = load_ingredient(i)
        if not ing:
            continue
        for a in ing.get("allergens", []):
            key = a["name"]
            if key not in merged or a.get("confidence") == "high":
                merged[key] = a
            contributors.setdefault(key, set()).add(ing["name"])
    out = []
    for name, a in merged.items():
        d = dict(a)
        d["ingredients"] = sorted(contributors.get(name, []))
        out.append(d)
    return out


def compute_diet_flags(allergens_list):
    def norm(s):
        return (s or "").strip().lower()
    gluten_norm = {norm(x) for x in GLUTEN_SOURCES}
    dairy_norm = {norm(x) for x in DAIRY_SOURCES}
    gluten_hits = [a for a in allergens_list if norm(a.get("name")) in gluten_norm]
    dairy_hits = [a for a in allergens_list if norm(a.get("name")) in dairy_norm]
    gluten_ingredients = sorted({ing for a in gluten_hits for ing in a.get("ingredients", [])})
    dairy_ingredients = sorted({ing for a in dairy_hits for ing in a.get("ingredients", [])})
    return {
        "glutenFree": len(gluten_hits) == 0,
        "glutenCulprits": gluten_ingredients,
        "dairyFree": len(dairy_hits) == 0,
        "dairyCulprits": dairy_ingredients,
    }


@app.route("/api/menu-items", methods=["GET"])
@login_required()
def list_menu_items(user):
    ids = list_menu_ids(user["company_id"])
    items = [load_menu(i) for i in ids]
    items = [m for m in items if m]
    items.sort(key=lambda m: m["name"])
    out = []
    for m in items:
        d = dict(m)
        allergens = compute_menu_allergens(m.get("ingredient_ids", []))
        d["allergens"] = allergens
        d["dietFlags"] = compute_diet_flags(allergens)
        out.append(d)
    return jsonify({"menuItems": out})


@app.route("/api/menu-items", methods=["POST"])
@login_required()
def create_menu_item(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    ingredient_ids = data.get("ingredientIds", [])
    if not name:
        return jsonify({"error": "Please enter a menu name."}), 400

    menu_id = new_id()
    menu = {
        "id": menu_id, "company_id": user["company_id"], "name": name,
        "ingredient_ids": ingredient_ids,
        "created_by_id": user["id"], "created_by_name": user["name"],
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    _raw_set(f"allergy_menu:{menu_id}", menu)
    ids = list_menu_ids(user["company_id"])
    ids.append(menu_id)
    _raw_set(f"allergy_menu_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "created", "menu_item", name)
    return jsonify({"ok": True})


@app.route("/api/menu-items/<item_id>", methods=["PUT"])
@login_required()
def update_menu_item(user, item_id):
    existing = load_menu(item_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404

    data = request.get_json(force=True)
    existing.update({
        "name": (data.get("name") or existing["name"]).strip(),
        "ingredient_ids": data.get("ingredientIds", existing.get("ingredient_ids", [])),
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "updated_at": now_iso(),
    })
    _raw_set(f"allergy_menu:{item_id}", existing)
    log_action(user["company_id"], user, "updated", "menu_item", existing["name"])
    return jsonify({"ok": True})


@app.route("/api/menu-items/<item_id>", methods=["DELETE"])
@login_required()
def delete_menu_item(user, item_id):
    existing = load_menu(item_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    _raw_delete(f"allergy_menu:{item_id}")
    ids = [i for i in list_menu_ids(user["company_id"]) if i != item_id]
    _raw_set(f"allergy_menu_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "deleted", "menu_item", existing["name"])
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Audit log (owner only)
# ------------------------------------------------------------------
@app.route("/api/audit-log", methods=["GET"])
@login_required(role="owner")
def audit_log(user):
    log = load_audit(user["company_id"])
    return jsonify({"log": list(reversed(log))})


# ------------------------------------------------------------------
# Staff-facing public read-only view (no individual login — shared access code)
# ------------------------------------------------------------------
@app.route("/api/staff/<slug>/menu", methods=["GET"])
def staff_menu(slug):
    code = request.args.get("code", "")
    auth = load_auth()
    company_id = auth["slug_index"].get(slug)
    company = auth["companies"].get(company_id) if company_id else None
    if not company or company["staff_code"] != code:
        return jsonify({"error": "Invalid access code."}), 403

    ids = list_menu_ids(company_id)
    items = [load_menu(i) for i in ids]
    items = [m for m in items if m]
    items.sort(key=lambda m: m["name"])
    out = [{"id": m["id"], "name": m["name"],
             "allergens": compute_menu_allergens(m.get("ingredient_ids", [])),
             "dietFlags": compute_diet_flags(compute_menu_allergens(m.get("ingredient_ids", [])))}
           for m in items]
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")
