"""Bulk backfill tool for mapping unmapped item descriptions across every
Purchase Expense Center at once.

The bulk UPDATE intentionally bypasses full document save/validation - mapped_item
doesn't affect totals or trigger recalculation, so this is a safe, fast path for
backfilling ~100 existing unmapped rows.
"""

import json

import frappe


@frappe.whitelist()
def get_unmapped_item_descriptions():
	return frappe.db.sql(
		"""
		select item_name, count(*) as row_count
		from `tabPurchase Invoice Item`
		where parenttype = 'Purchase Expense Center'
		  and (mapped_item is null or mapped_item = '')
		  and item_name is not null and item_name != ''
		group by item_name
		order by row_count desc
		""",
		as_dict=True,
	)


@frappe.whitelist()
def bulk_map_items(mapping):
	"""mapping: JSON string or dict of {item_name: mapped_item}."""
	if isinstance(mapping, str):
		mapping = json.loads(mapping)

	updated = 0
	for item_name, mapped_item in mapping.items():
		if not mapped_item:
			continue
		frappe.db.sql(
			"""
			update `tabPurchase Invoice Item`
			set mapped_item = %s
			where parenttype = 'Purchase Expense Center' and item_name = %s
			""",
			(mapped_item, item_name),
		)
		updated += frappe.db.sql("select row_count()")[0][0]

		if frappe.db.exists("Purchase Item Mapping", {"supplier_item_description": item_name}):
			frappe.db.set_value(
				"Purchase Item Mapping", {"supplier_item_description": item_name}, "mapped_item", mapped_item
			)
		else:
			frappe.get_doc(
				{
					"doctype": "Purchase Item Mapping",
					"supplier_item_description": item_name,
					"mapped_item": mapped_item,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
	return {"updated_rows": updated}
