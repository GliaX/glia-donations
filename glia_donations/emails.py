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
	"""Receipt email for every gift — one-off and recurring.

	Sent from a background worker so a mail failure can never turn a recorded
	donation into a payment error. Includes the details a donor needs for tax
	purposes (amount, date, business number) without claiming a formal receipt
	that finance has not issued.
	"""
	donation = frappe.get_doc("Donation", donation_name)
	recurring_name = donation.get("recurring_donation")
	recurring = frappe.get_doc("Recurring Donation", recurring_name) if recurring_name else None
	email = (recurring.email if recurring else None) or donation.get("donor_email")
	if not email:
		frappe.log_error(f"Donation {donation.name} has no donor email; receipt not sent.",
		                 "Donation receipt: missing email")
		return

	amount = f"{donation.currency} {flt(donation.amount):.2f}"
	donor = (recurring.donor_name if recurring else donation.get("donor_name")) or "friend"
	kind = "monthly gift" if recurring else "gift"
	manage_url = f"{get_url()}/recurring_donation?token={recurring.cancel_token}" if recurring else None
	manage_line = (
		f'<p style="color:#5b6b70;font-size:13px">You can pause or cancel any time here: '
		f'<a href="{manage_url}">manage your monthly gift</a>.</p>'
		if manage_url else ""
	)
	body = f"""
	<div style="font-family:Ubuntu,Arial,sans-serif;color:#032e38;max-width:560px">
	  <h2 style="margin:0 0 12px">Thank you, {escape_html(donor)}</h2>
	  <p>Your {kind} of <strong>{amount}</strong> to Glia was received on {donation.donation_date}.</p>
	  <p>It funds open-source medical devices and training for clinicians working in
	  low-resource settings — thank you for making that possible.</p>
	  <p style="font-size:13px"><strong>For your records</strong><br>
	  Glia · Business Number (BN) 807831391<br>
	  Gift {{amount}} on {{donation.donation_date}}</p>
	  <p style="color:#5b6b70;font-size:13px">Keep this email as your receipt. If you need a
	  formal tax receipt or a correction, reply or write to info@glia.org.</p>
	  {manage_line}
	  <p style="color:#5b6b70;font-size:13px">Glia · info@glia.org · https://glia.org</p>
	</div>
	"""

	try:
		frappe.sendmail(
			recipients=[email],
			subject=f"Thank you for your {kind} to Glia ({amount})",
			message=body,
			reference_doctype="Donation",
			reference_name=donation.name,
			now=True,
		)
		# record that a receipt went out, so the CRM can show acknowledged gifts
		updates = {}
		if frappe.get_meta("Donation").has_field("acknowledged"):
			updates["acknowledged"] = 1
		if frappe.get_meta("Donation").has_field("acknowledged_date"):
			updates["acknowledged_date"] = frappe.utils.today()
		if updates:
			frappe.db.set_value("Donation", donation.name, updates, update_modified=False)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Donation receipt failed ({donation.name})")
