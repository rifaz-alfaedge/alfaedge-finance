frappe.ui.form.on("Purchase Expense Center", {
	refresh(frm) {
		if (frm.doc.purchase_invoice) {
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

	existing_supplier(frm) {
		if (!frm.doc.existing_supplier) return;

		frappe.call({
			method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.get_supplier_tds",
			args: { supplier: frm.doc.existing_supplier, posting_date: frm.doc.supplier_invoice_date },
			callback(r) {
				const tds = r.message;
				if (!tds) return;

				frm.set_value("is_tds_applicable", 1);
				frm.set_value("tds_category", tds.category);
				frm.set_value("tds_rate", tds.rate);
				if (!frm.doc.tds_account) {
					frm.set_value("tds_account", tds.account);
				}
				if (frm.doc.extracted_taxable_amount) {
					frm.set_value(
						"tds_amount",
						flt((frm.doc.extracted_taxable_amount * tds.rate) / 100, precision("tds_amount", frm.doc))
					);
				}
				frappe.show_alert({
					message: __("Tax Withholding Category {0} found on this Supplier - TDS fields pre-filled.", [tds.category]),
					indicator: "blue",
				});
			},
		});
	},

	tds_category(frm) {
		if (!frm.doc.tds_category) return;

		frappe.call({
			method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.get_tax_withholding_category_rate",
			args: { category: frm.doc.tds_category, posting_date: frm.doc.supplier_invoice_date },
			callback(r) {
				const tds = r.message;
				if (!tds) {
					frappe.msgprint(__("This Tax Withholding Category has no rate covering this invoice's date, or no account configured for this company."));
					return;
				}

				frm.set_value("is_tds_applicable", 1);
				frm.set_value("tds_rate", tds.rate);
				if (!frm.doc.tds_account) {
					frm.set_value("tds_account", tds.account);
				}
				if (frm.doc.extracted_taxable_amount) {
					frm.set_value(
						"tds_amount",
						flt((frm.doc.extracted_taxable_amount * tds.rate) / 100, precision("tds_amount", frm.doc))
					);
				}
			},
		});
	},
});
