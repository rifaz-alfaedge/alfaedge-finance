import frappe
from frappe.model.document import Document


class PurchaseInvoiceAutomationSettings(Document):
	pass


def get_settings():
	"""Read automation config, falling back to site_config.json for secrets left blank."""
	settings = frappe.get_single("Purchase Invoice Automation Settings")
	return {
		"company": settings.company,
		"default_uom": settings.default_uom,
		"bifrost_endpoint": settings.bifrost_endpoint,
		"bifrost_model": settings.bifrost_model,
		"bifrost_virtual_key": settings.get_password("bifrost_virtual_key", raise_exception=False)
		or frappe.conf.get("BIFROST_VIRTUAL_KEY"),
		"webhook_secret": settings.get_password("webhook_secret", raise_exception=False)
		or frappe.conf.get("WEBHOOK_SECRET"),
	}
