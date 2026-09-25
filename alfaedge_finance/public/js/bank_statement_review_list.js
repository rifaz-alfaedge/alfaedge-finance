frappe.listview_settings["Bank Statement Review"] = {
	get_indicator(doc) {
		const colors = { Draft: "orange", Reviewed: "blue", Sent: "green" };
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},

	onload(listview) {
		listview.page.add_inner_button(__("Upload Bank Statement"), () => open_statement_upload_dialog());
	},
};

function open_statement_upload_dialog() {
	const dialog = new frappe.ui.Dialog({
		title: __("Upload Bank Statement"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "file_input",
				options: `<label class="control-label">${__("Statement file (.xlsx / .xls)")}</label>
					<input type="file" accept=".xlsx,.xls" class="form-control" id="bsr-file-input">
					<p class="help-box small text-muted">${__(
						"Any period works - the dates and the bank account are read from the file."
					)}</p>`,
			},
			{
				fieldtype: "Link",
				fieldname: "bank_account",
				label: __("Bank Account"),
				options: "Bank Account",
				description: __("Optional. Leave blank to pick it by the account number in the statement."),
				get_query: () => ({ filters: { is_company_account: 1 } }),
			},
		],
		primary_action_label: __("Upload & Analyse"),
		primary_action(values) {
			const file = (dialog.$wrapper.find("#bsr-file-input")[0].files || [])[0];
			if (!file) {
				frappe.msgprint(__("Choose the statement file."));
				return;
			}
			const reader = new FileReader();
			reader.onload = () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.api.bank_statement_upload.upload_bank_statement",
					args: {
						filename: file.name,
						content: reader.result.split(",")[1],
						bank_account: values.bank_account || null,
					},
					freeze: true,
					freeze_message: __("Reading statement and matching entries..."),
					callback(r) {
						if (!r.message) return;
						dialog.hide();
						frappe.set_route("Form", "Bank Statement Review", r.message);
					},
				});
			};
			reader.readAsDataURL(file);
		},
	});
	dialog.show();
}
