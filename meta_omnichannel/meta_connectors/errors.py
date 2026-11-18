"""Connector client error types (safe messages only — never include secrets)."""

from __future__ import annotations


class ConnectorError(Exception):
	"""Base connector error with a user-safe message."""

	def __init__(self, message: str, *, connection_status: str = "Error") -> None:
		super().__init__(message)
		self.message = message
		self.connection_status = connection_status


class ConnectorURLError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Misconfigured")


class ConnectorConfigError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Misconfigured")


class ConnectorAuthError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Authentication Failed")


class ConnectorForbiddenError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Forbidden")


class ConnectorRateLimitError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Rate Limited")


class ConnectorServerError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Server Error")


class ConnectorUnreachableError(ConnectorError):
	def __init__(self, message: str) -> None:
		super().__init__(message, connection_status="Unreachable")
