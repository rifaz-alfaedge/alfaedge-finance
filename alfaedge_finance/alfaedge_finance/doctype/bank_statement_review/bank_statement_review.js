// Bank Statement Review - staging step before ERPNext's Bank Reconciliation Tool.

const BSR_STATUS_COLORS = {
	"Already Booked": "green",
	"Partly Booked": "yellow",
	Check: "orange",
	Suggested: "blue",
	"Draft Created": "purple",
	"Already Imported": "gray",
	Ignored: "gray",
};

const BSR_FILTERS = {
	"Needs action": ["Suggested", "Check", "Draft Created", "Partly Booked"],
	Booked: ["Already Booked"],
	"Already Imported": ["Already Imported"],
	Ignored: ["Ignored"],
	All: null,
};

// Documents table filters, first one is the default.
const BSR_DOC_FILTERS = {
	"Drafts (not submitted)": (v) => v.voucher_status === "Draft",
	Advances: (v) => !!v.is_advance,
	Created: (v) => v.link_type === "Created",
	Matched: (v) => ["Matched", "Chosen"].includes(v.link_type),
	All: () => true,
};
const BSR_VOUCHER_COLORS = { Draft: "orange", Submitted: "green", Cancelled: "red" };

frappe.ui.form.on("Bank Statement Review", {
	setup(frm) {
		frm.set_query("bank_account", () => ({ filters: { is_company_account: 1 } }));
		frm.bsr_filter = "Needs action";
		frm.bsr_doc_filter = Object.keys(BSR_DOC_FILTERS)[0];
	},

	onload(frm) {
		frappe.realtime.on("bank_statement_review_submit_progress", (data) => {
			if (data.review !== frm.doc.name) return;
			frappe.show_progress(__("Submitting drafts"), data.done, data.total, __("{0} of {1}", [data.done, data.total]));
		});
		frappe.realtime.on("bank_statement_review_submit_done", (data) => {
			if (data.review !== frm.doc.name) return;
			frappe.hide_progress();
			if (data.failed.length) {
				frappe.msgprint({
					title: __("Submitted {0}, {1} failed", [data.submitted.length, data.failed.length]),
					message: data.failed.map(esc).join("<br>"),
					indicator: "orange",
				});
			} else {
				frappe.show_alert({ message: __("Submitted {0} draft(s)", [data.submitted.length]), indicator: "green" });
			}
			frm.reload_doc();
		});
	},

	refresh(frm) {
		render_workbench(frm);
		render_documents(frm);
		if (frm.is_new()) return;

		frm.add_custom_button(__("Re-run Reconciliation"), () => rerun(frm)).addClass("btn-primary");
		frm.add_custom_button(
			__("Create Drafts (High confidence)"),
			() => create_high_confidence_drafts(frm),
			__("Actions")
		);
		frm.add_custom_button(__("Send to Bank Reconciliation"), () => send_to_bank_reconciliation(frm), __("Actions"));
		frm.add_custom_button(__("Open Bank Reconciliation Tool"), () => open_bank_reconciliation_tool(frm), __("Actions"));
	},
});

function call_doc(frm, method, args, freeze_message) {
	return frm
		.call({ method, doc: frm.doc, args: args || {}, freeze: true, freeze_message })
		.then((r) => {
			frm.reload_doc();
			return r.message;
		});
}

function rerun(frm) {
	call_doc(frm, "rerun", {}, __("Matching statement lines...")).then((result) => {
		frappe.show_alert({ message: result.summary, indicator: result.newly_booked ? "green" : "blue" }, 8);
	});
}

function create_high_confidence_drafts(frm) {
	call_doc(frm, "create_high_confidence_drafts").then((result) => {
		let message = __("{0} draft(s) created.", [result.created.length]);
		if (result.failed.length) {
			message +=
				"<br><br>" + __("Could not create:") + "<br>" + result.failed.map(frappe.utils.escape_html).join("<br>");
		}
		frappe.msgprint({ title: __("Drafts"), message, indicator: result.failed.length ? "orange" : "green" });
	});
}

function send_to_bank_reconciliation(frm) {
	const open = (frm.doc.lines || []).filter(
		(l) => !l.bank_transaction && ["Suggested", "Check", "Draft Created", "Partly Booked"].includes(l.line_status)
	);
	const go = () =>
		call_doc(frm, "send_to_bank_reconciliation", {}, __("Creating Bank Transactions...")).then((result) => {
			frappe.msgprint({
				title: __("Sent to Bank Reconciliation"),
				message: __("{0} Bank Transaction(s) created, {1} linked to existing ones.", [result.created, result.linked]),
				indicator: "green",
				primary_action: {
					label: __("Open Bank Reconciliation Tool"),
					action: () => open_bank_reconciliation_tool(frm),
				},
			});
		});

	const warning = __(
		"Once sent, this review can no longer be deleted. {0} line(s) are still not fully booked by submitted entries - they will be sent anyway and show as unmatched in the Bank Reconciliation Tool. Continue?",
		[open.length]
	);
	frappe.confirm(open.length ? warning : __("Once sent, this review can no longer be deleted. Continue?"), go);
}

function open_bank_reconciliation_tool(frm) {
	frappe.set_route("Form", "Bank Reconciliation Tool").then(() => {
		const tool = cur_frm;
		if (!tool || tool.doctype !== "Bank Reconciliation Tool") return;
		tool.set_value("company", frm.doc.company);
		tool.set_value("bank_account", frm.doc.bank_account);
		tool.set_value("bank_statement_from_date", frm.doc.from_date);
		tool.set_value("bank_statement_to_date", frm.doc.to_date);
	});
}

// ------------------------------------------------------------------ helpers

const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));

function voucher_link(doctype, name) {
	if (!doctype || !name) return "";
	return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}" target="_blank">${esc(name)}</a>`;
}

function line_vouchers(frm, line) {
	return (frm.doc.vouchers || []).filter((v) => v.line_no === line.idx);
}

function line_amount(line) {
	return flt(line.withdrawal) || flt(line.deposit);
}

function parse_json_list(value) {
	// A stored JSON value that can't be read must not blank the whole table.
	try {
		const parsed = value ? JSON.parse(value) : [];
		return Array.isArray(parsed) ? parsed : [];
	} catch (e) {
		return [];
	}
}

function split_plan(line) {
	return parse_json_list(line.split_plan);
}

// ----------------------------------------------------------------- workbench

