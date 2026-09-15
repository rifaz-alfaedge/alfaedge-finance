from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.extraction import (
	strip_markdown_fences,
)


class TestExtraction(FrappeTestCase):
	def test_strips_json_code_fence(self):
		raw = '```json\n{"a": 1}\n```'
		self.assertEqual(strip_markdown_fences(raw), '{"a": 1}')

	def test_strips_bare_code_fence(self):
		raw = '```\n{"a": 1}\n```'
		self.assertEqual(strip_markdown_fences(raw), '{"a": 1}')

	def test_passes_through_plain_json(self):
		raw = '{"a": 1}'
		self.assertEqual(strip_markdown_fences(raw), '{"a": 1}')

	def test_handles_surrounding_whitespace(self):
		raw = '  \n```json\n{"a": 1}\n```\n  '
		self.assertEqual(strip_markdown_fences(raw), '{"a": 1}')
