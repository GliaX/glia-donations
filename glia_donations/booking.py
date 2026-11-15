"""Book recurring charges to the ledger (Sales Invoice + Payment Entry).

Recurring gifts used to create the Donation record only, which left the books
untouched: cash arrived at Stripe but no income was recognised. This module
mirrors what the shop already does for one-off orders — a paid Sales Invoice —
using a single generic donation item and the donation income account configured
in Donation Settings.

Booking is **opt-in** (`booking_enabled`), because the correct accounts are an
accounting decision, not something an app should guess. Everything is defensive:
a booking failure is logged and reported, but never loses the Donation record or
fails the Stripe webhook.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, today

from glia_donations.recurring import ensure_contact, settings


def _company() -> str:
	company = settings().get("company") or frappe.db.get_single_value("Webshop Settings", "company")
	return company or frappe.db.get_single_value("Global Defaults", "default_company")


def ensure_customer(recurring):
	"""Find or create the donor Customer, linked to their Contact."""
	if recurring.get("customer") and frappe.db.exists("Customer", recurring.customer):
		return recurring.customer

	if recurring.get("contact"):
		linked = frappe.db.get_value(
			"Dynamic Link",
			{"parenttype": "Contact", "parent": recurring.contact, "link_doctype": "Customer"},
			"link_name",
		)
		if linked:
			recurring.db_set("customer", linked, update_modified=False)
			return linked

	customer = frappe.new_doc("Customer")
	customer.update(
		{
			"customer_name": recurring.donor_name or recurring.email,
			"customer_type": "Individual",
			"customer_group": frappe.db.get_single_value("Selling Settings", "customer_group")
			or "Individual",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert(ignore_permissions=True)

	if not recurring.get("contact"):
		recurring.db_set("contact", ensure_contact(recurring.email, recurring.donor_name), update_modified=False)
	if recurring.contact:
		contact = frappe.get_doc("Contact", recurring.contact)
		if not any(
			link.link_doctype == "Customer" and link.link_name == customer.name
			for link in contact.get("links", [])
		):
			contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})
			contact.flags.ignore_permissions = True
			contact.save(ignore_permissions=True)

	recurring.db_set("customer", customer.name, update_modified=False)
	return customer.name


def _validate_config(cfg):
	missing = [f for f in ("donation_item", "donation_income_account") if not cfg.get(f)]
	if not cfg.booking_enabled:
		return "booking disabled"
	if missing:
		return "missing configuration: " + ", ".join(missing)
	if not frappe.db.exists("Item", cfg.donation_item):
		return f"donation item {cfg.donation_item} does not exist"
	if not frappe.db.exists("Account", cfg.donation_income_account):
		return f"income account {cfg.donation_income_account} does not exist"
	return None


def book_donation(donation_name: str):
	"""Create the Sales Invoice + Payment Entry for one Donation. Idempotent."""
	donation = frappe.get_doc("Donation", donation_name)
	if donation.get("sales_invoice") and frappe.db.exists("Sales Invoice", donation.sales_invoice):
		return {"status": "already booked", "sales_invoice": donation.sales_invoice}

	cfg = settings()
	problem = _validate_config(cfg)
	if problem:
		return {"status": "skipped", "reason": problem}

	recurring_name = donation.get("recurring_donation")
	recurring = frappe.get_doc("Recurring Donation", recurring_name) if recurring_name else None
	company = _company()
	currency = donation.currency or frappe.db.get_value("Company", company, "default_currency")

	party = ensure_customer(recurring) if recurring else _ensure_customer_from_donation(donation)
	cost_center = frappe.db.get_value("Company", company, "cost_center")

	invoice = frappe.new_doc("Sales Invoice")
	invoice.update(
		{
			"customer": party,
			"company": company,
			"posting_date": donation.donation_date or today(),
			"due_date": donation.donation_date or today(),
			"currency": currency,
			"selling_price_list": frappe.db.get_single_value("Selling Settings", "selling_price_list"),
			"remarks": (
				f"Recurring donation {recurring_name} — Stripe invoice {donation.get('stripe_invoice_id')}"
				if recurring_name
				else f"Donation {donation.name}"
			),
		}
	)
	invoice.append(
		"items",
		{
			"item_code": cfg.donation_item,
			"item_name": donation.get("campaign") or "Donation",
			"qty": 1,
			"rate": flt(donation.amount),
			"income_account": cfg.donation_income_account,
			"cost_center": cost_center,
		},
	)
	invoice.flags.ignore_permissions = True
	invoice.insert(ignore_permissions=True)
	invoice.submit()

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment = get_payment_entry("Sales Invoice", invoice.name, party_amount=invoice.outstanding_amount)
	if cfg.mode_of_payment and frappe.db.exists("Mode of Payment", cfg.mode_of_payment):
		payment.mode_of_payment = cfg.mode_of_payment
	if cfg.stripe_payment_account and frappe.db.exists("Account", cfg.stripe_payment_account):
		payment.paid_to = cfg.stripe_payment_account
	if donation.get("stripe_invoice_id"):
		payment.reference_no = donation.stripe_invoice_id
		payment.reference_date = donation.donation_date or today()
	payment.flags.ignore_permissions = True
	payment.insert(ignore_permissions=True)
	payment.submit()

	donation.db_set(
		{"sales_invoice": invoice.name, "payment_entry": payment.name}, update_modified=False
	)
	return {
		"status": "booked",
		"sales_invoice": invoice.name,
		"payment_entry": payment.name,
		"amount": flt(donation.amount),
	}


def _ensure_customer_from_donation(donation):
	if donation.get("contact"):
		linked = frappe.db.get_value(
			"Dynamic Link",
			{"parenttype": "Contact", "parent": donation.contact, "link_doctype": "Customer"},
			"link_name",
		)
		if linked:
			return linked
	customer = frappe.new_doc("Customer")
	customer.update(
		{
			"customer_name": donation.donor_name or donation.donor_email,
			"customer_type": "Individual",
			"customer_group": frappe.db.get_single_value("Selling Settings", "customer_group") or "Individual",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert(ignore_permissions=True)
	return customer.name


def book_safely(donation_name: str):
	"""Never let accounting break the donation flow."""
	try:
		return book_donation(donation_name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Recurring donation booking failed ({donation_name})")
		return {"status": "error", "donation": donation_name}


@frappe.whitelist()
def retry_missing_bookings(limit=100):
	"""Book donations that were recorded while booking was disabled/misconfigured."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	cfg = settings()
	problem = _validate_config(cfg)
	if problem:
		return {"booked": 0, "skipped_reason": problem}
	rows = frappe.get_all(
		"Donation",
		filters={"source": "Website", "sales_invoice": ["is", "not set"], "recurring_donation": ["is", "set"]},
		pluck="name",
		limit=limit,
	)
	results = [book_safely(name) for name in rows]
	return {
		"candidates": len(rows),
		"booked": sum(1 for r in results if r.get("status") == "booked"),
		"results": results[:50],
	}