function render_workbench(frm) {
	const $wrapper = frm.get_field("workbench_html").$wrapper;
	if (frm.is_new() || !(frm.doc.lines || []).length) {
		$wrapper.html(
			`<p class="text-muted">${__("Attach the statement and save, or use Upload Bank Statement on the list view.")}</p>`
		);
		return;
	}

	const lines = frm.doc.lines;
	const counts = {};
	lines.forEach((l) => (counts[l.line_status || "Pending"] = (counts[l.line_status || "Pending"] || 0) + 1));

	const difference = flt(frm.doc.closing_balance) - flt(frm.doc.book_balance);
	const balance_note =
		Math.abs(difference) < 0.005
			? `<span class="indicator-pill green">${__("Book balance matches statement")}</span>`
			: `<span class="indicator-pill orange">${__("Book balance differs from statement by {0}", [
					format_currency(difference, frm.doc.currency),
			  ])}</span>`;

	const pills = Object.keys(BSR_FILTERS)
		.map((label) => {
			const statuses = BSR_FILTERS[label];
			const count = statuses ? statuses.reduce((n, s) => n + (counts[s] || 0), 0) : lines.length;
			const active = frm.bsr_filter === label ? "btn-primary" : "btn-default";
			return `<button class="btn btn-xs ${active} bsr-filter" data-filter="${label}">${__(label)} (${count})</button>`;
		})
		.join(" ");

	const statuses = BSR_FILTERS[frm.bsr_filter];
	const visible = lines.filter((l) => !statuses || statuses.includes(l.line_status));
	const rows = visible.map((line) => row_html(frm, line)).join("");

	$wrapper.html(`
		<div class="flex justify-between align-center flex-wrap" style="gap: 8px; margin-bottom: 10px;">
			<div>${pills}
				<button class="btn btn-xs btn-default bsr-mark-advance" disabled>${__("Mark selected as Advance")}</button>
				<span class="text-muted small bsr-line-selected"></span>
			</div>
			<div>${balance_note}</div>
		</div>
		<div style="overflow-x: auto;">
			<table class="table table-bordered table-hover" style="font-size: 12px; margin: 0;">
				<thead><tr>
					<th style="width:28px"><input type="checkbox" class="bsr-line-pick-all" title="${__("Select all")}"></th>
					<th style="width:30px">#</th>
					<th style="width:85px">${__("Date")}</th>
					<th>${__("Particulars")}</th>
					<th class="text-right" style="width:110px">${__("Withdrawal")}</th>
					<th class="text-right" style="width:110px">${__("Deposit")}</th>
					<th style="width:120px">${__("Status")}</th>
					<th>${__("Record")}</th>
					<th style="width:190px"></th>
				</tr></thead>
				<tbody>${rows || `<tr><td colspan="9" class="text-muted text-center">${__("Nothing here.")}</td></tr>`}</tbody>
			</table>
		</div>`);

	$wrapper.find(".bsr-filter").on("click", function () {
		frm.bsr_filter = $(this).data("filter");
		render_workbench(frm);
	});
	const picked_lines = () =>
		$wrapper
			.find(".bsr-line-pick:checked")
			.map((_, el) => lines.find((l) => l.name === $(el).data("row")))
			.get();
	const update_line_selection = () => {
		const selected = picked_lines();
		$wrapper.find(".bsr-mark-advance").prop("disabled", !selected.length);
		$wrapper.find(".bsr-line-selected").text(selected.length ? __("{0} selected", [selected.length]) : "");
	};
	$wrapper.find(".bsr-line-pick").on("change", update_line_selection);
	$wrapper.find(".bsr-line-pick-all").on("change", function () {
		$wrapper.find(".bsr-line-pick").prop("checked", $(this).prop("checked"));
		update_line_selection();
	});
	$wrapper.find(".bsr-mark-advance").on("click", () => open_advance_dialog(frm, picked_lines()));
	$wrapper.find("[data-action]").on("click", function () {
		const line = lines.find((l) => l.name === $(this).data("row"));
		line_action(frm, line, $(this).data("action"));
	});
}

function vouchers_html(frm, line) {
	return line_vouchers(frm, line)
		.map(
			(v) => `<div>${esc(v.voucher_type)} ${voucher_link(v.voucher_type, v.voucher_no)}
				<span class="indicator-pill ${BSR_VOUCHER_COLORS[v.voucher_status] || "gray"}" style="font-size:10px">${__(
				v.voucher_status
			)}</span> ${format_currency(v.amount, line.currency)}${v.party ? " · " + esc(v.party) : ""}</div>`
		)
		.join("");
}

function record_html(frm, line) {
	switch (line.line_status) {
		case "Already Booked":
		case "Draft Created":
		case "Partly Booked": {
			const hint =
				line.line_status === "Draft Created"
					? __("Submit the draft(s), then Re-run Reconciliation")
					: line.line_status === "Partly Booked"
					? __("{0} still unbooked", [format_currency(line_amount(line) - sum_vouchers(frm, line), line.currency)])
					: esc(line.match_basis || "");
			return `${vouchers_html(frm, line)}<div class="text-muted small">${hint}</div>`;
		}
		case "Check": {
			const options = parse_json_list(line.alternatives);
			const first = (options[0] || [])
				.map((c) => `${esc(c.voucher_type)} ${voucher_link(c.voucher_type, c.voucher_no)}`)
				.join(" + ");
			return `${__("Possibly")} ${first}${options.length > 1 ? ` ${__("(+{0} more)", [options.length - 1])}` : ""}
				<div class="text-muted small">${esc(line.match_basis || "")}</div>`;
		}
		case "Already Imported": {
			const doctype = (line.duplicate_of || "").startsWith("BSR-") ? "Bank Statement Review" : "Bank Transaction";
			return `${__("Already in")} ${voucher_link(doctype, line.duplicate_of)}`;
		}
		case "Suggested":
			return suggestion_html(line);
		default:
			return "";
	}
}

function sum_vouchers(frm, line) {
	return line_vouchers(frm, line).reduce((n, v) => n + flt(v.amount), 0);
}

function bulk_plan(line) {
	return parse_json_list(line.bulk_plan);
}

const BSR_BULK_COLORS = { Matched: "green", "On-account": "orange", Mismatch: "red", Unresolved: "red" };

function bulk_summary_html(line) {
	const rows = bulk_plan(line);
	const list = rows
		.map(
			(r) =>
				`<div class="small"><span class="indicator-pill ${BSR_BULK_COLORS[r.status] || "gray"}" style="font-size:10px">${__(
					r.status
				)}</span> ${esc(r.beneficiary_name || r.account_no)}${r.party ? " → " + esc(r.party) : ""} · ${format_currency(
					r.amount,
					line.currency
				)}${r.documents.length ? " · " + r.documents.map((d) => esc(d.name)).join(", ") : ""}</div>`
		)
		.join("");
	return `${__("Create Journal Entry from bulk file")}:${list}`;
}

