from flask import Blueprint, request, jsonify

from helpers import (
    login_required, new_id, now_iso,
    list_subrecipe_ids, load_subrecipe, load_subrecipes_many, load_ingredients_many,
    extract_usage_ids, compute_allergens, compute_may_contain, compute_subrecipe_costing,
    _raw_set, _raw_delete, log_action,
)

bp = Blueprint("subrecipes", __name__)


@bp.route("/api/sub-recipes", methods=["GET"])
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
        d["mayContain"] = compute_may_contain(records)
        d["costing"] = compute_subrecipe_costing(s, ingredient_by_id)
        out.append(d)
    return jsonify({"subRecipes": out})


@bp.route("/api/sub-recipes", methods=["POST"])
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


@bp.route("/api/sub-recipes/<sr_id>", methods=["PUT"])
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


@bp.route("/api/sub-recipes/<sr_id>", methods=["DELETE"])
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
