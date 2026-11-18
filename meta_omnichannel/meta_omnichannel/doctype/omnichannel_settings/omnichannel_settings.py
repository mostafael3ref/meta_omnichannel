# Copyright (c) 2026, Meta Omnichannel and contributors
# License: MIT. See license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from meta_omnichannel.meta_connectors.ssrf import ConnectorURLError, validate_connector_base_url


class OmnichannelSettings(Document):
	def validate(self):
		self.connector_base_url = (self.connector_base_url or "").strip().rstrip("/")
		self.connector_tenant_id = (self.connector_tenant_id or "").strip()

		if not self.connector_base_url:
			frappe.throw(_("Connector Base URL is required"))

		if not self.connector_tenant_id:
			frappe.throw(_("Connector Tenant ID is required"))

		try:
			validate_connector_base_url(self.connector_base_url)
		except ConnectorURLError as exc:
			frappe.throw(_("Invalid Connector Base URL: {0}").format(str(exc)))
