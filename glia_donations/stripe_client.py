"""Thin Stripe wrapper.

Keys come from the same place the ERPNext Stripe gateway reads them — the
`Stripe Settings` doctype record (a regular doctype with a single record named
"Stripe Settings"). A site-config fallback exists so a fresh site can be booted
with `stripe_secret_key` in site_config before the doctype record is created.
"""

from __future__ import annotations

import frappe
import stripe

_configured = False


def _settings_record():
	"""Stripe Settings is autonamed by gateway_name; the ERPNext Stripe gateway
	looks it up via Payment Gateway.gateway_controller, which is "Stripe"."""
	controller = frappe.db.get_value("Payment Gateway", {"gateway": "Stripe"}, "gateway_controller")
	for candidate in (controller, "Stripe", "Stripe Settings"):
		if candidate and frappe.db.exists("Stripe Settings", candidate):
			return frappe.get_doc("Stripe Settings", candidate)
	name = frappe.db.get_value("Stripe Settings", {}, "name")
	return frappe.get_doc("Stripe Settings", name) if name else None


def _secret_key() -> str:
	record = _settings_record()
	key = record.get_password("secret_key", raise_exception=False) if record else None
	if not key:
		key = frappe.conf.get("stripe_secret_key")
	if not key:
		frappe.throw(
			"Stripe is not configured: set the secret key in Stripe Settings "
			"(ERPNext) so recurring donations can be charged."
		)
	return key


def configure():
	"""Point the SDK at our key (idempotent)."""
	global _configured
	if _configured:
		return stripe
	stripe.api_key = _secret_key()
	stripe.max_network_retries = 2
	_configured = True
	return stripe


def is_live() -> bool:
	key = stripe.api_key if getattr(stripe, "api_key", None) else _secret_key()
	return str(key).startswith("sk_live")


def reset():
	"""Forget the configured key (used after Stripe Settings change / in tests)."""
	global _configured
	_configured = False
	stripe.api_key = None
