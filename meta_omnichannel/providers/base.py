"""Channel provider contracts. External connector methods must be confirmed before enabling."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderCapability:
	LIST_CONVERSATIONS = "list_conversations"
	GET_MESSAGES = "get_messages"
	SEND_TEXT = "send_text"
	SEND_MEDIA = "send_media"
	SEND_TEMPLATE = "send_template"
	OAUTH_CONNECT = "oauth_connect"


class ChannelProvider(ABC):
	provider_name: str
	# Confirmed connector paths (None = unavailable)
	capabilities: dict[str, str | None]

	@abstractmethod
	def is_external_enabled(self) -> bool:
		"""True only when connector API contracts are confirmed and wired."""

	def missing_contracts(self) -> list[str]:
		return [name for name, path in self.capabilities.items() if not path]

	def status(self) -> dict[str, Any]:
		return {
			"provider": self.provider_name,
			"external_enabled": self.is_external_enabled(),
			"capabilities": self.capabilities,
			"missing_contracts": self.missing_contracts(),
		}
