import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	resolve_by_gst,
)

TEST_GSTIN = "29AABCF8078M1C8"


class TestSupplierResolution(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Supplier", {"gstin": TEST_GSTIN}):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "Test PIA Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": TEST_GSTIN,
				}
			).insert(ignore_permissions=True)

	def test_matches_existing_supplier_by_gstin(self):
		self.assertEqual(resolve_by_gst(TEST_GSTIN), "Test PIA Supplier")

	def test_no_match_for_unknown_gstin(self):
		self.assertIsNone(resolve_by_gst("27ZZZZZ9999Z1Z1"))

	def test_no_gstin_is_not_an_error(self):
		self.assertIsNone(resolve_by_gst(None))
		self.assertIsNone(resolve_by_gst(""))
