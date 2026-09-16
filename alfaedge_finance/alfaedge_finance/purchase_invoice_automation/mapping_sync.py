"""Keep the global item/tax mapping tables in sync with whatever a reviewer
maps by hand on a Purchase Expense Center - not just mappings applied through
the dedicated "Bulk Map Items" dialog. Without this, an individually-mapped
row would work for that one invoice but never get remembered for the next one.
"""

import frappe

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.text_normalization import (
	normalize_for_matching,
)


def sync_mappings(doc):
	for row in doc.items:
		if row.mapped_item and row.item_name:
			_upsert(
				"Purchase Item Mapping",
				{"supplier_item_description": normalize_for_matching(row.item_name)},
				"mapped_item",
				row.mapped_item,
			)

	for row in doc.taxes:
		if row.mapped_account and row.tax_type:
			_upsert(
				"Tax Account Mapping",
				{"tax_type": row.tax_type},
				"mapped_account",
				row.mapped_account,
			)


def _upsert(doctype, key, value_field, value):
	name = frappe.db.get_value(doctype, key, "name")
	if name:
		if frappe.db.get_value(doctype, name, value_field) != value:
			frappe.db.set_value(doctype, name, value_field, value)
	else:
		frappe.get_doc({"doctype": doctype, **key, value_field: value}).insert(ignore_permissions=True)
