// Copyright (c) 2026, Meta Omnichannel and contributors
// License: MIT. See license.txt

frappe.ui.form.on("Omnichannel Settings", {
	refresh(frm) {
		const can_admin =
			frappe.user.has_role("System Manager") ||
			frappe.user.has_role("Omnichannel Administrator");

		if (!can_admin) {
			return;
		}

		frm.add_custom_button(__("Open Inbox"), () => {
			window.open("/omnichannel", "_blank");
		});

		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({
				method: "meta_omnichannel.api.connector.test_connector_connection",
				freeze: true,
				freeze_message: __("Testing connector connection..."),
				callback(r) {
					frm.reload_doc();
					const message =
						(r && r.message && r.message.message) || __("Connection test finished");
					frappe.show_alert({
						message,
						indicator: r && r.message && r.message.ok ? "green" : "red",
					});
				},
			});
		});

		frm.add_custom_button(__("Connect Meta Account"), () => {
			frappe.call({
				method: "meta_omnichannel.onboarding.service.begin_meta_connect",
				freeze: true,
				callback(r) {
					const msg = (r && r.message && r.message.message) || __("Onboarding pending");
					frappe.msgprint({
						title: __("Meta Onboarding"),
						indicator: "orange",
						message: msg,
					});
				},
			});
		});
	},
});
