import os
import json
import secrets
import base64
from io import BytesIO
from datetime import datetime, timezone
from functools import wraps

import requests
import openpyxl
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
UPSTASH_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")


def _raw_pipeline(commands):
    """Send many Redis commands in ONE HTTP request via Upstash's pipeline REST
    endpoint, instead of one request per command. Used for bulk operations
    (import, bulk delete) so they don't take many seconds of round trips."""
    if not commands:
        return []
    resp = requests.post(
        f"{UPSTASH_REST_URL}/pipeline",
        headers={"Authorization": f"Bearer {UPSTASH_REST_TOKEN}", "Content-Type": "application/json"},
        json=commands,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _raw_mget(keys):
    """Batch-fetch many keys in a single pipeline request. Returns {key: parsed_value_or_None}."""
    if not keys:
        return {}
    results = _raw_pipeline([["GET", k] for k in keys])
    out = {}
    for k, r in zip(keys, results):
        val = r.get("result") if isinstance(r, dict) else None
        out[k] = json.loads(val) if val is not None else None
    return out

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


def load_ingredients_many(ids):
    """Batch-fetch several ingredients in one request, preserving order and dropping missing ones."""
    if not ids:
        return []
    data = _raw_mget([f"allergy_ingredient:{i}" for i in ids])
    return [data[f"allergy_ingredient:{i}"] for i in ids if data.get(f"allergy_ingredient:{i}") is not None]


def list_menu_ids(company_id):
    return _raw_get(f"allergy_menu_ids:{company_id}", [])


def load_menu(menu_id):
    return _raw_get(f"allergy_menu:{menu_id}", None)


def load_menus_many(ids):
    if not ids:
        return []
    data = _raw_mget([f"allergy_menu:{i}" for i in ids])
    return [data[f"allergy_menu:{i}"] for i in ids if data.get(f"allergy_menu:{i}") is not None]


def list_subrecipe_ids(company_id):
    return _raw_get(f"allergy_subrecipe_ids:{company_id}", [])


def load_subrecipe(sr_id):
    return _raw_get(f"allergy_subrecipe:{sr_id}", None)


def load_subrecipes_many(ids):
    if not ids:
        return []
    data = _raw_mget([f"allergy_subrecipe:{i}" for i in ids])
    return [data[f"allergy_subrecipe:{i}"] for i in ids if data.get(f"allergy_subrecipe:{i}") is not None]


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


@app.route("/api/invoices/analyze", methods=["POST"])
@login_required()
def analyze_invoice(user):
    """Read a supplier invoice photo and compare its line-item prices against
    currently registered ingredient prices (matched by product code). Does not
    save anything — the admin reviews and applies changes explicitly."""
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on the server."}), 500

    data = request.get_json(force=True)
    image_b64 = data.get("imageBase64")
    if not image_b64:
        return jsonify({"error": "No image provided."}), 400

    system_prompt = """You are reading a photo of a restaurant supplier invoice.

First, read the supplier/issuing company's name as printed on the letterhead or header of the invoice (not the buyer's name) — put this in "supplierName" (null if you cannot find one).

Then extract every line item you can read: its product code (SKU/item code as printed), the product name as printed, and the price shown for that line (state whether it is a unit price or a line/extended total in "priceType"). If a field is unreadable or not shown, use null for that field. Never invent values that are not visible in the photo.

Respond ONLY in this JSON format, no markdown, no explanation:
{
  "supplierName": "issuing company name as printed, or null",
  "items": [
    {"productCode": "code as printed or null", "productName": "name as printed", "price": number or null, "priceType": "unit" | "line" | "unknown"}
  ]
}"""

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
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": "Read this invoice photo and extract every line item in the specified JSON format."},
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

    # Match each extracted line against registered ingredients by product code
    detected_supplier = (parsed.get("supplierName") or "").strip()

    def names_roughly_match(a, b):
        a, b = (a or "").strip().lower(), (b or "").strip().lower()
        if not a or not b:
            return True  # nothing to compare against, don't flag a mismatch
        return a in b or b in a

    by_code = {}
    for i in list_ingredient_ids(user["company_id"]):
        ing = load_ingredient(i)
        if ing and ing.get("product_code"):
            by_code[ing["product_code"]] = ing

    results = []
    for item in parsed.get("items", []):
        code = item.get("productCode")
        match = by_code.get(code) if code else None
        invoice_price = item.get("price")
        current_price = match.get("price") if match else None
        price_changed = bool(
            match and invoice_price is not None and current_price is not None
            and abs(float(current_price) - float(invoice_price)) > 0.001
        )
        registered_supplier_name = match.get("supplier_name") if match else None
        supplier_mismatch = bool(
            match and registered_supplier_name and detected_supplier
            and not names_roughly_match(registered_supplier_name, detected_supplier)
        )
        results.append({
            "productCode": code,
            "invoiceName": item.get("productName"),
            "invoicePrice": invoice_price,
            "priceType": item.get("priceType"),
            "matchedIngredientId": match["id"] if match else None,
            "matchedName": match["name"] if match else None,
            "currentPrice": current_price,
            "priceChanged": price_changed,
            "registeredSupplierName": registered_supplier_name,
            "supplierMismatch": supplier_mismatch,
        })

    return jsonify({"detectedSupplierName": detected_supplier or None, "items": results})



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


@app.route("/api/ingredients/bulk-import", methods=["POST"])
@login_required()
def bulk_import_ingredients(user):
    """Import a supplier's product list (code, name, price) from a spreadsheet.
    Every row is created WITHOUT a photo or allergens — flagged unverified — so
    diet/allergen claims never assume something that hasn't actually been checked."""
    data = request.get_json(force=True)
    file_b64 = data.get("fileBase64")
    supplier_name = (data.get("supplierName") or "").strip()
    if not file_b64 or not supplier_name:
        return jsonify({"error": "A spreadsheet file and a supplier name are both required."}), 400

    try:
        file_bytes = base64.b64decode(file_b64)
        wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        return jsonify({"error": f"Could not read the spreadsheet: {e}"}), 400

    rows = [r for r in rows if r and any(c is not None for c in r)]
    if not rows:
        return jsonify({"error": "The spreadsheet appears to be empty."}), 400

    # Detect code/name/price columns from the header row; fall back to column order.
    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    PRICE_KEYWORDS = ("price", "cost", "rate", "amount")
    CODE_KEYWORDS = ("code", "sku", "item no", "product no", "part no", "no.", " no")
    NAME_KEYWORDS = ("description", "name", "product")

    code_col = price_col = name_col = None
    for idx, h in enumerate(header):
        if any(k in h for k in PRICE_KEYWORDS):
            price_col = idx
            break
    for idx, h in enumerate(header):
        if idx == price_col:
            continue
        if any(k in h for k in CODE_KEYWORDS) or h == "no":
            code_col = idx
            break
    for idx, h in enumerate(header):
        if idx in (code_col, price_col):
            continue
        if any(k in h for k in NAME_KEYWORDS):
            name_col = idx
            break
    if name_col is None:
        for idx, h in enumerate(header):
            if idx not in (code_col, price_col) and h:
                name_col = idx
                break
    if code_col is None and name_col is None and price_col is None:
        code_col, name_col, price_col = 0, 1, 2
    data_rows = rows[1:]

    # Find or create the supplier
    supplier_id = None
    for i in list_supplier_ids(user["company_id"]):
        s = load_supplier(i)
        if s and s["name"].strip().lower() == supplier_name.lower():
            supplier_id = s["id"]
            supplier_name = s["name"]  # use the existing casing
            break
    if not supplier_id:
        supplier_id = new_id()
        _raw_set(f"allergy_supplier:{supplier_id}", {
            "id": supplier_id, "company_id": user["company_id"], "name": supplier_name, "created_at": now_iso(),
        })
        sids = list_supplier_ids(user["company_id"])
        sids.append(supplier_id)
        _raw_set(f"allergy_supplier_ids:{user['company_id']}", sids)

    existing_codes = {}
    ing_ids = list_ingredient_ids(user["company_id"])
    for i in ing_ids:
        ing = load_ingredient(i)
        if ing and ing.get("product_code"):
            existing_codes[ing["product_code"]] = ing["name"]

    created, skipped = [], []
    write_commands = []
    for r in data_rows:
        code = str(r[code_col]).strip() if code_col is not None and code_col < len(r) and r[code_col] is not None else None
        name = str(r[name_col]).strip() if name_col is not None and name_col < len(r) and r[name_col] is not None else None
        price = None
        if price_col is not None and price_col < len(r) and r[price_col] is not None:
            try:
                price = float(r[price_col])
            except (ValueError, TypeError):
                price = None
        if not name:
            continue
        if code and code in existing_codes:
            skipped.append({"code": code, "name": name, "reason": f"Product code already registered as \"{existing_codes[code]}\""})
            continue

        ing_id = new_id()
        ingredient = {
            "id": ing_id, "company_id": user["company_id"], "name": name,
            "raw_text": "", "allergens": [], "overall_confidence": "",
            "notes": "Imported from spreadsheet — no label photo yet, allergens not verified.",
            "supplier_id": supplier_id, "supplier_name": supplier_name,
            "product_code": code, "price": price, "diet_category": None,
            "photo_base64": None, "last_verified_at": None,
            "created_by_id": user["id"], "created_by_name": user["name"],
            "updated_by_id": user["id"], "updated_by_name": user["name"],
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        write_commands.append(["SET", f"allergy_ingredient:{ing_id}", json.dumps(ingredient)])
        ing_ids.append(ing_id)
        if code:
            existing_codes[code] = name
        created.append({"code": code, "name": name, "price": price})

    write_commands.append(["SET", f"allergy_ingredient_ids:{user['company_id']}", json.dumps(ing_ids)])
    _raw_pipeline(write_commands)
    if created:
        log_action(user["company_id"], user, "created", "bulk_import", f"{len(created)} ingredients from {supplier_name}")

    return jsonify({
        "supplierName": supplier_name,
        "createdCount": len(created), "skippedCount": len(skipped),
        "created": created, "skipped": skipped,
    })


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
    ingredients = load_ingredients_many(ids)
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
        "unit": data.get("unit") or None,
        "package_qty": data.get("packageQty"),
        "is_free": bool(data.get("isFree")),
        "diet_category": data.get("dietCategory") or None,
        "photo_base64": data.get("photoBase64"),
        "last_verified_at": now_iso() if data.get("photoBase64") else None,
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
        "unit": data.get("unit", existing.get("unit")) or None,
        "package_qty": data.get("packageQty", existing.get("package_qty")),
        "is_free": bool(data.get("isFree", existing.get("is_free", False))),
        "diet_category": data.get("dietCategory", existing.get("diet_category")) or None,
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "updated_at": now_iso(),
    })
    if data.get("photoBase64"):
        existing["photo_base64"] = data.get("photoBase64")
        existing["last_verified_at"] = now_iso()  # a new label photo = re-verified now
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


