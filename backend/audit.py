from flask import Blueprint, jsonify

from helpers import login_required, load_audit

bp = Blueprint("audit", __name__)


@bp.route("/api/audit-log", methods=["GET"])
@login_required(role="owner")
def audit_log(user):
    log = load_audit(user["company_id"])
    return jsonify({"log": list(reversed(log))})
