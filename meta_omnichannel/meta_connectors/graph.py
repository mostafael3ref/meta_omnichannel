"""Meta Graph API client stub (not used in Phase 1)."""

from __future__ import annotations


class MetaGraphClient:
	"""Placeholder for future direct Graph API usage.

	Phase 1 talks to the external FastAPI connector only.
	"""

	def __init__(self, *, access_token: str | None = None, api_version: str | None = None) -> None:
		self.access_token = access_token
		self.api_version = api_version

	def request(self, method: str, path: str, **kwargs):  # noqa: ARG002
		raise NotImplementedError(
			"MetaGraphClient.request is not implemented; use ConnectorClient for Phase 1"
		)
