"""Stripe webhook: turns subscription events into Donation records and status.

Endpoint: POST /api/method/glia_donations.webhooks.stripe_webhook
Configure it in Stripe with these events:
  checkout.session.completed
  invoice.paid
  invoice.payment_failed
  customer.subscription.deleted
and paste the endpoint's signing secret (whsec_…) into Donation Settings.

Every handler is idempotent: Stripe retries and can deliver out of order, so
each one looks up state before writing.
"""

from __future__ import annotations

import frappe
import stripe
from frappe.utils import cint, flt, today

from glia_donations.recurring import create_from_checkout, date_from_period_end, settings
from glia_donations.stripe_client import configure

HANDLED_EVENTS = (
	"checkout.session.completed",
	"invoice.paid",
	"invoice.payment_failed",
	"customer.subscription.deleted",
)


def _webhook_secret() -> str:
	secret = settings().get_password("stripe_webhook_secret", raise_exception=False)
	if not secret:
		secret = frappe.conf.get("stripe_webhook_secret")
	if not secret:
		frappe.throw("Recurring donations webhook secret is not configured.")
	return secret


@frappe.whitelist(allow_guest=True, methods=["POST"])
def stripe_webhook():
	payload = frappe.request.get_data(as_text=True)
	signature = frappe.request.headers.get("Stripe-Signature")

	if not signature:
		frappe.throw("Missing Stripe-Signature header.", frappe.AuthenticationError)

	try:
		event = stripe.Webhook.construct_event(payload, signature, _webhook_secret())
	except ValueError:
		frappe.throw("Invalid webhook payload.", frappe.AuthenticationError)
	except stripe.error.SignatureVerificationError:
		frappe.throw("Invalid webhook signature.", frappe.AuthenticationError)

	event_type = event["type"]
	if event_type not in HANDLED_EVENTS:
		return {"received": True, "ignored": event_type}

	frappe.set_user("Administrator")
	try:
		result = dispatch(event_type, event["data"]["object"])
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), f"Recurring donation webhook: {event_type}")
		raise
	finally:
		frappe.set_user("Guest")

	return {"received": True, "handled": event_type, **result}


def dispatch(event_type, obj):
	if event_type == "checkout.session.completed":
		return on_checkout_completed(obj)
	if event_type == "invoice.paid":
		return on_invoice_paid(obj)
	if event_type == "invoice.payment_failed":
		return on_invoice_failed(obj)
	if event_type == "customer.subscription.deleted":
		return on_subscription_deleted(obj)
	return {}


def _recurring_donation_from(obj):
	name = None
	metadata = obj.get("metadata") or {}
	if isinstance(metadata, dict):
		name = metadata.get("recurring_donation")
	if not name and obj.get("client_reference_id"):
		name = obj.get("client_reference_id")
	if name and frappe.db.exists("Recurring Donation", name):
		return frappe.get_doc("Recurring Donation", name)
	token = metadata.get("token") if isinstance(metadata, dict) else None
	if token:
		name = frappe.db.get_value("Recurring Donation", {"cancel_token": token}, "name")
		if name:
			return frappe.get_doc("Recurring Donation", name)
	subscription_id = obj.get("subscription") or obj.get("id")
	if subscription_id:
		name = frappe.db.get_value(
			"Recurring Donation", {"stripe_subscription_id": subscription_id}, "name"
		)
		if name:
			return frappe.get_doc("Recurring Donation", name)
	return None


def on_checkout_completed(session):
	doc = _recurring_donation_from(session)
	if not doc:
		metadata = session.get("metadata") or {}
		token = metadata.get("token") if isinstance(metadata, dict) else None
		if not token:
			return {"skipped": "no recurring donation or token for session"}
		doc = create_from_checkout(session, token)
		frappe.logger("glia_donations").info(
			f"Recurring donation {doc.name} created from checkout {session.get('id')}"
		)

	subscription_id = session.get("subscription")
	period_end = None
	configure()
	if subscription_id:
		try:
			subscription = stripe.Subscription.retrieve(subscription_id)
			period_end = subscription.get("current_period_end")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Stripe subscription retrieve failed")

	doc.stripe_customer_id = session.get("customer") or doc.stripe_customer_id
	doc.stripe_subscription_id = subscription_id or doc.stripe_subscription_id
	doc.stripe_checkout_session = session.get("id") or doc.stripe_checkout_session
	if doc.status in ("Pending", "Past Due"):
		doc.status = "Active"
	doc.start_date = doc.start_date or today()
	if period_end:
		doc.next_charge_date = date_from_period_end(period_end)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"recurring_donation": doc.name, "status": doc.status}


