import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import _apply_extraction


class TestApplyExtraction(FrappeTestCase):
	def test_skips_zero_amount_line_items(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"items": [
				{"item_name": "Free Sample", "qty": 1, "rate": 0, "amount": 0},
				{"item_name": "Real Item", "qty": 2, "rate": 50, "amount": 100},
			],
			"taxes": [],
		}
		_apply_extraction(doc, parsed)

		self.assertEqual(len(doc.items), 1)
		self.assertEqual(doc.items[0].item_name, "Real Item")

	def test_skips_line_item_with_zero_qty_and_rate(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {"items": [{"item_name": "Note", "qty": 0, "rate": 0}], "taxes": []}
		_apply_extraction(doc, parsed)

		self.assertEqual(len(doc.items), 0)
