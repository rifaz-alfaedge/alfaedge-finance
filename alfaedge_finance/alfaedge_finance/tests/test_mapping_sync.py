import frappe
from frappe.tests.utils import FrappeTestCase


class TestMappingSync(FrappeTestCase):
	def test_saving_a_manually_mapped_item_row_upserts_purchase_item_mapping(self):
		description = f"Sync Test Item {frappe.generate_hash(length=6)}"
		if not frappe.db.exists("Item", "PIA Test Item"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "PIA Test Item",
					"item_name": "PIA Test Item",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"gst_hsn_code": "998719",
				}
			).insert(ignore_permissions=True)

		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		doc.append(
			"items",
			{
				"item_name": description,
				"description": description,
				"qty": 1,
				"rate": 10,
				"amount": 10,
				"uom": "Nos",
				"stock_qty": 1,
				"base_rate": 10,
				"base_amount": 10,
				"mapped_item": "PIA Test Item",
			},
		)
		doc.insert(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("Purchase Item Mapping", {"supplier_item_description": description}, "mapped_item"),
			"PIA Test Item",
		)

	def test_saving_a_manually_mapped_tax_row_upserts_tax_account_mapping(self):
		tax_type = f"SYNC-{frappe.generate_hash(length=6)}"
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		doc.append("taxes", {"tax_type": tax_type, "rate": 5, "amount": 5, "mapped_account": "Creditors - CDS"})
		doc.insert(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("Tax Account Mapping", {"tax_type": tax_type}, "mapped_account"),
			"Creditors - CDS",
		)