def on_invoice_paid(invoice):
	# Only subscription invoices create recurring Donation records here; one-off
	# invoices belong to the guest checkout flow.
	if not (invoice.get("subscription") or (invoice.get("lines") or {}).get("data")):
		return {"skipped": "not a subscription invoice"}

	subscription_id = invoice.get("subscription")
	doc = None
	if subscription_id:
		name = frappe.db.get_value(
			"Recurring Donation", {"stripe_subscription_id": subscription_id}, "name"
		)
		if name:
			doc = frappe.get_doc("Recurring Donation", name)
	if not doc:
		doc = _recurring_donation_from(invoice)
	if not doc:
		return {"skipped": "no recurring donation for invoice"}

	invoice_id = invoice.get("id")
	if invoice_id and frappe.db.exists("Donation", {"stripe_invoice_id": invoice_id}):
		return {"skipped": f"donation exists for {invoice_id}"}

	amount_minor = invoice.get("amount_paid") or invoice.get("total")
	amount = flt(amount_minor) / 100.0
	if amount <= 0:
		return {"skipped": "zero-amount invoice"}

	currency = (invoice.get("currency") or doc.currency or "CAD").upper()
	period_end = None
	lines = (invoice.get("lines") or {}).get("data") or []
	if lines:
		period_end = (lines[0].get("period") or {}).get("end")

	donation = frappe.new_doc("Donation")
	donation.update(
		{
			"contact": doc.contact,
			"source": "Website",
			"donor_name": doc.donor_name,
			"donor_email": doc.email,
			"donation_date": today(),
			"amount": amount,
			"currency": currency,
			"donation_type": "Recurring",
			"campaign": doc.campaign or "",
			"financial_status": "PAID",
			"stripe_invoice_id": invoice_id,
			"recurring_donation": doc.name,
			"stripe_payment_intent": invoice.get("payment_intent") or "",
		}
	)
	donation.flags.ignore_permissions = True
	donation.insert(ignore_permissions=True)

	doc.charges_count = cint(doc.charges_count) + 1
	doc.total_collected = flt(doc.total_collected) + amount
	doc.last_payment_date = today()
	if period_end:
		doc.next_charge_date = date_from_period_end(period_end)
	elif doc.next_charge_date:
		from frappe.utils import add_months

		doc.next_charge_date = add_months(doc.next_charge_date, 1)
	doc.status = "Active"
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)

	_send_receipt(doc, donation, invoice_id)
	return {"recurring_donation": doc.name, "donation": donation.name, "amount": amount}


def on_invoice_failed(invoice):
	doc = _recurring_donation_from(invoice)
	if not doc:
		return {"skipped": "no recurring donation for invoice"}
	doc.status = "Past Due"
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return {"recurring_donation": doc.name, "status": doc.status}


def on_subscription_deleted(subscription):
	name = (subscription.get("metadata") or {}).get("recurring_donation")
	doc = None
	if name and frappe.db.exists("Recurring Donation", name):
		doc = frappe.get_doc("Recurring Donation", name)
	if not doc:
		name = frappe.db.get_value(
			"Recurring Donation", {"stripe_subscription_id": subscription.get("id")}, "name"
		)
		doc = frappe.get_doc("Recurring Donation", name) if name else None
	if not doc:
		return {"skipped": "no recurring donation for subscription"}
	if doc.status != "Cancelled":
		doc.status = "Cancelled"
		doc.cancelled_on = frappe.utils.now_datetime()
		doc.cancellation_reason = doc.cancellation_reason or "Subscription ended at Stripe"
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
	return {"recurring_donation": doc.name, "status": doc.status}


def _send_receipt(recurring, donation, invoice_id):
	"""Queue the receipt on the worker; SMTP failures must never fail the webhook."""
	cfg = settings()
	if not cfg.send_receipt:
		return
	try:
		frappe.enqueue(
			"glia_donations.emails.send_receipt_email",
			queue="short",
			donation_name=donation.name,
			enqueue_after_commit=True,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Recurring donation receipt queue failed ({invoice_id})")