@app.route("/api/ingredients/bulk-delete", methods=["POST"])
@login_required()
def bulk_delete_ingredients(user):
    data = request.get_json(force=True)
    ids_to_delete = set(data.get("ids", []))
    if not ids_to_delete:
        return jsonify({"error": "No ingredients selected."}), 400

    ing_ids = list_ingredient_ids(user["company_id"])
    ids_present = [i for i in ing_ids if i in ids_to_delete]
    remaining_ids = [i for i in ing_ids if i not in ids_to_delete]

    commands = [["DEL", f"allergy_ingredient:{i}"] for i in ids_present]
    commands.append(["SET", f"allergy_ingredient_ids:{user['company_id']}", json.dumps(remaining_ids)])
    _raw_pipeline(commands)
    deleted_count = len(ids_present)

    if deleted_count:
        log_action(user["company_id"], user, "deleted", "ingredient", f"{deleted_count} ingredients (bulk delete)")

    return jsonify({"deletedCount": deleted_count})


# ------------------------------------------------------------------
# Allergen / diet computation shared by sub-recipes and menu items
# ------------------------------------------------------------------
UNIT_INFO = {
    "g": ("weight", 1), "kg": ("weight", 1000),
    "ml": ("volume", 1), "l": ("volume", 1000),
    "each": ("count", 1),
}


