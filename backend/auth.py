from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash

from helpers import (
    load_auth, save_auth, new_id, now_iso, make_slug, make_staff_code,
    current_user, login_required, log_action,
)

bp = Blueprint("auth", __name__)


@bp.route("/api/signup", methods=["POST"])
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


@bp.route("/api/login", methods=["POST"])
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


@bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/me", methods=["GET"])
def me():
    user = current_user()
    if not user:
        return jsonify({"user": None})
    auth = load_auth()
    company = auth["companies"].get(user["company_id"])
    return jsonify({
        "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]},
        "company": {
            "name": company["name"], "slug": company["slug"], "staffCode": company["staff_code"],
            "gstApplied": bool(company.get("gst_applied")),
        },
    })


# ------------------------------------------------------------------
# Owner: manage admin accounts
# ------------------------------------------------------------------
@bp.route("/api/admins", methods=["GET"])
@login_required()
def list_admins(user):
    auth = load_auth()
    users = [u for u in auth["users"].values() if u["company_id"] == user["company_id"]]
    users.sort(key=lambda u: u["created_at"])
    return jsonify({"users": [{"id": u["id"], "name": u["name"], "email": u["email"],
                                "role": u["role"], "created_at": u["created_at"]} for u in users]})


@bp.route("/api/admins", methods=["POST"])
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


@bp.route("/api/admins/<admin_id>", methods=["DELETE"])
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
