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


def _dedupe_allergen_list(items):
    """Collapse duplicate allergen entries that share the same name (e.g. the AI
    flagged "Wheat" from two different lines on the same label) into one tag,
    keeping the highest confidence seen and combining the source ingredient
    text so no information is lost."""
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    merged = {}
    order = []
    for a in (items or []):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        if name not in merged:
            merged[name] = dict(a)
            order.append(name)
        else:
            existing = merged[name]
            srcs = {(existing.get("source_ingredient") or "").strip(), (a.get("source_ingredient") or "").strip()}
            srcs.discard("")
            existing["source_ingredient"] = "; ".join(sorted(srcs))
            if conf_rank.get(a.get("confidence"), 0) > conf_rank.get(existing.get("confidence"), 0):
                existing["confidence"] = a.get("confidence")
    return [merged[n] for n in order]


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
3. Separately, look for any precautionary / cross-contact statement on the label — phrasing like "May contain...", "Trace of...", "Made in a facility that also processes...", "May be present:". These are NOT ingredients actually in the product; list any allergens from the list above that they mention as "may_contain", kept completely separate from "allergens" in step 2. If the label has no such statement, return an empty array.
4. Classify the product itself for vegetarian/vegan purposes, based only on what's actually in the ingredient list (ignore "may contain" statements for this):
   - "meat_fish" if it contains any meat, poultry, fish, seafood, or their extracts/derivatives (e.g. "meat extract", "beef stock", "gelatine", "anchovy", "fish sauce", "lard", "chicken fat", "rennet" from animal source).
   - "animal_non_meat" if it contains animal-derived ingredients but no meat/fish/poultry (e.g. milk, cheese, egg, honey, butter).
   - "plant" if everything in the list is plant-derived / has no animal ingredients at all.
   - If you genuinely cannot tell from the text, omit this field rather than guessing.
5. If the photo is blurry or only partially visible and you are not confident, set confidence to "low". Never invent content that is not in the photo.

