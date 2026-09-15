from flask import Blueprint, request, jsonify

from helpers import load_auth, list_menu_ids, load_menus_many, build_menu_responses_batch

bp = Blueprint("staff", __name__)


@bp.route("/api/staff/<slug>/menu", methods=["GET"])
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
