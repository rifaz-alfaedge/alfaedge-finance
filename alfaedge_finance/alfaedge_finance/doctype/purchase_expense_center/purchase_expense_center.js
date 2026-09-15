frappe.ui.form.on("Purchase Expense Center", {
	refresh(frm) {
		if (frm.doc.invoice_status === "Invoice Created" && frm.doc.purchase_invoice) {
			frm.add_custom_button(__("View Invoice"), () => {
				frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice);
			});
		} else if (frm.doc.status === "Extracted") {
			frm.add_custom_button(__("Create Invoice"), () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.create_purchase_invoice",
					args: { expense_center: frm.doc.name },
					freeze: true,
					freeze_message: __("Creating draft Purchase Invoice..."),
					callback(r) {
						if (!r.exc && r.message) {
							if (r.message.warning) {
								frappe.msgprint({ title: __("Check Before Submitting"), message: r.message.warning, indicator: "orange" });
							}
							frappe.show_alert({ message: __("Draft Purchase Invoice created"), indicator: "green" });
							frm.reload_doc();
						}
					},
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry Extraction"), () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.retry_extraction",
					args: { expense_center: frm.doc.name },
					callback(r) {
						if (!r.exc) {
							frappe.show_alert({ message: __("Re-queued for extraction"), indicator: "blue" });
							frm.reload_doc();
						}
					},
				});
			});
		}

		if (frm.doc.is_potential_duplicate) {
			frm.dashboard.add_comment(
				__("This may be a duplicate of another Purchase Expense Center with the same supplier and invoice number."),
				"orange",
				true
			);
		}
	},

	tds_category(frm) {
		if (!frm.doc.tds_category) return;

		frappe.db.get_doc("TDS Category", frm.doc.tds_category).then((category) => {
			frm.set_value("is_tds_applicable", 1);
			frm.set_value("tds_rate", category.rate);
			if (!frm.doc.tds_account) {
				frm.set_value("tds_account", category.account);
			}
			if (frm.doc.extracted_taxable_amount) {
				frm.set_value(
					"tds_amount",
					flt((frm.doc.extracted_taxable_amount * category.rate) / 100, precision("tds_amount", frm.doc))
				);
			}
		});
	},
});
