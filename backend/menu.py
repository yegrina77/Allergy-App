from flask import Blueprint, request, jsonify

from helpers import (
    login_required, new_id, now_iso,
    list_menu_ids, load_menu, load_menus_many, build_menu_responses_batch,
    _raw_set, _raw_delete, log_action,
)

bp = Blueprint("menu", __name__)


@bp.route("/api/menu-items", methods=["GET"])
@login_required()
def list_menu_items(user):
    ids = list_menu_ids(user["company_id"])
    items = load_menus_many(ids)
    items.sort(key=lambda m: m["name"])
    return jsonify({"menuItems": build_menu_responses_batch(items)})


@bp.route("/api/menu-items", methods=["POST"])
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


@bp.route("/api/menu-items/<item_id>", methods=["PUT"])
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


@bp.route("/api/menu-items/<item_id>", methods=["DELETE"])
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
