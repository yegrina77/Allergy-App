import json

import requests
from flask import Blueprint, request, jsonify

from helpers import (
    ANTHROPIC_API_KEY, ALLERGENS, login_required, new_id, now_iso,
    list_ingredient_ids, load_ingredient, load_ingredients_many,
    load_supplier, _raw_set, _raw_delete, _raw_pipeline, log_action,
    _ingredient_public, load_auth, save_auth,
)

GST_RATE = 0.15

bp = Blueprint("ingredients", __name__)


# ------------------------------------------------------------------
# AI analysis (does not save anything, just returns a suggestion)
# ------------------------------------------------------------------
@bp.route("/api/ingredients/analyze", methods=["POST"])
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
# Ingredients CRUD (admin/owner)
# ------------------------------------------------------------------
@bp.route("/api/ingredients", methods=["GET"])
@login_required()
def list_ingredients(user):
    ids = list_ingredient_ids(user["company_id"])
    ingredients = load_ingredients_many(ids)
    ingredients.sort(key=lambda i: i["name"])
    return jsonify({"ingredients": [_ingredient_public(i) for i in ingredients]})


@bp.route("/api/ingredients/<ing_id>/photo", methods=["GET"])
@login_required()
def get_ingredient_photo(user, ing_id):
    ing = load_ingredient(ing_id)
    if not ing or ing["company_id"] != user["company_id"]:
        return jsonify({"error": "Not found."}), 404
    return jsonify({"photoBase64": ing.get("photo_base64")})


@bp.route("/api/ingredients/lookup", methods=["GET"])
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


@bp.route("/api/ingredients", methods=["POST"])
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
        "aliases": [a.strip() for a in data.get("aliases", []) if a.strip()],
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


@bp.route("/api/ingredients/<ing_id>", methods=["PUT"])
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
        "aliases": [a.strip() for a in data.get("aliases", existing.get("aliases", [])) if a.strip()],
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


@bp.route("/api/ingredients/<ing_id>", methods=["DELETE"])
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


@bp.route("/api/ingredients/bulk-delete", methods=["POST"])
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
# Bulk GST toggle: multiplies every priced ingredient's price by 1.15,
# or divides back by 1.15 if it was already applied. The company's
# gst_applied flag tracks which state we're in, so the button always
# knows whether to apply or undo, and can't accidentally double-apply.
# ------------------------------------------------------------------
@bp.route("/api/ingredients/bulk-apply-gst", methods=["POST"])
@login_required()
def bulk_apply_gst(user):
    auth = load_auth()
    company = auth["companies"].get(user["company_id"])
    currently_applied = bool(company.get("gst_applied")) if company else False
    new_state = not currently_applied

    ids = list_ingredient_ids(user["company_id"])
    ingredients = load_ingredients_many(ids)

    commands = []
    updated_count = 0
    for ing in ingredients:
        if ing.get("price") is None:
            continue
        if new_state:
            ing["price"] = round(ing["price"] * (1 + GST_RATE), 2)
        else:
            ing["price"] = round(ing["price"] / (1 + GST_RATE), 2)
        commands.append(["SET", f"allergy_ingredient:{ing['id']}", json.dumps(ing)])
        updated_count += 1

    if commands:
        _raw_pipeline(commands)

    if company is not None:
        company["gst_applied"] = new_state
        save_auth(auth)

    if commands:
        action_desc = f"Applied 15% GST to {updated_count} ingredient prices (bulk)" if new_state \
            else f"Reverted 15% GST on {updated_count} ingredient prices (bulk)"
        log_action(user["company_id"], user, "updated", "ingredient", action_desc)

    return jsonify({"updatedCount": updated_count, "gstApplied": new_state})