def compute_usage_cost(ingredient, qty, usage_unit):
    """Cost of using `qty` `usage_unit`s of this ingredient. None means the
    ingredient doesn't have enough costing data yet (unit/package size/price
    missing, or the usage unit's family doesn't match the ingredient's)."""
    if ingredient.get("is_free"):
        return 0.0
    if qty is None:
        return None
    price = ingredient.get("price")
    ing_unit = ingredient.get("unit")
    package_qty = ingredient.get("package_qty")
    if price is None or not ing_unit or not package_qty or not usage_unit:
        return None
    ing_fam, ing_mult = UNIT_INFO.get(ing_unit, (None, 1))
    usage_fam, usage_mult = UNIT_INFO.get(usage_unit, (None, 1))
    if ing_fam is None or usage_fam is None or ing_fam != usage_fam:
        return None
    package_base = package_qty * ing_mult
    if package_base <= 0:
        return None
    return (price / package_base) * (qty * usage_mult)


def extract_usage_ids(item, usage_key, legacy_key):
    """Ingredient/sub-recipe ids referenced by a menu or sub-recipe, supporting
    both the new {id, qty, unit} usage format and older plain id-list records."""
    usages = item.get(usage_key)
    if usages is not None:
        return [u.get("id") for u in usages if u.get("id")]
    return item.get(legacy_key, []) or []


