frappe.listview_settings["Bank Narration Rule"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Learn from History"), () => open_learn_dialog(listview));
	},
};

function open_learn_dialog(listview) {
	frappe.call({
		method: "alfaedge_finance.alfaedge_finance.doctype.bank_narration_rule.bank_narration_rule.get_history_proposals",
		freeze: true,
		callback(r) {
			const proposals = r.message || [];
			if (!proposals.length) {
				frappe.msgprint(__("No new consistent patterns found in reconciled Bank Transactions."));
				return;
			}
			const esc = frappe.utils.escape_html;
			const rows = proposals
				.map(
					(p, i) => `<tr>
					<td><input type="checkbox" class="bnr-pick" data-index="${i}" checked></td>
					<td>${esc(p.direction)}</td>
					<td>${esc(p.counterparty_key)}</td>
					<td>${esc(p.suggested_doctype)}</td>
					<td>${esc(p.party ? `${p.party_type}: ${p.party}` : p.account || "")}</td>
					<td class="text-right">${p.occurrences}</td></tr>`
				)
				.join("");

			const dialog = new frappe.ui.Dialog({
				title: __("Learn Bank Narration Rules from History"),
				size: "extra-large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "table",
						options: `<p class="small text-muted">${__(
							"Each counterparty below was always booked the same way in past reconciled Bank Transactions. Untick any you don't want as a rule."
						)}</p>
						<div style="max-height: 60vh; overflow: auto;"><table class="table table-bordered small">
						<thead><tr><th></th><th>${__("Direction")}</th><th>${__("Counterparty")}</th><th>${__(
							"Create"
						)}</th><th>${__("Party / Account")}</th><th>${__("Seen")}</th></tr></thead>
						<tbody>${rows}</tbody></table></div>`,
					},
				],
				primary_action_label: __("Create Rules"),
				primary_action() {
					const picked = dialog.$wrapper
						.find(".bnr-pick:checked")
						.map((_, el) => proposals[$(el).data("index")])
						.get();
					frappe.call({
						method: "alfaedge_finance.alfaedge_finance.doctype.bank_narration_rule.bank_narration_rule.create_rules",
						args: { rules: picked },
						freeze: true,
						callback(res) {
							dialog.hide();
							frappe.show_alert({ message: __("{0} rule(s) created", [res.message.created]), indicator: "green" });
							listview.refresh();
						},
					});
				},
			});
			dialog.show();
		},
	});
}