Respond ONLY in the following JSON format. No markdown, no explanation, no code block — pure JSON only:
{{
  "raw_text": "the ingredient list exactly as read from the photo",
  "allergens": [{{"name": "exact English name from the list above", "confidence": "high|medium|low", "source_ingredient": "the original ingredient text that triggered this allergen"}}],
  "may_contain": [{{"name": "exact English name from the list above", "confidence": "high|medium|low", "source_ingredient": "the exact precautionary statement text that triggered this"}}],
  "suggested_diet_category": "plant|animal_non_meat|meat_fish or omit if unsure",
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

    parsed["allergens"] = _dedupe_allergen_list(parsed.get("allergens"))
    parsed["may_contain"] = _dedupe_allergen_list(parsed.get("may_contain"))
    if parsed.get("suggested_diet_category") not in ("plant", "animal_non_meat", "meat_fish"):
        parsed["suggested_diet_category"] = None

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
        "allergens": _dedupe_allergen_list(data.get("allergens", [])),
        "may_contain": _dedupe_allergen_list(data.get("mayContain", [])),
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
        "allergens": _dedupe_allergen_list(data.get("allergens", existing.get("allergens", []))),
        "may_contain": _dedupe_allergen_list(data.get("mayContain", existing.get("may_contain", []))),
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


# ------------------------------------------------------------------
# One-time fix for companies that ran the OLD (non-toggle) bulk-apply
# button before this flag existed: their prices already have GST
# applied, but the flag doesn't know that yet. Visiting this URL once
# (while logged in, e.g. in a new browser tab) sets the flag to match
# reality WITHOUT touching any prices, so the toggle button shows the
# correct state from then on.
# ------------------------------------------------------------------
@bp.route("/api/ingredients/mark-gst-applied", methods=["GET"])
@login_required()
def mark_gst_applied(user):
    auth = load_auth()
    company = auth["companies"].get(user["company_id"])
    if company is not None:
        company["gst_applied"] = True
        save_auth(auth)
    return jsonify({"ok": True, "gstApplied": True,
                     "note": "Flag set. No prices were changed. You can close this tab."})


# ------------------------------------------------------------------
# One-time cleanup for ingredients saved before duplicate allergen tags
# were de-duplicated on save (e.g. "Wheat" showing twice because the AI
# found it mentioned in two different places on the label). Visiting
# this URL once collapses duplicate names within each ingredient's own
# allergens/may_contain lists, without changing anything else.
# ------------------------------------------------------------------
@bp.route("/api/ingredients/dedupe-allergens", methods=["GET"])
@login_required()
def dedupe_allergens(user):
    ids = list_ingredient_ids(user["company_id"])
    ingredients = load_ingredients_many(ids)

    commands = []
    fixed_count = 0
    for ing in ingredients:
        new_allergens = _dedupe_allergen_list(ing.get("allergens", []))
        new_may_contain = _dedupe_allergen_list(ing.get("may_contain", []))
        if new_allergens != ing.get("allergens", []) or new_may_contain != ing.get("may_contain", []):
            ing["allergens"] = new_allergens
            ing["may_contain"] = new_may_contain
            commands.append(["SET", f"allergy_ingredient:{ing['id']}", json.dumps(ing)])
            fixed_count += 1

    if commands:
        _raw_pipeline(commands)
        log_action(user["company_id"], user, "updated", "ingredient", f"Removed duplicate allergen tags on {fixed_count} ingredients (cleanup)")

    return jsonify({"ok": True, "fixedCount": fixed_count,
                     "note": "Duplicate allergen tags removed. You can close this tab."})


def _classify_diet_category_from_text(raw_text):
    """Text-only classification (no photo re-upload needed) for ingredients that
    already have a stored raw_text from a previous label analysis, so existing
    ingredients can be backfilled with a suggested diet category."""
    prompt = f"""Classify this food product's ingredient list for vegetarian/vegan purposes, based only on what's actually listed (ignore any "may contain" / precautionary statements):
- "meat_fish" if it contains any meat, poultry, fish, seafood, or their extracts/derivatives (e.g. "meat extract", "beef stock", "gelatine", "anchovy", "fish sauce", "lard", "chicken fat", animal-derived "rennet").
- "animal_non_meat" if it contains animal-derived ingredients but no meat/fish/poultry (e.g. milk, cheese, egg, honey, butter).
- "plant" if everything listed is plant-derived / has no animal ingredients at all.
- "unknown" if you genuinely cannot tell from the text.

Ingredient list:
{raw_text}

Respond with ONLY one word: meat_fish, animal_non_meat, plant, or unknown. No other text."""
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
                "max_tokens": 20,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        content = resp.json()["content"]
        text_block = next(b["text"] for b in content if b["type"] == "text").strip().lower()
        # be lenient about trailing punctuation / extra words around the answer
        for candidate in ("animal_non_meat", "meat_fish", "plant"):
            if candidate in text_block:
                return candidate, None
        return None, f"unrecognized:{text_block[:60]}"
    except Exception as e:
        return None, f"error:{e}"


# ------------------------------------------------------------------
# One-time backfill for ingredients registered before diet-category was
# AI-suggested: reuses each ingredient's already-stored raw_text (no
# re-photographing needed) to suggest plant / animal_non_meat / meat_fish
# for any ingredient that doesn't already have a diet category set.
# Ingredients that already have a diet category (manually chosen) are
# left untouched, and ones with no stored raw_text (e.g. free items) are
# skipped since there's nothing to classify from.
# ------------------------------------------------------------------
@bp.route("/api/ingredients/suggest-diet-categories", methods=["GET"])
@login_required()
def suggest_diet_categories(user):
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on the server."}), 500

    ids = list_ingredient_ids(user["company_id"])
    ingredients = load_ingredients_many(ids)

    commands = []
    updated_count = 0
    skipped_no_text = 0
    skipped_unclear = []
    for ing in ingredients:
        if ing.get("diet_category"):
            continue
        raw_text = (ing.get("raw_text") or "").strip()
        if not raw_text:
            skipped_no_text += 1
            continue
        suggestion, reason = _classify_diet_category_from_text(raw_text)
        if suggestion:
            ing["diet_category"] = suggestion
            commands.append(["SET", f"allergy_ingredient:{ing['id']}", json.dumps(ing)])
            updated_count += 1
        else:
            skipped_unclear.append({"name": ing.get("name"), "reason": reason})

    if commands:
        _raw_pipeline(commands)
        log_action(user["company_id"], user, "updated", "ingredient", f"AI-suggested diet category on {updated_count} ingredients (backfill)")

    return jsonify({
        "ok": True, "updatedCount": updated_count,
        "skippedNoText": skipped_no_text,
        "skippedUnclearCount": len(skipped_unclear),
        "skippedUnclearSample": skipped_unclear[:10],
        "note": "Diet categories suggested from existing ingredient text. Please review them in the ingredient list, then you can close this tab.",
    })
