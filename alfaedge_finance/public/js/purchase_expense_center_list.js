frappe.listview_settings["Purchase Expense Center"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Bulk Map Items"), () => {
			open_bulk_map_items_dialog(listview);
		});

		listview.page.add_inner_button(__("New from PDF"), () => {
			open_single_pdf_upload_dialog(listview);
		});
	},
};

function open_bulk_map_items_dialog(listview) {
	frappe.call({
		method: "alfaedge_finance.alfaedge_finance.purchase_invoice_automation.item_mapping.get_unmapped_item_descriptions",
		freeze: true,
		callback(r) {
			const rows = r.message || [];
			if (!rows.length) {
				frappe.msgprint(__("No unmapped item descriptions found."));
				return;
			}

			const fields = rows.map((row, i) => ({
				fieldname: `item_${i}`,
				fieldtype: "Link",
				options: "Item",
				label: `${row.item_name} (${row.row_count} row${row.row_count === 1 ? "" : "s"})`,
			}));

			const dialog = new frappe.ui.Dialog({
				title: __("Bulk Map Items"),
				fields,
				size: "large",
				primary_action_label: __("Apply"),
				primary_action(values) {
					const mapping = {};
					rows.forEach((row, i) => {
						const mapped = values[`item_${i}`];
						if (mapped) mapping[row.item_name] = mapped;
					});

					frappe.call({
						method: "alfaedge_finance.alfaedge_finance.purchase_invoice_automation.item_mapping.bulk_map_items",
						args: { mapping },
						freeze: true,
						callback(res) {
							frappe.show_alert({
								message: __("Updated {0} row(s)", [res.message.updated_rows]),
								indicator: "green",
							});
							dialog.hide();
							listview.refresh();
						},
					});
				},
			});
			dialog.show();
		},
	});
}

function open_single_pdf_upload_dialog(listview) {
	const dialog = new frappe.ui.Dialog({ title: __("New Purchase Expense Center from PDF") });

	// Plain native file input, since we need raw file bytes (not an uploaded File doc).
	dialog.$body.html(
		`<input type="file" accept="application/pdf" multiple class="form-control" id="pec-pdf-input">`
	);

	dialog.set_primary_action(__("Upload"), () => {
		const input = dialog.$body.find("#pec-pdf-input")[0];
		const files = Array.from(input.files || []);
		if (!files.length) {
			frappe.msgprint(__("Choose at least one PDF file."));
			return;
		}

		Promise.all(files.map(read_file_as_base64)).then((attachments) => {
			frappe.call({
				method: "alfaedge_finance.alfaedge_finance.api.purchase_invoice_upload.upload_purchase_invoices",
				args: { attachments },
				freeze: true,
				freeze_message: __("Uploading and queueing for extraction..."),
				callback(r) {
					dialog.hide();
					const results = r.message || [];
					const succeeded = results.filter((x) => x.status === "queued").length;
					frappe.show_alert({
						message: __("{0} of {1} file(s) queued for extraction", [succeeded, results.length]),
						indicator: succeeded === results.length ? "green" : "orange",
					});
					listview.refresh();
				},
			});
		});
	});

	dialog.show();
}

function read_file_as_base64(file) {
	return new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => {
			const base64 = reader.result.split(",")[1];
			resolve({ filename: file.name, content: base64 });
		};
		reader.onerror = reject;
		reader.readAsDataURL(file);
	});
}
