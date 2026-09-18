"""Structured data extraction from a supplier invoice PDF via the Bifrost LLM gateway."""

import base64
import json

import frappe
import requests

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)

EXTRACTION_PROMPT_TEMPLATE = """Analyse this purchase invoice PDF and extract information
about the SUPPLIER (the party who issued/sent this invoice and is being paid) - not the
buyer/customer/bill-to party.

{buyer_note}

Return ONLY a valid JSON object with these exact keys - no markdown, no commentary:

{{
  "supplier_name":   "Full supplier company name or null",
  "gst_number":      "15-char Indian GSTIN or null (some invoices have none)",
  "invoice_number":  "Invoice number or null",
  "invoice_date":    "YYYY-MM-DD or null",
  "address":         "Street address of supplier or null",
  "city":            "City or null",
  "state":           "State or null",
  "postal_code":     "PIN / postal code or null",
  "country":         "Supplier's country, full English name (e.g. 'India', 'United States') or null",
  "currency":        "3-letter ISO 4217 currency code the invoice is billed in (e.g. 'INR', 'USD') or null",
  "items": [
    {{ "item_name": "description", "qty": 0, "rate": 0.0, "amount": 0.0 }}
  ],
  "taxes": [
    {{ "tax_type": "CGST", "rate": 9, "amount": 0.0 }}
  ],
  "taxable_amount": 0.0,
  "grand_total": 0.0,
  "tds_rate": null,
  "tds_amount": null
}}

Use null for missing strings and 0 for missing numbers.
"supplier_name", "gst_number", "address", "city", "state", "postal_code", and "country"
must all describe the SUPPLIER issuing the invoice. Many invoices also print the buyer's
own name, address, and GSTIN in a "Bill To" / "Customer" / "Ship To" section - never
extract any of those buyer-side details into these fields, even if the supplier's own
details are less prominent on the page.
"gst_number" must be a real 15-character Indian GSTIN belonging to the supplier, and
nothing else. If no GSTIN is visible for the supplier - including for any supplier based
outside India, who will never have one - use null, even if a GSTIN appears elsewhere on
the invoice for the buyer. Never substitute a different identifier (EIN, VAT number, tax
ID, company registration number, etc.) for gst_number just because one is printed.
"currency" must be inferred from the invoice itself (a currency symbol, or an explicit
mention) - never assume INR by default. All amounts (item rates, tax amounts,
taxable_amount, grand_total, tds_amount) are in this same currency, exactly as printed -
do not convert them to any other currency.
"tax_type" must be one of CGST, SGST, IGST, or Other if it doesn't match those.
"rate" is a plain number, e.g. 9 for 9%.
"taxable_amount" is the pre-tax subtotal as printed on the invoice.
"grand_total" is the final total as printed on the invoice (items + all taxes).
Do not invent tax rows if none are printed on the invoice (e.g. reverse-charge or
tax-exempt invoices) - an empty taxes array is a valid and expected result.
"tds_rate" and "tds_amount" are only for a TDS (tax deducted at source) deduction that
is explicitly printed on the invoice itself (e.g. a line saying "Less: TDS @ 2%" or
similar). Use null for both if the invoice does not mention TDS - do not guess or
calculate a TDS figure that isn't printed.
"""


def strip_markdown_fences(text):
	"""Models often wrap JSON in ```json ... ``` despite instructions not to."""
	text = text.strip()
	if text.startswith("```"):
		newline_pos = text.find("\n")
		if newline_pos != -1:
			text = text[newline_pos + 1 :]
		if text.endswith("```"):
			text = text[:-3]
	return text.strip()


def _build_prompt(company: str) -> str:
	"""Names our own company (and its GSTIN, if it has one) explicitly, so the
	model can recognize and exclude it when it shows up in a "Bill To" section -
	seen in practice: our own company's GSTIN extracted as the supplier's.
	"""
	company_gstin = frappe.db.get_value("Company", company, "gstin")
	buyer_note = f'The invoice is addressed TO our own company, "{company}"'
	if company_gstin:
		buyer_note += f' (GSTIN {company_gstin})'
	buyer_note += (
		" - that is the buyer, not the supplier. If you see this name or GSTIN on the "
		"invoice, it identifies the buyer/customer, never extract it as the supplier."
	)
	return EXTRACTION_PROMPT_TEMPLATE.format(buyer_note=buyer_note)


def extract_invoice_data(pdf_bytes: bytes) -> dict:
	"""Call Bifrost with the PDF and return the parsed extraction dict.

	Raises on any failure (network, non-2xx, unparseable JSON) so the caller can
	record it against the Purchase Expense Center's failure_reason.
	"""
	settings = get_settings()
	pdf_base64 = base64.b64encode(pdf_bytes).decode()

	payload = {
		"model": settings["bifrost_model"],
		"stream": False,
		"messages": [
			{
				"role": "user",
				"content": [
					{
						"type": "file",
						"file": {
							"filename": "invoice.pdf",
							"file_data": f"data:application/pdf;base64,{pdf_base64}",
						},
					},
					{"type": "text", "text": _build_prompt(settings["company"])},
				],
			}
		],
	}
	headers = {
		"content-type": "application/json",
		"authorization": f"Bearer {settings['bifrost_virtual_key']}",
	}

	response = requests.post(settings["bifrost_endpoint"], json=payload, headers=headers, timeout=90)
	response.raise_for_status()
	result = response.json()

	raw_text = result["choices"][0]["message"]["content"]
	raw_text = strip_markdown_fences(raw_text)
	try:
		return json.loads(raw_text)
	except json.JSONDecodeError:
		frappe.log_error(
			title="Bifrost response was not valid JSON",
			message=raw_text[:2000],
		)
		raise
