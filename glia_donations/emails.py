"""Receipt email, sent from a background worker.

The webhook must never fail because SMTP did: a failed send inside the request
(where Frappe flushes its email queue on commit) turned a successfully recorded
donation into a 500 and made Stripe retry. This runs on the worker instead, and
only logs on failure.
"""

from __future__ import annotations

import frappe
from frappe.utils import escape_html, flt, get_url


def send_receipt_email(donation_name: str):
	donation = frappe.get_doc("Donation", donation_name)
	recurring_name = donation.get("recurring_donation")
	if not recurring_name:
		return
	recurring = frappe.get_doc("Recurring Donation", recurring_name)
	if not recurring.email:
		return

	amount = f"{donation.currency} {flt(donation.amount):.2f}"
	manage_url = f"{get_url()}/recurring_donation?token={recurring.cancel_token}"
	message = f"""
	<div style="font-family:Ubuntu,Arial,sans-serif;color:#032e38;max-width:560px">
	  <h2 style="margin:0 0 12px">Thank you, {escape_html(recurring.donor_name)}</h2>
	  <p>Your monthly gift of <strong>{amount}</strong> to Glia was received on {donation.donation_date}.</p>
	  <p>It funds open-source medical devices and training for clinicians working in
	  low-resource settings — thank you for making that possible.</p>
	  <p style="color:#5b6b70;font-size:13px">You can pause or cancel any time here:
	  <a href="{manage_url}">manage your monthly gift</a>.</p>
	  <p style="color:#5b6b70;font-size:13px">Glia · info@glia.org · https://glia.org</p>
	</div>
	"""

	try:
		frappe.sendmail(
			recipients=[recurring.email],
			subject=f"Thank you for your monthly gift to Glia ({amount})",
			message=message,
			reference_doctype="Donation",
			reference_name=donation.name,
			now=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Recurring donation receipt failed ({donation.name})")
