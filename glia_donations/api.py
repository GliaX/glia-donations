"""Guest-facing endpoints for recurring donations (no login required)."""

from __future__ import annotations

import frappe

from glia_donations import recurring


@frappe.whitelist(allow_guest=True)
def start_recurring_donation(email, full_name=None, amount=0, currency=None, campaign=None,
                             frequency="Monthly"):
	"""Create the subscription checkout and return the Stripe URL."""
	return recurring.start(
		email=email,
		full_name=full_name,
		amount=amount,
		currency=currency,
		campaign=campaign,
		frequency=frequency,
	)


@frappe.whitelist(allow_guest=True)
def recurring_donation_status(token):
	doc = recurring.by_token(token)
	if not doc:
		frappe.throw("This donation link is not valid.")
	return recurring.donor_view(doc)


@frappe.whitelist(allow_guest=True)
def cancel_recurring_donation(token, reason=None):
	return recurring.cancel(token, reason)