GLUTEN_SOURCES = {"Wheat", "Rye", "Barley", "Oats", "Spelt", "Triticale"}
DAIRY_SOURCES = {"Milk"}
NUT_SOURCES = {"Peanuts", "Almond", "Brazil Nut", "Cashew", "Hazelnut", "Macadamia", "Pecan", "Pine Nut", "Pistachio", "Walnut"}
SHELLFISH_SOURCES = {"Crustacea", "Molluscs"}

DIET_FLAG_DEFS = [
    ("gluten", GLUTEN_SOURCES, "Gluten Free"),
    ("dairy", DAIRY_SOURCES, "Dairy Free"),
    ("nut", NUT_SOURCES, "Nut Free"),
    ("shellfish", SHELLFISH_SOURCES, "Shellfish Free"),
]


def compute_allergens(ingredient_records):
    merged = {}
    contributors = {}
    for ing in ingredient_records:
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


def compute_diet_flags(allergens_list, ingredient_records=None):
    def norm(s):
        return (s or "").strip().lower()
    unverified_records = [r for r in (ingredient_records or []) if not r.get("last_verified_at") and not r.get("is_free")]
    has_unverified = bool(unverified_records)
    unverified_names = sorted({r["name"] for r in unverified_records})

    result = {}
    for key, sources, _label in DIET_FLAG_DEFS:
        norm_sources = {norm(x) for x in sources}
        hits = [a for a in allergens_list if norm(a.get("name")) in norm_sources]
        culprits = sorted({ing for a in hits for ing in a.get("ingredients", [])})
        if hits:
            result[f"{key}Free"] = False  # a confirmed allergen is real regardless of what else is unverified
        elif has_unverified:
            result[f"{key}Free"] = None
            culprits = unverified_names
        else:
            result[f"{key}Free"] = True
        result[f"{key}Culprits"] = culprits
    return result


def compute_veg_flags(ingredient_records):
    has_meat = any(r.get("diet_category") == "meat_fish" for r in ingredient_records)
    has_animal_other = any(r.get("diet_category") == "animal_non_meat" for r in ingredient_records)
    has_unclassified = any(r.get("diet_category") not in ("plant", "animal_non_meat", "meat_fish") for r in ingredient_records)

    veg_culprits = sorted({r["name"] for r in ingredient_records if r.get("diet_category") == "meat_fish"})
    vegan_culprits = sorted({r["name"] for r in ingredient_records if r.get("diet_category") in ("meat_fish", "animal_non_meat")})

    vegetarian = False if has_meat else (None if has_unclassified else True)
    vegan = False if (has_meat or has_animal_other) else (None if has_unclassified else True)

    return {
        "vegetarian": vegetarian, "vegetarianCulprits": veg_culprits,
        "vegan": vegan, "veganCulprits": vegan_culprits,
    }


def compute_subrecipe_costing(sr, ingredient_by_id):
    total_cost = 0.0
    incomplete = False
    missing = []
    for u in sr.get("ingredient_usages") or []:
        ing = ingredient_by_id.get(u.get("id"))
        if not ing:
            incomplete = True
            continue
        cost = compute_usage_cost(ing, u.get("qty"), u.get("unit"))
        if cost is None:
            incomplete = True
            missing.append(ing["name"])
        else:
            total_cost += cost

    yield_qty = sr.get("yield_qty")
    yield_unit = sr.get("yield_unit")
    unit_cost = None
    unit_family = None
    if yield_qty and yield_unit and not incomplete:
        fam, mult = UNIT_INFO.get(yield_unit, (None, 1))
        unit_family = fam
        base_qty = yield_qty * mult
        if base_qty > 0:
            unit_cost = total_cost / base_qty

    return {
        "totalCost": None if incomplete else round(total_cost, 4),
        "unitCost": unit_cost,
        "unitFamily": unit_family,
        "costIncomplete": incomplete or not (yield_qty and yield_unit),
        "missingCostItems": missing,
    }