function suggestion_html(line) {
	if (line.bulk_plan) {
		return `${bulk_summary_html(line)}<div class="text-muted small">${esc(line.suggestion_source || "")}</div>`;
	}
	const plan = split_plan(line);
	const confidence = line.confidence
		? `<span class="indicator-pill ${{ High: "green", Medium: "orange", Low: "gray" }[line.confidence]}">${__(
				line.confidence
		  )}</span> `
		: "";
	let body;
	if (plan.length) {
		body =
			`${__("Split into {0} payment(s)", [new Set(plan.map((r) => r.party)).size])}:` +
			plan
				.map(
					(r) =>
						`<div class="small">${esc(r.party)} ${
							r.against_name ? "· " + voucher_link(r.against_doctype, r.against_name) : ""
						} · ${format_currency(r.amount, line.currency)}</div>`
				)
				.join("");
	} else {
		const target =
			line.suggested_doctype === "Payment Entry"
				? line.party
					? `${esc(line.party_type)}: <b>${esc(line.party)}</b>`
					: `<span class="text-muted">${__("party not set")}</span>`
				: line.account
				? `<b>${esc(line.account)}</b>${line.party ? ` (${esc(line.party)})` : ""}`
				: `<span class="text-muted">${__("account not set")}</span>`;
		const settles = line.against_name
			? `<div class="small">${__("Settles")} ${voucher_link(line.against_doctype, line.against_name)}${
					flt(line.tds_amount) ? " " + __("less TDS {0}", [format_currency(line.tds_amount, line.currency)]) : ""
			  }</div>`
			: "";
		body = line.is_advance
			? `${__("Advance to")} ${target}${settles}${
					line.advance_reference ? ` <span class="text-muted">(${esc(line.advance_reference)})</span>` : ""
			  }`
			: `${__("Create")} ${esc(line.suggested_doctype || "Journal Entry")} → ${target}${settles}`;
	}
	return `${body}<div class="text-muted small">${confidence}${esc(line.suggestion_source || "")}</div>`;
}

function can_mark_advance(line) {
	// Draft Created too: marking replaces the unsubmitted draft.
	return (
		!line.bank_transaction &&
		["Suggested", "Check", "Partly Booked", "Draft Created"].includes(line.line_status) &&
		!(is_bulk_line(line) && !line.bulk_plan) &&
		!line.bulk_plan
	);
}

function row_html(frm, line) {
	const color = BSR_STATUS_COLORS[line.line_status] || "gray";
	const buttons = [];
	const button = (action, label, cls = "btn-default") =>
		buttons.push(`<button class="btn btn-xs ${cls}" data-action="${action}" data-row="${line.name}">${label}</button>`);

	if (!line.bank_transaction) {
		const has_drafts = line_vouchers(frm, line).some((v) => v.link_type === "Created" && v.voucher_status === "Draft");
		if (["Suggested", "Check", "Partly Booked"].includes(line.line_status)) {
			// Bulk payments are booked from the bank's bulk file by default; Review still
			// lets the reviewer pick the record by hand (e.g. a single payment sent as bulk).
			if (is_bulk_line(line) && !line.bulk_plan) button("bulk", __("Upload Bulk File"), "btn-primary");
			button("review", __("Review"), is_bulk_line(line) && !line.bulk_plan ? "btn-default" : "btn-primary");
			if (line.line_status === "Suggested" && is_complete(line)) button("draft", __("Create Draft"));
			if (can_mark_advance(line)) button("advance", __("Advance"));
			if (line.line_status !== "Partly Booked") button("ignore", __("Ignore"));
		} else if (line.line_status === "Ignored") {
			button("unignore", __("Un-ignore"));
		}
		if (has_drafts) {
			if (line.line_status === "Draft Created" && !line.is_advance && can_mark_advance(line)) {
				button("advance", __("Advance"));
			}
			button("view_drafts", __("View Draft"));
			button("submit_drafts", __("Submit Draft"), "btn-success");
			button("delete_drafts", __("Delete Draft(s)"), "btn-danger");
		}
	}
	const sent = line.bank_transaction
		? `<div class="small">${__("Bank Txn")} ${voucher_link("Bank Transaction", line.bank_transaction)}</div>`
		: "";

	const advance_badge = line.is_advance
		? ` <span class="indicator-pill purple" style="font-size:10px">${__("Advance")}</span>`
		: "";
	return `<tr>
		<td>${can_mark_advance(line) ? `<input type="checkbox" class="bsr-line-pick" data-row="${line.name}">` : ""}</td>
		<td>${line.idx}</td>
		<td>${frappe.datetime.str_to_user(line.transaction_date)}</td>
		<td style="word-break: break-word;">${esc(line.particulars)}
			<div class="text-muted small">${line.reference ? __("Ref") + ": " + esc(line.reference) : ""}</div></td>
		<td class="text-right">${flt(line.withdrawal) ? format_currency(line.withdrawal, line.currency) : ""}</td>
		<td class="text-right">${flt(line.deposit) ? format_currency(line.deposit, line.currency) : ""}</td>
		<td><span class="indicator-pill ${color}">${__(line.line_status || "Pending")}</span>${advance_badge}${sent}</td>
		<td>${record_html(frm, line)}</td>
		<td>${buttons.join(" ")}</td>
	</tr>`;
}

function is_bulk_line(line) {
	return line.channel === "Bulk Upload" || (flt(line.withdrawal) && /^NEFT\/[^/]*\/\d+\/AW/i.test(line.particulars || ""));
}

function is_complete(line) {
	if (line.bulk_plan) return bulk_plan(line).every((r) => !["Mismatch", "Unresolved"].includes(r.status));
	if (split_plan(line).length) return true;
	if (line.suggested_doctype === "Payment Entry") return !!(line.party_type && line.party);
	return !!line.account;
}

