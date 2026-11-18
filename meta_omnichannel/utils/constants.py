"""Shared constants for Meta Omnichannel CRM."""

from __future__ import annotations

APP_NAME = "meta_omnichannel"
APP_TITLE = "Meta Omnichannel CRM"

CHANNEL_MESSENGER = "messenger"
CHANNEL_INSTAGRAM = "instagram"
CHANNEL_WHATSAPP = "whatsapp"

SUPPORTED_CHANNELS = (
	CHANNEL_MESSENGER,
	CHANNEL_INSTAGRAM,
	CHANNEL_WHATSAPP,
)

TARGET_FRAPPE_MAJOR = 15
TARGET_FRAPPE_CRM_APP = "crm"
TARGET_FRAPPE_CRM = "1.91.1"

# External FastAPI connector (Phase 1)
DEFAULT_CONNECTOR_BASE_URL = "https://connect.tarteebagency.com"
CONNECTOR_API_PREFIX = "/api"
CONNECTOR_TEST_PATH = "/api/facebook/conversations"
OMNICHANNEL_SETTINGS_DOCTYPE = "Omnichannel Settings"
OMNICHANNEL_ADMIN_ROLE = "Omnichannel Administrator"

# Code-level hostname allowlist (NOT configurable via Omnichannel Settings).
# Changing the connector host requires an application release / code change.
ALLOWED_CONNECTOR_HOSTS = frozenset(
	{
		"connect.tarteebagency.com",
	}
)