def compute_menu_costing(m, ingredient_by_id, subrecipe_costing_by_id):
    total_cost = 0.0
    incomplete = False
    missing = []
    for u in m.get("ingredient_usages") or []:
        ing = ingredient_by_id.get(u.get("id"))
        if not ing:
            incomplete = True
            continue
        cost = compute_usage_cost(ing, u.get("qty"), u.get("unit"))
        if cost is None:
            incomplete = True
            missing.append(ing["name"])
        else:
            total_cost += cost

    for u in m.get("subrecipe_usages") or []:
        sc = subrecipe_costing_by_id.get(u.get("id"))
        if not sc or sc.get("unitCost") is None:
            incomplete = True
            continue
        usage_fam, mult = UNIT_INFO.get(u.get("unit"), (None, 1))
        if usage_fam != sc.get("unitFamily") or u.get("qty") is None:
            incomplete = True
            continue
        total_cost += sc["unitCost"] * (u.get("qty") * mult)

    # A menu with no usages recorded at all (e.g. an older menu created before
    # costing existed) has nothing to compute from — flag it rather than show $0.
    if not (m.get("ingredient_usages") or m.get("subrecipe_usages")):
        incomplete = True

    result = {
        "foodCost": None if incomplete else round(total_cost, 2),
        "costIncomplete": incomplete,
        "missingCostItems": missing,
    }
    selling_price = m.get("selling_price")
    if selling_price and result["foodCost"] is not None and selling_price > 0:
        result["foodCostPercent"] = round(result["foodCost"] / selling_price * 100, 1)
    else:
        result["foodCostPercent"] = None
    return result


def build_menu_responses_batch(items):
    """Fetches every sub-recipe and ingredient needed across ALL items in a
    small, fixed number of batched requests, then computes allergens, diet
    flags, and food costing for each menu item."""
    all_sr_ids = set()
    for m in items:
        all_sr_ids.update(extract_usage_ids(m, "subrecipe_usages", "sub_recipe_ids"))
    subrecipe_by_id = {s["id"]: s for s in load_subrecipes_many(list(all_sr_ids))}

    all_ing_ids = set()
    for m in items:
        all_ing_ids.update(extract_usage_ids(m, "ingredient_usages", "ingredient_ids"))
    for sr in subrecipe_by_id.values():
        all_ing_ids.update(extract_usage_ids(sr, "ingredient_usages", "ingredient_ids"))
    ingredient_by_id = {i["id"]: i for i in load_ingredients_many(list(all_ing_ids))}

    def gather_from_cache(item):
        seen = {}
        for i in extract_usage_ids(item, "ingredient_usages", "ingredient_ids"):
            ing = ingredient_by_id.get(i)
            if ing:
                seen[ing["id"]] = ing
        for sid in extract_usage_ids(item, "subrecipe_usages", "sub_recipe_ids"):
            sr = subrecipe_by_id.get(sid)
            if not sr:
                continue
            for i in extract_usage_ids(sr, "ingredient_usages", "ingredient_ids"):
                ing = ingredient_by_id.get(i)
                if ing:
                    seen[ing["id"]] = ing
        return list(seen.values())

    subrecipe_costing_by_id = {
        sr_id: compute_subrecipe_costing(sr, ingredient_by_id) for sr_id, sr in subrecipe_by_id.items()
    }

    out = []
    for m in items:
        d = dict(m)
        records = gather_from_cache(m)
        allergens = compute_allergens(records)
        d["allergens"] = allergens
        d["dietFlags"] = compute_diet_flags(allergens, records)
        d["vegFlags"] = compute_veg_flags(records)
        d["costing"] = compute_menu_costing(m, ingredient_by_id, subrecipe_costing_by_id)
        out.append(d)
    return out


