"""Normalize existing Purchase Item Mapping keys (whitespace/casing) so they
match what the app now looks up. Also merges rows that collapse onto the same
normalized key (e.g. the same recurring line item extracted with and without
an appended date range) as long as they already point at the same Item -
those are kept as one row; a genuine conflict (same normalized text, two
different mapped Items) is left alone and logged for manual review instead of
guessing which one is right.
"""

import frappe

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.text_normalization import (
	normalize_for_matching,
)


def execute():
	if not frappe.db.table_exists("Purchase Item Mapping"):
		return

	rows = frappe.get_all(
		"Purchase Item Mapping", fields=["name", "supplier_item_description", "mapped_item"]
	)

	groups = {}
	for row in rows:
		key = normalize_for_matching(row.supplier_item_description)
		groups.setdefault(key, []).append(row)

	for key, group in groups.items():
		mapped_items = {row.mapped_item for row in group}

		if len(mapped_items) > 1:
			frappe.log_error(
				title="Purchase Item Mapping normalization: conflicting mappings",
				message=(
					f"Normalized key {key!r} is shared by rows mapping to different Items "
					f"({mapped_items}); left as-is for manual review: "
					f"{[row.name for row in group]}"
				),
			)
			continue

		survivor, *duplicates = group
		for dup in duplicates:
			frappe.delete_doc("Purchase Item Mapping", dup.name, ignore_permissions=True)

		if survivor.supplier_item_description != key:
			frappe.db.set_value("Purchase Item Mapping", survivor.name, "supplier_item_description", key)

	frappe.db.commit()