function line_action(frm, line, action) {
	if (action === "review") return line.bulk_plan ? open_bulk_dialog(frm, line) : open_review_dialog(frm, line);
	if (action === "bulk") return upload_bulk_file(frm, line);
	if (action === "draft") {
		return call_doc(frm, "create_draft", {
			row_name: line.name,
			values: { use_split: split_plan(line).length > 0 },
		}).then((vouchers) =>
			frappe.show_alert({
				message: __("Draft(s) created: {0}", [vouchers.map((v) => v.name).join(", ")]),
				indicator: "green",
			})
		);
	}
	if (action === "view_drafts") {
		const drafts = line_vouchers(frm, line).filter((v) => v.link_type === "Created" && v.voucher_status === "Draft");
		if (drafts.length === 1) return frappe.set_route("Form", drafts[0].voucher_type, drafts[0].voucher_no);
		return frappe.msgprint({
			title: __("Drafts for line {0}", [line.idx]),
			message: drafts
				.map(
					(v) =>
						`<div>${esc(v.voucher_type)} ${voucher_link(v.voucher_type, v.voucher_no)} · ${format_currency(
							v.amount,
							line.currency
						)}${v.party ? " · " + esc(v.party) : ""}</div>`
				)
				.join(""),
		});
	}
	if (action === "submit_drafts") {
		const drafts = line_vouchers(frm, line).filter((v) => v.link_type === "Created" && v.voucher_status === "Draft");
		return frappe.confirm(
			__("Submit {0}? This posts it to the ledger.", [drafts.map((v) => v.voucher_no).join(", ")]),
			() => submit_drafts(frm, drafts)
		);
	}
	if (action === "delete_drafts") {
		const drafts = line_vouchers(frm, line).filter((v) => v.link_type === "Created" && v.voucher_status === "Draft");
		return frappe.confirm(
			__("Delete draft(s) {0}?", [drafts.map((v) => v.voucher_no).join(", ")]),
			() => delete_drafts(frm, drafts)
		);
	}
	if (action === "advance") return open_advance_dialog(frm, [line]);
	if (action === "ignore") return call_doc(frm, "set_ignored", { row_name: line.name, ignored: 1 });
	if (action === "unignore") return call_doc(frm, "set_ignored", { row_name: line.name, ignored: 0 });
}

function submit_drafts(frm, drafts) {
	let chain = Promise.resolve();
	drafts.forEach((v) => {
		chain = chain.then(() =>
			frm.call({
				method: "submit_draft",
				doc: frm.doc,
				args: { voucher_type: v.voucher_type, voucher_no: v.voucher_no },
				freeze: true,
				freeze_message: __("Submitting {0}...", [v.voucher_no]),
			})
		);
	});
	// Reload even if one fails, so the lines show what was submitted before the error.
	return chain
		.then(() => frappe.show_alert({ message: __("Submitted {0} document(s)", [drafts.length]), indicator: "green" }))
		.finally(() => frm.reload_doc());
}

function delete_drafts(frm, drafts) {
	let chain = Promise.resolve();
	drafts.forEach((v) => {
		chain = chain.then(() =>
			frm.call({
				method: "delete_draft",
				doc: frm.doc,
				args: { voucher_type: v.voucher_type, voucher_no: v.voucher_no },
				freeze: true,
			})
		);
	});
	return chain.then(() => {
		frappe.show_alert({ message: __("Deleted {0} draft(s)", [drafts.length]), indicator: "green" });
		frm.reload_doc();
	});
}

// ------------------------------------------------------------- documents

