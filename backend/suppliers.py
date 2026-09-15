from flask import Blueprint, request, jsonify

from helpers import login_required, new_id, now_iso, list_supplier_ids, load_supplier, _raw_set

bp = Blueprint("suppliers", __name__)


@bp.route("/api/suppliers", methods=["GET"])
@login_required()
def list_suppliers(user):
    ids = list_supplier_ids(user["company_id"])
    suppliers = [load_supplier(i) for i in ids]
    suppliers = [s for s in suppliers if s]
    suppliers.sort(key=lambda s: s["name"])
    return jsonify({"suppliers": suppliers})


@bp.route("/api/suppliers", methods=["POST"])
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
