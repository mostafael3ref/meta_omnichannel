"""HTTPS client for the external Omnichannel FastAPI connector.

Authentication: ``X-API-Key`` header only.
Redirects: disabled (fail closed on 3xx).
Transport: DNS-resolved IP pinning with hostname TLS (SNI/Host/verify).
Proxies: disabled (``trust_env=False``).
Secrets: never logged or attached to raised error messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorRateLimitError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.meta_connectors.pinned_http import (
	assert_tls_verify_enabled,
	build_pinned_request_url,
	create_pinned_session,
	pinned_request_headers,
)
from meta_omnichannel.meta_connectors.ssrf import (
	resolve_safe_destination,
	validate_connector_base_url,
)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
TEST_CONVERSATIONS_PATH = "/api/facebook/conversations"

# Explicitly disable proxies on every call (defense in depth vs session mutation).
_NO_PROXIES = {"http": None, "https": None}


@dataclass(frozen=True)
class ConnectorTestResult:
	ok: bool
	status_code: int | None
	connection_status: str
	message: str


class ConnectorClient:
	"""Minimal connector HTTP client for Phase 1 connectivity testing."""

	def __init__(
		self,
		*,
		base_url: str,
		api_key: str,
		tenant_id: str | None = None,
		connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
		read_timeout: float = DEFAULT_READ_TIMEOUT,
		session: requests.Session | None = None,
	) -> None:
		if not api_key or not str(api_key).strip():
			raise ConnectorError("Connector API key is not configured", connection_status="Misconfigured")

		# Syntax + allowlist + DNS safety check at construction (no connection yet).
		self.base_url = validate_connector_base_url(base_url)
		self._api_key = str(api_key).strip()
		self.tenant_id = (tenant_id or "").strip() or None
		self.timeout = (connect_timeout, read_timeout)
		# Optional injected session is for tests only; production uses pinned sessions.
		self._injected_session = session

	def _headers(self) -> dict[str, str]:
		# Auth contract for Phase 1: X-API-Key only (tenant id is site-local config).
		return {
			"Accept": "application/json",
			"X-API-Key": self._api_key,
		}

	def request(
		self,
		method: str,
		path: str,
		*,
		params: dict[str, Any] | None = None,
		json_body: dict[str, Any] | None = None,
		data: dict[str, Any] | None = None,
		files: Any | None = None,
	) -> requests.Response:
		"""Perform a connector request to a DNS-pinned, validated destination IP.

		``json_body`` sets ``Content-Type: application/json``. Multipart uploads pass
		``files`` (and optional form ``data``); do not set Content-Type so ``requests``
		generates the multipart boundary. ``json_body`` and ``files`` are mutually exclusive.
		"""
		if json_body is not None and files is not None:
			raise ConnectorError(
				"Connector request cannot combine json_body with multipart files",
				connection_status="Misconfigured",
			)

		destination = resolve_safe_destination(self.base_url)
		pinned_url = build_pinned_request_url(destination, path)
		base_headers = self._headers()
		# Multipart: never force Content-Type — requests must set boundary.
		if json_body is not None:
			base_headers = {**base_headers, "Content-Type": "application/json"}
		headers = pinned_request_headers(destination, base_headers)

		session = self._injected_session or create_pinned_session(destination.hostname)
		if self._injected_session is None:
			# Fresh session: trust_env already False; reinforce proxy disable.
			session.trust_env = False
			session.proxies = dict(_NO_PROXIES)

		request_kwargs: dict[str, Any] = {
			"method": method.upper(),
			"url": pinned_url,
			"headers": headers,
			"params": params,
			"timeout": self.timeout,
			"allow_redirects": False,
			"verify": True,
			"proxies": dict(_NO_PROXIES),
		}
		if json_body is not None:
			request_kwargs["json"] = json_body
		if data is not None:
			request_kwargs["data"] = data
		if files is not None:
			request_kwargs["files"] = files
		assert_tls_verify_enabled(request_kwargs)

		try:
			response = session.request(**request_kwargs)
		except ConnectorError:
			raise
		except requests.Timeout as exc:
			raise ConnectorUnreachableError("Connector request timed out") from exc
		except requests.ConnectionError as exc:
			raise ConnectorUnreachableError("Could not reach the connector") from exc
		except requests.RequestException as exc:
			raise ConnectorUnreachableError("Connector request failed") from exc
		finally:
			if self._injected_session is None:
				session.close()

		if 300 <= response.status_code < 400:
			raise ConnectorError(
				"Connector returned a redirect, which is not allowed",
				connection_status="Error",
			)

		return response

	def test_connection(self) -> ConnectorTestResult:
		"""Probe connector auth using GET /api/facebook/conversations?limit=1&offset=0."""
		response = self.request(
			"GET",
			TEST_CONVERSATIONS_PATH,
			params={"limit": 1, "offset": 0},
		)
		return interpret_test_response(response.status_code)


def interpret_test_response(status_code: int) -> ConnectorTestResult:
	"""Map HTTP status codes from the connectivity probe to safe results."""
	if status_code == 200:
		return ConnectorTestResult(
			ok=True,
			status_code=200,
			connection_status="Connected",
			message="Connector is reachable and the API key was accepted.",
		)
	if status_code == 401:
		return ConnectorTestResult(
			ok=False,
			status_code=401,
			connection_status="Authentication Failed",
			message="Invalid or missing API key (HTTP 401).",
		)
	if status_code == 403:
		return ConnectorTestResult(
			ok=False,
			status_code=403,
			connection_status="Forbidden",
			message="Authorization failure (HTTP 403).",
		)
	if status_code == 429:
		return ConnectorTestResult(
			ok=False,
			status_code=429,
			connection_status="Rate Limited",
			message="Connector rate limited the request (HTTP 429).",
		)
	if 500 <= status_code <= 599:
		return ConnectorTestResult(
			ok=False,
			status_code=status_code,
			connection_status="Server Error",
			message=f"Connector server error (HTTP {status_code}).",
		)

	return ConnectorTestResult(
		ok=False,
		status_code=status_code,
		connection_status="Error",
		message=f"Unexpected connector response (HTTP {status_code}).",
	)


def raise_for_connector_status(status_code: int) -> None:
	"""Raise typed errors for non-success connector statuses (for future call sites)."""
	result = interpret_test_response(status_code)
	if result.ok:
		return
	mapping = {
		"Authentication Failed": ConnectorAuthError,
		"Forbidden": ConnectorForbiddenError,
		"Rate Limited": ConnectorRateLimitError,
		"Server Error": ConnectorServerError,
		"Unreachable": ConnectorUnreachableError,
	}
	exc_type = mapping.get(result.connection_status, ConnectorError)
	raise exc_type(result.message)