# ------------------------------------------------------------------
# Sub-recipes CRUD — intermediate prep (e.g. "Curry Sauce") made from
# ingredients, reused across one or more menu items.
# ------------------------------------------------------------------
@app.route("/api/sub-recipes", methods=["GET"])
@login_required()
def list_sub_recipes(user):
    ids = list_subrecipe_ids(user["company_id"])
    items = load_subrecipes_many(ids)
    items.sort(key=lambda s: s["name"])

    all_ing_ids = set()
    for s in items:
        all_ing_ids.update(extract_usage_ids(s, "ingredient_usages", "ingredient_ids"))
    ingredient_by_id = {i["id"]: i for i in load_ingredients_many(list(all_ing_ids))}

    out = []
    for s in items:
        d = dict(s)
        ids_for_allergens = extract_usage_ids(s, "ingredient_usages", "ingredient_ids")
        records = [ingredient_by_id[i] for i in ids_for_allergens if i in ingredient_by_id]
        d["allergens"] = compute_allergens(records)
        d["costing"] = compute_subrecipe_costing(s, ingredient_by_id)
        out.append(d)
    return jsonify({"subRecipes": out})


@app.route("/api/sub-recipes", methods=["POST"])
@login_required()
def create_sub_recipe(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Please enter a sub-recipe name."}), 400
    sr_id = new_id()
    sr = {
        "id": sr_id, "company_id": user["company_id"], "name": name,
        "ingredient_usages": data.get("ingredientUsages", []),
        "yield_qty": data.get("yieldQty"),
        "yield_unit": data.get("yieldUnit"),
        "created_by_id": user["id"], "created_by_name": user["name"],
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    _raw_set(f"allergy_subrecipe:{sr_id}", sr)
    ids = list_subrecipe_ids(user["company_id"])
    ids.append(sr_id)
    _raw_set(f"allergy_subrecipe_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "created", "sub_recipe", name)
    return jsonify({"ok": True})


@app.route("/api/sub-recipes/<sr_id>", methods=["PUT"])
@login_required()
def update_sub_recipe(user, sr_id):
    existing = load_subrecipe(sr_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    data = request.get_json(force=True)
    existing.update({
        "name": (data.get("name") or existing["name"]).strip(),
        "ingredient_usages": data.get("ingredientUsages", existing.get("ingredient_usages", [])),
        "yield_qty": data.get("yieldQty", existing.get("yield_qty")),
        "yield_unit": data.get("yieldUnit", existing.get("yield_unit")),
        "updated_by_id": user["id"], "updated_by_name": user["name"],
        "updated_at": now_iso(),
    })
    _raw_set(f"allergy_subrecipe:{sr_id}", existing)
    log_action(user["company_id"], user, "updated", "sub_recipe", existing["name"])
    return jsonify({"ok": True})


@app.route("/api/sub-recipes/<sr_id>", methods=["DELETE"])
@login_required()
def delete_sub_recipe(user, sr_id):
    existing = load_subrecipe(sr_id)
    if not existing or existing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    _raw_delete(f"allergy_subrecipe:{sr_id}")
    ids = [i for i in list_subrecipe_ids(user["company_id"]) if i != sr_id]
    _raw_set(f"allergy_subrecipe_ids:{user['company_id']}", ids)
    log_action(user["company_id"], user, "deleted", "sub_recipe", existing["name"])
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Menu items CRUD (admin/owner) — allergens/diet flags are computed from
# linked ingredients AND linked sub-recipes.
# ------------------------------------------------------------------
@app.route("/api/menu-items", methods=["GET"])
@login_required()
def list_menu_items(user):
    ids = list_menu_ids(user["company_id"])
    items = load_menus_many(ids)
    items.sort(key=lambda m: m["name"])
    return jsonify({"menuItems": build_menu_responses_batch(items)})


@app.route("/api/menu-items", methods=["POST"])
@login_required()
def create_menu_item(user):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Please enter a menu name."}), 400

    menu_id = new_id()
    menu = {
        "id": menu_id, "company_id": user["company_id"], "name": name,
        "ingredient_usages": data.get("ingredientUsages", []),
        "subrecipe_usages": data.get("subrecipeUsages", []),
        "selling_price": data.get("sellingPrice"),
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
        "ingredient_usages": data.get("ingredientUsages", existing.get("ingredient_usages", [])),
        "subrecipe_usages": data.get("subrecipeUsages", existing.get("subrecipe_usages", [])),
        "selling_price": data.get("sellingPrice", existing.get("selling_price")),
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
    items = load_menus_many(ids)
    items.sort(key=lambda m: m["name"])
    built = build_menu_responses_batch(items)
    out = [{"id": d["id"], "name": d["name"], "allergens": d["allergens"],
            "dietFlags": d["dietFlags"], "vegFlags": d["vegFlags"]} for d in built]
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
