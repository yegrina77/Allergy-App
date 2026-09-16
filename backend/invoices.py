import json
import difflib

import requests
from flask import Blueprint, request, jsonify

from helpers import ANTHROPIC_API_KEY, login_required, list_ingredient_ids, load_ingredient

bp = Blueprint("invoices", __name__)


@bp.route("/api/invoices/analyze", methods=["POST"])
@login_required()
def analyze_invoice(user):
    """Read a supplier invoice photo and compare its line-item prices against
    currently registered ingredient prices (matched by product code). Does not
    save anything — the admin reviews and applies changes explicitly."""
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured on the server."}), 500

    data = request.get_json(force=True)
    image_b64 = data.get("imageBase64")
    if not image_b64:
        return jsonify({"error": "No image provided."}), 400

    system_prompt = """You are reading a photo of a restaurant supplier invoice.

First, read the supplier/issuing company's name as printed on the letterhead or header of the invoice (not the buyer's name) — put this in "supplierName" (null if you cannot find one).

Then extract every line item you can read: its product code (SKU/item code as printed), the product name as printed, the quantity ordered (the number of units/cases/bags, as printed — not the weight inside a package), the unit price (price per single unit, if shown), and the line total (the extended/total price for that line, if shown). If a field is unreadable or not shown, use null for that field — do not invent or calculate values that are not visible in the photo, just report what is printed.

Respond ONLY in this JSON format, no markdown, no explanation:
{
  "supplierName": "issuing company name as printed, or null",
  "items": [
    {"productCode": "code as printed or null", "productName": "name as printed", "quantity": number or null, "unitPrice": number or null, "lineTotal": number or null}
  ]
}"""

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
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": "Read this invoice photo and extract every line item in the specified JSON format."},
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

    # Match each extracted line against registered ingredients by product code
    detected_supplier = (parsed.get("supplierName") or "").strip()

    def names_roughly_match(a, b):
        a, b = (a or "").strip().lower(), (b or "").strip().lower()
        if not a or not b:
            return True  # nothing to compare against, don't flag a mismatch
        return a in b or b in a

    by_code = {}
    all_ingredients = []
    for i in list_ingredient_ids(user["company_id"]):
        ing = load_ingredient(i)
        if not ing:
            continue
        all_ingredients.append(ing)
        if ing.get("product_code"):
            by_code[ing["product_code"]] = ing

    def find_possible_match(invoice_name, code):
        """When a product code doesn't match anything, look for a registered
        ingredient with a very similar name — likely the supplier just
        changed the code for the same product."""
        if not invoice_name:
            return None
        best, best_ratio = None, 0.0
        for ing in all_ingredients:
            if ing.get("product_code") == code:
                continue  # already matched by code, not what we're looking for
            ratio = difflib.SequenceMatcher(None, invoice_name.lower(), ing["name"].lower()).ratio()
            if ratio > best_ratio:
                best, best_ratio = ing, ratio
        if best and best_ratio >= 0.6:
            return {"id": best["id"], "name": best["name"], "oldCode": best.get("product_code")}
        return None

    results = []
    for item in parsed.get("items", []):
        code = item.get("productCode")
        match = by_code.get(code) if code else None
        possible_match = find_possible_match(item.get("productName"), code) if not match else None

        qty = item.get("quantity")
        unit_price = item.get("unitPrice")
        line_total = item.get("lineTotal")
        # Fill in whichever of qty/unitPrice/lineTotal is missing, if the other two are known.
        if line_total is None and qty is not None and unit_price is not None:
            line_total = round(qty * unit_price, 2)
        elif unit_price is None and qty and line_total is not None:
            unit_price = round(line_total / qty, 4)

        current_price = match.get("price") if match else None
        price_changed = bool(
            match and unit_price is not None and current_price is not None
            and abs(float(current_price) - float(unit_price)) > 0.001
        )
        registered_supplier_name = match.get("supplier_name") if match else None
        supplier_mismatch = bool(
            match and registered_supplier_name and detected_supplier
            and not names_roughly_match(registered_supplier_name, detected_supplier)
        )
        results.append({
            "productCode": code,
            "invoiceName": item.get("productName"),
            "quantity": qty,
            "unitPrice": unit_price,
            "lineTotal": line_total,
            "matchedIngredientId": match["id"] if match else None,
            "matchedName": match["name"] if match else None,
            "currentPrice": current_price,
            "priceChanged": price_changed,
            "registeredSupplierName": registered_supplier_name,
            "supplierMismatch": supplier_mismatch,
            "possibleMatch": possible_match,
        })

    return jsonify({"detectedSupplierName": detected_supplier or None, "items": results})
