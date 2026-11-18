# Copyright (c) 2026, Meta Omnichannel and contributors
# License: MIT. See license.txt

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/omnichannel"
		raise frappe.Redirect

	from meta_omnichannel.permissions import user_is_omnichannel_agent

	if not user_is_omnichannel_agent():
		frappe.throw(_("Not permitted to access Omnichannel Inbox"), frappe.PermissionError)

	context.boot = {
		"user": frappe.session.user,
		"lang": frappe.local.lang,
		"sitename": frappe.local.site,
		"csrf_token": frappe.sessions.get_csrf_token(),
	}
	return context
