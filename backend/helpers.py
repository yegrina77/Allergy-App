"""Shared helpers: Redis-backed storage, auth, and allergen/diet/costing
computation. No Flask routes live here — every blueprint imports from this
module instead of duplicating logic."""
import os
import json
import secrets
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import jsonify, session
from upstash_redis import Redis

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

redis = Redis.from_env()  # UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN 환경변수 사용
UPSTASH_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

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


def _raw_pipeline(commands):
    """Send many Redis commands in ONE HTTP request via Upstash's pipeline REST
    endpoint, instead of one request per command. Used for bulk operations
    so they don't take many seconds of round trips."""
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


def _ingredient_public(ing):
    d = dict(ing)
    d["has_photo"] = bool(d.get("photo_base64"))
    d.pop("photo_base64", None)  # 목록 조회에는 사진 원본을 안 실어서 응답을 가볍게 유지
    return d


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
# Allergen / diet / costing computation shared by sub-recipes and menu items
# ------------------------------------------------------------------
UNIT_INFO = {
    "g": ("weight", 1), "kg": ("weight", 1000),
    "ml": ("volume", 1), "l": ("volume", 1000),
    "each": ("count", 1),
}

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


def compute_may_contain(ingredient_records):
    """Precautionary ('may contain' / 'trace of') allergens, kept separate from
    compute_allergens() on purpose: these are cross-contact warnings, not
    confirmed ingredients, so they must never feed into compute_diet_flags()
    or any safe/unsafe verdict — only shown to staff as extra context."""
    merged = {}
    contributors = {}
    for ing in ingredient_records:
        for a in ing.get("may_contain", []):
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
        d["mayContain"] = compute_may_contain(records)
        d["dietFlags"] = compute_diet_flags(allergens, records)
        d["vegFlags"] = compute_veg_flags(records)
        d["costing"] = compute_menu_costing(m, ingredient_by_id, subrecipe_costing_by_id)
        out.append(d)
    return out
