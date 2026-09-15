from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO

import openpyxl
from flask import Blueprint, request, jsonify, send_file

from helpers import login_required, new_id, now_iso, _raw_get, _raw_set

bp = Blueprint("purchases", __name__)


def list_purchase_ids(company_id):
    return _raw_get(f"allergy_purchase_ids:{company_id}", [])


def load_purchase(pid):
    return _raw_get(f"allergy_purchase:{pid}", None)


def _current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ------------------------------------------------------------------
# Log a reviewed invoice as a purchase record. This is a separate, explicit
# step from /api/invoices/analyze — analyze is just a preview/comparison,
# this is what actually gets counted in monthly spend reports.
# ------------------------------------------------------------------
@bp.route("/api/purchases", methods=["POST"])
@login_required()
def create_purchase(user):
    data = request.get_json(force=True)
    date = (data.get("date") or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line_items = data.get("lineItems", [])
    if not line_items:
        return jsonify({"error": "No line items to save."}), 400

    total = 0.0
    clean_items = []
    for li in line_items:
        qty = li.get("quantity")
        unit_price = li.get("unitPrice")
        line_total = li.get("lineTotal")
        if line_total is None and qty is not None and unit_price is not None:
            line_total = round(qty * unit_price, 2)
        if line_total is not None:
            total += line_total
        clean_items.append({
            "productCode": li.get("productCode"),
            "itemName": li.get("itemName") or li.get("invoiceName") or "Unknown item",
            "matchedIngredientId": li.get("matchedIngredientId"),
            "matchedName": li.get("matchedName"),
            "quantity": qty,
            "unitPrice": unit_price,
            "lineTotal": line_total,
        })

    pid = new_id()
    purchase = {
        "id": pid, "company_id": user["company_id"],
        "date": date,
        "supplier_name": (data.get("supplierName") or "").strip() or None,
        "total_amount": round(total, 2),
        "line_items": clean_items,
        "created_by_id": user["id"], "created_by_name": user["name"],
        "created_at": now_iso(),
    }
    _raw_set(f"allergy_purchase:{pid}", purchase)
    ids = list_purchase_ids(user["company_id"])
    ids.append(pid)
    _raw_set(f"allergy_purchase_ids:{user['company_id']}", ids)
    return jsonify({"ok": True, "purchase": purchase})


@bp.route("/api/purchases", methods=["GET"])
@login_required()
def list_purchases(user):
    month = request.args.get("month")  # "YYYY-MM", optional filter
    ids = list_purchase_ids(user["company_id"])
    purchases = [load_purchase(i) for i in ids]
    purchases = [p for p in purchases if p]
    if month:
        purchases = [p for p in purchases if (p.get("date") or "").startswith(month)]
    purchases.sort(key=lambda p: p.get("date") or "", reverse=True)
    return jsonify({"purchases": purchases})


def _build_report(user, month):
    ids = list_purchase_ids(user["company_id"])
    purchases = [load_purchase(i) for i in ids]
    purchases = [p for p in purchases if p and (p.get("date") or "").startswith(month)]

    totals_by_key = defaultdict(lambda: {
        "name": "", "productCode": None, "supplierName": None,
        "totalQuantity": 0.0, "totalSpent": 0.0, "hasQuantity": False,
    })
    grand_total = 0.0
    for p in purchases:
        grand_total += p.get("total_amount") or 0
        for li in p.get("line_items", []):
            key = li.get("matchedIngredientId") or li.get("productCode") or (li.get("itemName") or "").strip().lower()
            entry = totals_by_key[key]
            entry["name"] = li.get("matchedName") or li.get("itemName") or "Unknown item"
            entry["productCode"] = li.get("productCode")
            entry["supplierName"] = p.get("supplier_name")
            if li.get("quantity") is not None:
                entry["totalQuantity"] += li["quantity"]
                entry["hasQuantity"] = True
            if li.get("lineTotal") is not None:
                entry["totalSpent"] += li["lineTotal"]

    items = sorted(totals_by_key.values(), key=lambda x: x["totalSpent"], reverse=True)
    for it in items:
        it["totalQuantity"] = round(it["totalQuantity"], 3) if it["hasQuantity"] else None
        it["totalSpent"] = round(it["totalSpent"], 2)
        del it["hasQuantity"]

    return {
        "month": month,
        "purchaseCount": len(purchases),
        "totalSpent": round(grand_total, 2),
        "items": items,
    }


@bp.route("/api/purchases/report", methods=["GET"])
@login_required()
def purchases_report(user):
    month = request.args.get("month") or _current_month()
    return jsonify(_build_report(user, month))


@bp.route("/api/purchases/report/export", methods=["GET"])
@login_required()
def purchases_report_export(user):
    month = request.args.get("month") or _current_month()
    report = _build_report(user, month)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Purchase Report"
    ws.append([f"Purchase Report — {report['month']}"])
    ws.append([f"Total spent: ${report['totalSpent']:.2f}", f"Purchases logged: {report['purchaseCount']}"])
    ws.append([])
    ws.append(["Item", "Product Code", "Supplier", "Quantity", "Total Spent"])
    for it in report["items"]:
        ws.append([
            it["name"], it["productCode"] or "", it["supplierName"] or "",
            it["totalQuantity"] if it["totalQuantity"] is not None else "",
            it["totalSpent"],
        ])
    for col, width in zip("ABCDE", [32, 16, 18, 12, 14]):
        ws.column_dimensions[col].width = width

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"purchase-report-{report['month']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
