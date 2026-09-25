import frappe
from frappe import _
from frappe.model.document import Document

from alfaedge_finance.alfaedge_finance.bank_statement.narration import BULK_UPLOAD, counterparty_key
from alfaedge_finance.alfaedge_finance.bank_statement.suggestion import load_history_outcomes


class BankNarrationRule(Document):
	def validate(self):
		self.counterparty_key = counterparty_key(self.counterparty_key)
		if not self.counterparty_key:
			frappe.throw(_("Counterparty Key is required."))
		if self.counterparty_key == counterparty_key(BULK_UPLOAD):
			frappe.throw(
				_(
					"Bulk uploads pay different parties each time; they are booked from the bank's bulk file, not a rule."
				)
			)
		self.rule_key = f"{self.direction}|{self.counterparty_key}"
		if self.suggested_doctype == "Payment Entry" and not (self.party_type and self.party):
			frappe.throw(_("A Payment Entry rule needs a Party Type and Party."))
		if self.treat_as_advance and self.suggested_doctype != "Payment Entry":
			frappe.throw(_("Only a Payment Entry rule can treat payments as advances."))
		if self.suggested_doctype == "Journal Entry" and not self.account:
			frappe.throw(_("A Journal Entry rule needs an Account."))


@frappe.whitelist()
def get_history_proposals():
	"""Counterparties whose past reconciled Bank Transactions were all booked the same
	way and that have no rule yet - offered to the user to save as rules in bulk."""
	frappe.has_permission("Bank Narration Rule", "create", throw=True)
	existing = set(frappe.get_all("Bank Narration Rule", pluck="rule_key"))
	proposals = []
	for (direction, key), outcomes in sorted(load_history_outcomes().items()):
		if f"{direction}|{key}" in existing or None in outcomes or len(set(outcomes)) != 1:
			continue
		doctype, party_type, party, account = outcomes[0]
		proposals.append(
			{
				"direction": direction,
				"counterparty_key": key,
				"suggested_doctype": doctype,
				"party_type": party_type,
				"party": party,
				"account": account,
				"occurrences": len(outcomes),
			}
		)
	return proposals


@frappe.whitelist()
def create_rules(rules):
	frappe.has_permission("Bank Narration Rule", "create", throw=True)
	rules = frappe.parse_json(rules)
	created = 0
	for rule in rules:
		doc = frappe.get_doc(
			{
				"doctype": "Bank Narration Rule",
				"direction": rule["direction"],
				"counterparty_key": rule["counterparty_key"],
				"suggested_doctype": rule["suggested_doctype"],
				"party_type": rule.get("party_type"),
				"party": rule.get("party"),
				"account": rule.get("account"),
			}
		)
		doc.validate()
		if frappe.db.exists("Bank Narration Rule", {"rule_key": doc.rule_key}):
			continue
		doc.insert()
		created += 1
	return {"created": created}