function render_documents(frm) {
	const $wrapper = frm.get_field("documents_html").$wrapper;
	const all = frm.doc.vouchers || [];
	if (frm.is_new() || !all.length) {
		$wrapper.html(`<p class="text-muted">${__("No documents linked or created yet.")}</p>`);
		return;
	}
	const visible = all.filter(BSR_DOC_FILTERS[frm.bsr_doc_filter] || BSR_DOC_FILTERS.All);
	const created_total = all.filter((v) => v.link_type === "Created").reduce((n, v) => n + flt(v.amount), 0);

	const pills = Object.keys(BSR_DOC_FILTERS)
		.map((label) => {
			const count = all.filter(BSR_DOC_FILTERS[label]).length;
			const active = frm.bsr_doc_filter === label ? "btn-primary" : "btn-default";
			return `<button class="btn btn-xs ${active} bsr-doc-filter" data-filter="${label}">${__(label)} (${count})</button>`;
		})
		.join(" ");

	const rows = visible
		.map((v) => {
			const deletable =
				v.link_type === "Created" &&
				v.voucher_status === "Draft" &&
				!(frm.doc.lines[v.line_no - 1] || {}).bank_transaction;
			return `<tr>
				<td>${
					deletable
						? `<input type="checkbox" class="bsr-pick" data-type="${esc(v.voucher_type)}" data-name="${esc(v.voucher_no)}">`
						: ""
				}</td>
				<td>${v.line_no}</td>
				<td>${__(v.link_type)}</td>
				<td>${esc(v.voucher_type)}</td>
				<td>${voucher_link(v.voucher_type, v.voucher_no)}</td>
				<td style="word-break: break-all;">${esc(v.reference_no || "")}${
					v.voucher_status === "Draft"
						? ` <a class="bsr-edit-ref" title="${__("Edit")}" data-type="${esc(v.voucher_type)}" data-name="${esc(
								v.voucher_no
						  )}" data-ref="${esc(v.reference_no || "")}">✎</a>`
						: ""
				}</td>
				<td>${esc(v.party || "")}</td>
				<td>${v.posting_date ? frappe.datetime.str_to_user(v.posting_date) : ""}</td>
				<td class="text-right">${format_currency(v.amount, frm.doc.currency)}</td>
				<td><span class="indicator-pill ${BSR_VOUCHER_COLORS[v.voucher_status] || "gray"}">${__(v.voucher_status)}</span></td>
				<td style="word-break: break-word;">${esc(v.settles || "")}</td>
				<td>
					<button class="btn btn-xs btn-default bsr-open" data-type="${esc(v.voucher_type)}" data-name="${esc(
				v.voucher_no
			)}">${__("Open")}</button>
					${
						deletable
							? `<button class="btn btn-xs btn-success bsr-submit" data-type="${esc(v.voucher_type)}" data-name="${esc(
									v.voucher_no
							  )}">${__("Submit")}</button>`
							: ""
					}
					${
						deletable
							? `<button class="btn btn-xs btn-danger bsr-delete" data-type="${esc(v.voucher_type)}" data-name="${esc(
									v.voucher_no
							  )}">${__("Delete")}</button>`
							: ""
					}
				</td>
			</tr>`;
		})
		.join("");

	const draft_count = visible.filter(
		(v) => v.link_type === "Created" && v.voucher_status === "Draft" && !(frm.doc.lines[v.line_no - 1] || {}).bank_transaction
	).length;
	$wrapper.html(`
		<div class="flex justify-between align-center flex-wrap" style="gap: 8px; margin-bottom: 10px;">
			<div>${pills}
				${
					draft_count
						? `<button class="btn btn-xs btn-success bsr-submit-selected" disabled>${__("Submit Selected")}</button>
						<span class="text-muted small bsr-selected-info"></span>`
						: ""
				}
			</div>
			<div class="text-muted small">${__("Created from this review")}: <b>${format_currency(created_total, frm.doc.currency)}</b></div>
		</div>
		<div style="overflow-x: auto;">
			<table class="table table-bordered table-hover" style="font-size: 12px; margin: 0;">
				<thead><tr>
					<th style="width:28px">${
						draft_count ? `<input type="checkbox" class="bsr-pick-all" title="${__("Select all drafts")}">` : ""
					}</th>
					<th style="width:45px">${__("Line")}</th><th>${__("Link")}</th><th>${__("Type")}</th><th>${__("Document")}</th><th>${__("Cheque/Reference No")}</th>
					<th>${__("Party")}</th><th>${__("Posting Date")}</th><th class="text-right">${__("Amount")}</th>
					<th>${__("Status")}</th><th>${__("Settles")}</th><th style="width:180px"></th>
				</tr></thead>
				<tbody>${rows || `<tr><td colspan="12" class="text-muted text-center">${__("Nothing here.")}</td></tr>`}</tbody>
			</table>
		</div>`);

	const picked = () =>
		$wrapper
			.find(".bsr-pick:checked")
			.map((_, el) => ({ voucher_type: $(el).data("type"), voucher_no: String($(el).data("name")) }))
			.get();
	const update_selection = () => {
		const selected = picked();
		const total = selected.reduce(
			(n, s) => n + flt((all.find((v) => v.voucher_no === s.voucher_no) || {}).amount),
			0
		);
		$wrapper.find(".bsr-submit-selected").prop("disabled", !selected.length);
		$wrapper
			.find(".bsr-selected-info")
			.text(selected.length ? __("{0} selected · {1}", [selected.length, format_currency(total, frm.doc.currency)]) : "");
		$wrapper.find(".bsr-pick-all").prop("checked", selected.length && selected.length === $wrapper.find(".bsr-pick").length);
	};
	$wrapper.find(".bsr-pick").on("change", update_selection);
	$wrapper.find(".bsr-pick-all").on("change", function () {
		$wrapper.find(".bsr-pick").prop("checked", $(this).prop("checked"));
		update_selection();
	});
	$wrapper.find(".bsr-submit-selected").on("click", () => {
		const selected = picked();
		frappe.confirm(
			__("Submit {0} draft(s) in the background? They will be posted to the ledger.", [selected.length]),
			() =>
				frm
					.call({ method: "submit_drafts_in_background", doc: frm.doc, args: { vouchers: selected }, freeze: true })
					.then((r) =>
						frappe.show_alert({
							message: __("{0} draft(s) queued for submission - this page updates when done.", [r.message.queued]),
							indicator: "blue",
						})
					)
		);
	});

	$wrapper.find(".bsr-edit-ref").on("click", function () {
		const voucher_type = $(this).data("type");
		const voucher_no = String($(this).data("name"));
		frappe.prompt(
			{
				fieldtype: "Data",
				fieldname: "reference_no",
				label: __("Cheque/Reference No"),
				default: String($(this).data("ref") || ""),
				reqd: 1,
			},
			(values) =>
				call_doc(frm, "update_voucher_reference", {
					voucher_type,
					voucher_no,
					reference_no: values.reference_no,
				}).then(() => frappe.show_alert({ message: __("Reference updated on {0}", [voucher_no]), indicator: "green" })),
			__("Edit reference of {0}", [voucher_no]),
			__("Update")
		);
	});
	$wrapper.find(".bsr-doc-filter").on("click", function () {
		frm.bsr_doc_filter = $(this).data("filter");
		render_documents(frm);
	});
	$wrapper.find(".bsr-open").on("click", function () {
		frappe.set_route("Form", $(this).data("type"), String($(this).data("name")));
	});
	$wrapper.find(".bsr-submit").on("click", function () {
		const voucher = { voucher_type: $(this).data("type"), voucher_no: String($(this).data("name")) };
		frappe.confirm(__("Submit {0}? This posts it to the ledger.", [voucher.voucher_no]), () =>
			submit_drafts(frm, [voucher])
		);
	});
	$wrapper.find(".bsr-delete").on("click", function () {
		const voucher = { voucher_type: $(this).data("type"), voucher_no: String($(this).data("name")) };
		frappe.confirm(__("Delete draft {0}?", [voucher.voucher_no]), () => delete_drafts(frm, [voucher]));
	});
}

// ------------------------------------------------------------- review dialog

function open_review_dialog(frm, line) {
	const remaining = line_amount(line) - sum_vouchers(frm, line);
	const direction = flt(line.withdrawal) ? __("Withdrawal") : __("Deposit");
	const options = parse_json_list(line.alternatives);
	const plan = split_plan(line);
	const settle_types = flt(line.withdrawal)
		? "\nPurchase Invoice\nPurchase Order\nExpense Claim"
		: "\nSales Invoice\nSales Order";

	const header = `
		<div class="small" style="margin-bottom: 8px;">
			<b>${frappe.datetime.str_to_user(line.transaction_date)}</b> · ${direction}
			<b>${format_currency(line_amount(line), line.currency)}</b>
			${
				Math.abs(remaining - line_amount(line)) > 0.005
					? ` · ${__("left to book")}: <b>${format_currency(remaining, line.currency)}</b>`
					: ""
			}<br>
			${esc(line.particulars)}<br>
			<span class="text-muted">${__("Reference")}: ${esc(line.reference || "-")} ·
			${__("Counterparty")}: ${esc(line.counterparty || "-")}</span>
		</div>
		${line.suggestion_source ? `<div class="small text-muted">${__("Why")}: ${esc(line.suggestion_source)}</div>` : ""}`;

	const options_html = options.length
		? `<div class="small"><b>${__("Existing vouchers that could book this line")}</b> — ${esc(line.match_basis || "")}
			<table class="table table-bordered" style="margin-top: 6px;">
			${options
				.map(
					(option, index) => `<tr>
					<td>${option
						.map(
							(c) =>
								`${voucher_link(c.voucher_type, c.voucher_no)} · ${frappe.datetime.str_to_user(
									c.posting_date
								)} · ${format_currency(c.amount, line.currency)} · ${esc(c.party || c.reference || "")}`
						)
						.join("<br>")}</td>
					<td style="width:90px"><button class="btn btn-xs btn-primary bsr-use" data-index="${index}">${
						option.length > 1 ? __("Use these") : __("Use this")
					}</button></td></tr>`
				)
				.join("")}
			</table>${__("Or create new record(s) below if none of these is right.")}</div>`
		: "";

	const dialog = new frappe.ui.Dialog({
		title: __("Statement line {0}", [line.idx]),
		size: "extra-large",
		fields: [
			{ fieldtype: "HTML", fieldname: "header", options: header },
			{ fieldtype: "HTML", fieldname: "options", options: options_html },
			{
				fieldtype: "Check",
				fieldname: "use_split",
				label: __("Split this payment across several invoices / parties"),
				default: plan.length ? 1 : 0,
			},
			{ fieldtype: "Section Break", label: __("Record to create"), depends_on: "eval:!doc.use_split" },
			{
				fieldtype: "Select",
				fieldname: "suggested_doctype",
				label: __("Create"),
				options: "Payment Entry\nJournal Entry",
				default: line.suggested_doctype || "Journal Entry",
			},
			{
				fieldtype: "Select",
				fieldname: "party_type",
				label: __("Party Type"),
				options: "\nSupplier\nCustomer\nEmployee",
				default: line.party_type,
			},
			{ fieldtype: "Dynamic Link", fieldname: "party", label: __("Party"), options: "party_type", default: line.party },
			{ fieldtype: "Column Break" },
			{
				fieldtype: "Link",
				fieldname: "account",
				label: __("Contra Account"),
				options: "Account",
				default: line.account,
				depends_on: "eval:doc.suggested_doctype=='Journal Entry'",
				get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
			},
			{
				fieldtype: "Select",
				fieldname: "against_doctype",
				label: __("Settles"),
				options: settle_types,
				default: line.against_doctype,
				depends_on: "eval:doc.suggested_doctype=='Payment Entry'",
			},
			{
				fieldtype: "Dynamic Link",
				fieldname: "against_name",
				label: __("Document"),
				options: "against_doctype",
				default: line.against_name,
				depends_on: "eval:doc.suggested_doctype=='Payment Entry' && doc.against_doctype",
				get_query: () => against_query(frm, dialog.get_values(true)),
			},
			{ fieldtype: "Section Break", label: __("Split"), depends_on: "eval:doc.use_split" },
			{
				fieldtype: "HTML",
				fieldname: "split_help",
				options: `<p class="small text-muted">${__(
					"One Draft Payment Entry is created per party. Amounts must add up to {0}.",
					[format_currency(remaining, line.currency)]
				)} <span class="bsr-split-total"></span></p>`,
			},
			{
				fieldtype: "Table",
				fieldname: "split_rows",
				label: __("Split"),
				cannot_add_rows: false,
				in_place_edit: true,
				data: plan.map((r) => ({ ...r })),
				fields: [
					{
						fieldtype: "Select",
						fieldname: "party_type",
						label: __("Party Type"),
						options: "\nSupplier\nCustomer\nEmployee",
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldtype: "Dynamic Link",
						fieldname: "party",
						label: __("Party"),
						options: "party_type",
						in_list_view: 1,
						columns: 3,
						get_options: (control) => (control.doc || {}).party_type,
					},
					{
						fieldtype: "Select",
						fieldname: "against_doctype",
						label: __("Settles"),
						options: settle_types,
						in_list_view: 1,
						columns: 2,
					},
					{
						fieldtype: "Dynamic Link",
						fieldname: "against_name",
						label: __("Document"),
						options: "against_doctype",
						in_list_view: 1,
						columns: 2,
						get_options: (control) => (control.doc || {}).against_doctype,
						get_query: (row) => against_query(frm, row || {}),
					},
					{ fieldtype: "Currency", fieldname: "amount", label: __("Amount"), in_list_view: 1, columns: 1 },
				],
			},
			{ fieldtype: "Section Break" },
			{
				fieldtype: "Link",
				fieldname: "mode_of_payment",
				label: __("Mode of Payment"),
				options: "Mode of Payment",
				default: line.mode_of_payment,
			},
			{
				fieldtype: "Currency",
				fieldname: "tds_amount",
				label: __("TDS Deducted by Customer"),
				default: line.tds_amount,
				depends_on: `eval:!doc.use_split && doc.suggested_doctype=='Payment Entry' && ${flt(line.deposit) > 0}`,
				description: __("Booked as a deduction on the Payment Entry; the invoice is settled for bank amount + TDS."),
			},
			{
				fieldtype: "Link",
				fieldname: "tds_account",
				label: __("TDS Account"),
				options: "Account",
				default: line.tds_account,
				depends_on: "eval:doc.tds_amount",
				get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
			},
			{ fieldtype: "Column Break" },
			{ fieldtype: "Small Text", fieldname: "remarks", label: __("Remarks") },
			{ fieldtype: "Section Break", label: __("Or link an existing submitted voucher"), collapsible: 1 },
			{
				fieldtype: "Select",
				fieldname: "link_voucher_type",
				label: __("Voucher Type"),
				options: "Payment Entry\nJournal Entry",
				default: "Payment Entry",
			},
			{ fieldtype: "Column Break" },
			{
				fieldtype: "Dynamic Link",
				fieldname: "link_voucher",
				label: __("Voucher"),
				options: "link_voucher_type",
				get_query: () => ({ filters: { docstatus: 1, company: frm.doc.company } }),
			},
			{
				fieldtype: "Button",
				fieldname: "link_button",
				label: __("Link this voucher"),
				click: () => {
					const { link_voucher_type, link_voucher } = dialog.get_values(true);
					if (!link_voucher) return frappe.msgprint(__("Pick a voucher first."));
					run(
						call_doc(frm, "accept_voucher", {
							row_name: line.name,
							voucher_type: link_voucher_type,
							voucher_no: link_voucher,
						}),
						__("Linked {0}", [link_voucher])
					);
				},
			},
		],
		primary_action_label: __("Create Draft(s)"),
		primary_action() {
			const values = collect(dialog);
			if (values.use_split && !check_split_total(values.split_plan)) return;
			run(call_doc(frm, "create_draft", { row_name: line.name, values }), __("Draft(s) created"));
		},
		secondary_action_label: __("Open in Form"),
		secondary_action() {
			const values = collect(dialog);
			if (values.use_split) {
				frappe.msgprint(__("A split creates several documents - use Create Draft(s) instead."));
				return;
			}
			frm.call({ method: "get_unsaved_voucher", doc: frm.doc, args: { row_name: line.name, values }, freeze: true }).then(
				(r) => {
					dialog.hide();
					const doc = frappe.model.sync(r.message)[0];
					frappe.set_route("Form", doc.doctype, doc.name);
				}
			);
		},
	});

	function run(promise, message) {
		promise.then(() => {
			dialog.hide();
			frappe.show_alert({ message, indicator: "green" });
		});
	}

	function check_split_total(rows) {
		const total = rows.reduce((n, r) => n + flt(r.amount), 0);
		if (Math.abs(total - remaining) > 0.005) {
			frappe.msgprint(
				__("The split adds up to {0}, but {1} is left to book on this line.", [
					format_currency(total, line.currency),
					format_currency(remaining, line.currency),
				])
			);
			return false;
		}
		return true;
	}

	dialog.add_custom_action(__("Save Suggestion"), () => {
		run(call_doc(frm, "save_line_values", { row_name: line.name, values: collect(dialog) }), __("Saved"));
	});
	dialog.$wrapper.find(".bsr-use").on("click", function () {
		run(call_doc(frm, "accept_option", { row_name: line.name, option_index: $(this).data("index") }), __("Linked"));
	});
	dialog.show();
}

function collect(dialog) {
	const values = dialog.get_values(true) || {};
	const out = {};
	[
		"suggested_doctype",
		"party_type",
		"party",
		"account",
		"against_doctype",
		"against_name",
		"mode_of_payment",
		"tds_account",
		"remarks",
	].forEach((field) => (out[field] = values[field] || null));
	out.tds_amount = flt(values.tds_amount) || 0;
	if (out.suggested_doctype === "Payment Entry") out.account = null;
	else {
		out.against_doctype = out.against_name = out.tds_account = null;
		out.tds_amount = 0;
	}

	out.use_split = !!values.use_split;
	out.split_plan = out.use_split
		? (values.split_rows || [])
				.filter((r) => r.party && flt(r.amount))
				.map((r) => ({
					party_type: r.party_type,
					party: r.party,
					against_doctype: r.against_name ? r.against_doctype : null,
					against_name: r.against_name || null,
					amount: flt(r.amount),
				}))
		: null;
	return out;
}

function against_query(frm, values) {
	// Server-side search that shows each document's party, date, outstanding and total.
	return {
		query: "alfaedge_finance.alfaedge_finance.bank_statement.link_queries.search_settleable",
		filters: { company: frm.doc.company, party: values.party || null },
	};
}

// --------------------------------------------------------- bulk payment file

function upload_bulk_file(frm, line) {
	const dialog = new frappe.ui.Dialog({
		title: __("Bulk payment file for line {0}", [line.idx]),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "file_input",
				options: `<p class="small text-muted">${__(
					"The bank upload file for this payment ({0}, {1}) - e.g. the one the Bulk Payment CSV page generated. Needs a 'Beneficiary Account Number' column.",
					[format_currency(line_amount(line), line.currency), esc(line.particulars)]
				)}</p>
				<input type="file" accept=".xlsx,.xls,.csv" class="form-control" id="bsr-bulk-input">`,
			},
		],
		primary_action_label: __("Upload & Match"),
		primary_action() {
			const file = (dialog.$wrapper.find("#bsr-bulk-input")[0].files || [])[0];
			if (!file) return frappe.msgprint(__("Choose the bulk payment file."));
			const reader = new FileReader();
			reader.onload = () => {
				frm.call({
					method: "upload_bulk_file",
					doc: frm.doc,
					args: { row_name: line.name, filename: file.name, content: reader.result.split(",")[1] },
					freeze: true,
					freeze_message: __("Matching beneficiaries..."),
				}).then((r) => {
					dialog.hide();
					const result = r.message || {};
					frappe.show_alert({ message: esc(result.summary), indicator: "green" }, 8);
					if ((result.warnings || []).length) {
						frappe.msgprint({ title: __("Check"), message: result.warnings.map(esc).join("<br>"), indicator: "orange" });
					}
					frm.reload_doc().then(() => {
						const fresh = (frm.doc.lines || []).find((l) => l.name === line.name);
						if (fresh) open_bulk_dialog(frm, fresh);
					});
				});
			};
			reader.readAsDataURL(file);
		},
	});
	dialog.show();
}

function open_bulk_dialog(frm, line) {
	const rows = bulk_plan(line);
	const remaining = line_amount(line) - sum_vouchers(frm, line);
	const kinds = "expense_claim\nsalary\npurchase_invoice\npurchase_order\non_account";
	const dialog = new frappe.ui.Dialog({
		title: __("Bulk payment - line {0}", [line.idx]),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "header",
				options: `<div class="small">
					<b>${frappe.datetime.str_to_user(line.transaction_date)}</b> · ${esc(line.particulars)} ·
					<b>${format_currency(remaining, line.currency)}</b>
					${line.bulk_count ? " · " + __("{0} beneficiaries per bank", [line.bulk_count]) : ""}
					${line.bulk_file ? ` · <a href="${encodeURI(line.bulk_file)}" target="_blank">${__("uploaded file")}</a>` : ""}
					<div class="text-muted">${__(
						"Each row is matched to its Employee / Supplier by bank account number, then to the open documents that add up to it exactly. Fix any Unresolved or Mismatch row (party, kind, or documents as a comma-separated list), then Re-check. One Draft Journal Entry is created for the whole payment."
					)}</div></div>`,
			},
			{
				fieldtype: "Table",
				fieldname: "rows",
				label: __("Beneficiaries"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				in_place_edit: true,
				data: rows.map((r) => ({
					row_no: r.row_no,
					beneficiary_name: r.beneficiary_name,
					account_no: r.account_no,
					amount: r.amount,
					party_type: r.party_type,
					party: r.party,
					kind: r.kind,
					documents: (r.documents || []).map((d) => d.name).join(", "),
					status: r.status,
					note: r.note,
				})),
				fields: [
					{ fieldtype: "Data", fieldname: "beneficiary_name", label: __("Beneficiary"), in_list_view: 1, read_only: 1, columns: 2 },
					{ fieldtype: "Currency", fieldname: "amount", label: __("Amount"), in_list_view: 1, read_only: 1, columns: 1 },
					{ fieldtype: "Select", fieldname: "party_type", label: __("Party Type"), options: "\nEmployee\nSupplier", in_list_view: 1, columns: 1 },
					{
						fieldtype: "Dynamic Link",
						fieldname: "party",
						label: __("Party"),
						options: "party_type",
						in_list_view: 1,
						columns: 2,
						get_options: (control) => (control.doc || {}).party_type,
					},
					{ fieldtype: "Select", fieldname: "kind", label: __("Pays"), options: kinds, in_list_view: 1, columns: 1 },
					{ fieldtype: "Data", fieldname: "documents", label: __("Documents"), in_list_view: 1, columns: 2 },
					{ fieldtype: "Data", fieldname: "status", label: __("Status"), in_list_view: 1, read_only: 1, columns: 1 },
					{ fieldtype: "Data", fieldname: "account_no", label: __("Account No."), read_only: 1 },
					{ fieldtype: "Small Text", fieldname: "note", label: __("Note"), read_only: 1 },
					{ fieldtype: "Int", fieldname: "row_no", label: __("Row"), read_only: 1 },
				],
			},
			{ fieldtype: "HTML", fieldname: "notes", options: bulk_notes_html(rows) },
		],
		primary_action_label: __("Create Draft Journal Entry"),
		primary_action() {
			save_plan().then(() => {
				const fresh = bulk_plan((frm.doc.lines || []).find((l) => l.name === line.name) || line);
				if (fresh.some((r) => ["Mismatch", "Unresolved"].includes(r.status))) {
					frappe.msgprint(__("Fix the Unresolved / Mismatch rows first."));
					return open_bulk_dialog(frm, (frm.doc.lines || []).find((l) => l.name === line.name));
				}
				call_doc(frm, "create_draft", { row_name: line.name }).then((vouchers) => {
					frappe.show_alert({ message: __("Draft {0} created", [vouchers.map((v) => v.name).join(", ")]), indicator: "green" });
				});
			});
		},
		secondary_action_label: __("Re-check"),
		secondary_action() {
			save_plan().then(() => open_bulk_dialog(frm, (frm.doc.lines || []).find((l) => l.name === line.name)));
		},
	});

	function save_plan() {
		const edited = (dialog.get_values(true).rows || []).map((r) => ({
			row_no: r.row_no,
			beneficiary_name: r.beneficiary_name,
			account_no: r.account_no,
			amount: flt(r.amount),
			party_type: r.party_type || null,
			party: r.party || null,
			kind: r.kind || "on_account",
			documents: (r.documents || "")
				.split(",")
				.map((name) => name.trim())
				.filter(Boolean)
				.map((name) => ({ name })),
		}));
		dialog.hide();
		return frm
			.call({ method: "save_bulk_plan", doc: frm.doc, args: { row_name: line.name, plan: edited }, freeze: true })
			.then(() => frm.reload_doc());
	}

	dialog.add_custom_action(__("Upload another file"), () => {
		dialog.hide();
		upload_bulk_file(frm, line);
	});
	dialog.add_custom_action(__("Remove bulk file"), () => {
		dialog.hide();
		call_doc(frm, "clear_bulk_plan", { row_name: line.name });
	});
	dialog.show();
}

function bulk_notes_html(rows) {
	const notes = rows.filter((r) => r.note);
	if (!notes.length) return "";
	return `<div class="small">${notes
		.map(
			(r) =>
				`<div><span class="indicator-pill ${BSR_BULK_COLORS[r.status] || "gray"}" style="font-size:10px">${__(r.status)}</span> ${__(
					"Row {0}",
					[r.row_no]
				)} (${esc(r.beneficiary_name || r.account_no)}): ${esc(r.note)}</div>`
		)
		.join("")}</div>`;
}

// ------------------------------------------------------------------ advances

function open_advance_dialog(frm, lines) {
	if (!lines.length) return;
	const single = lines.length === 1;
	const first = lines[0];
	const outgoing = lines.every((l) => flt(l.withdrawal));
	const default_type = first.party_type || (outgoing ? "Supplier" : "Customer");
	const without_party = lines.filter((l) => !l.party);
	const total = lines.reduce((n, l) => n + line_amount(l), 0);

	const summary = single
		? `<div class="small"><b>${frappe.datetime.str_to_user(first.transaction_date)}</b> · ${format_currency(
				line_amount(first),
				first.currency
		  )}<br>${esc(first.particulars)}</div>`
		: `<div class="small">${__("{0} lines · {1}", [lines.length, format_currency(total, frm.doc.currency)])}
			<table class="table table-bordered" style="margin-top:6px;">${lines
				.map(
					(l) => `<tr><td>${l.idx}</td><td>${esc(l.particulars)}</td><td class="text-right">${format_currency(
						line_amount(l),
						l.currency
					)}</td><td>${l.party ? esc(l.party) : `<span class="text-muted">${__("party below")}</span>`}</td></tr>`
				)
				.join("")}</table>
			${
				without_party.length
					? `<span class="text-muted">${__(
							"Lines without a detected party use the party chosen below; the others keep their own."
					  )}</span>`
					: ""
			}</div>`;

	const dialog = new frappe.ui.Dialog({
		title: single ? __("Mark line {0} as Advance", [first.idx]) : __("Mark {0} lines as Advance", [lines.length]),
		size: "large",
		fields: [
			{ fieldtype: "HTML", fieldname: "summary", options: summary },
			{
				fieldtype: "Select",
				fieldname: "party_type",
				label: __("Party Type"),
				options: "Supplier\nCustomer\nEmployee",
				default: default_type,
				reqd: 1,
			},
			{
				fieldtype: "Dynamic Link",
				fieldname: "party",
				label: __("Party"),
				options: "party_type",
				default: single ? first.party : null,
				reqd: single || without_party.length ? 1 : 0,
			},
			{ fieldtype: "Column Break" },
			{
				fieldtype: "Select",
				fieldname: "against_doctype",
				label: __("Against Order (optional)"),
				options: "\nPurchase Order\nSales Order",
				depends_on: `eval:${single} && doc.party_type != 'Employee'`,
			},
			{
				fieldtype: "Dynamic Link",
				fieldname: "against_name",
				label: __("Order"),
				options: "against_doctype",
				depends_on: `eval:${single} && doc.against_doctype`,
				get_query: () => against_query(frm, dialog.get_values(true)),
			},
			{
				fieldtype: "Data",
				fieldname: "reference",
				label: __("Proforma / Reference (optional)"),
				description: __("E.g. the proforma invoice number - goes into the Payment Entry remarks."),
			},
			{ fieldtype: "Section Break" },
			{
				fieldtype: "Check",
				fieldname: "remember",
				label: __("Always treat payments to this payee as advances"),
				description: __(
					"Saves a Bank Narration Rule, so future payments to them are suggested as advances (e.g. prepaid usage)."
				),
			},
		],
		primary_action_label: single ? __("Create Advance Draft") : __("Create Advance Drafts"),
		primary_action(values) {
			frm.call({
				method: "mark_as_advance",
				doc: frm.doc,
				args: { row_names: lines.map((l) => l.name), values },
				freeze: true,
				freeze_message: __("Creating advance drafts..."),
			}).then((r) => {
				dialog.hide();
				const result = r.message || { created: [], failed: [] };
				if (result.failed.length) {
					frappe.msgprint({
						title: __("{0} created, {1} failed", [result.created.length, result.failed.length]),
						message: result.failed.map(esc).join("<br>"),
						indicator: "orange",
					});
				} else {
					frappe.show_alert({
						message: __("Advance draft(s) created: {0}", [result.created.join(", ")]),
						indicator: "green",
					});
				}
				frm.reload_doc();
			});
		},
	});
	dialog.show();
}
