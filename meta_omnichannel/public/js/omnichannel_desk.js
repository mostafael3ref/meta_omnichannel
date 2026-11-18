// Desk shortcut: open Unified Inbox
frappe.provide("meta_omnichannel");

meta_omnichannel.open_inbox = function () {
	window.open("/omnichannel", "_blank");
};
