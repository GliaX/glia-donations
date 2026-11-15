"""Recurring donation lifecycle: start (Stripe Checkout), manage, cancel."""

from __future__ import annotations

import secrets

import frappe
import stripe
from frappe.utils import cint, flt, get_url

from glia_donations.stripe_client import configure, is_live

SUPPORTED_FREQUENCIES = ("Monthly",)
FREQUENCY_INTERVAL = {"Monthly": ("month", 1), "Quarterly": ("month", 3), "Yearly": ("year", 1)}


def new_cancel_token() -> str:
	return secrets.token_urlsafe(24)


def settings():
	return frappe.get_cached_doc("Donation Settings")


def _amount_bounds():
	cfg = settings()
	minimum = flt(cfg.min_amount or 1)
	maximum = flt(cfg.max_amount or 5000)
	return minimum, maximum


def _normalise_amount(amount) -> float:
	value = round(flt(amount), 2)
	minimum, maximum = _amount_bounds()
	if value < minimum:
		frappe.throw(f"The minimum recurring gift is {minimum:.2f} {settings().default_currency or 'CAD'}.")
	if value > maximum:
		frappe.throw(
			f"For a recurring gift above {maximum:.0f}, please contact info@glia.org — we would love to talk."
		)
	return value


def ensure_donation_price(amount: float, currency: str) -> str:
	"""Return a Stripe recurring Price id for this amount, creating it if needed.

	Prices are reused by lookup key so repeat donors get the same price object and
	Stripe keeps a tidy catalog.
	"""
	stripe = configure()
	cfg = settings()
	currency = (currency or cfg.default_currency or "CAD").upper()
	minor = int(round(amount * 100))
	lookup_key = f"{cfg.price_lookup_prefix or 'glia_monthly'}_{currency.lower()}_{minor}"

	found = _find_price(stripe, lookup_key)
	if found:
		return found

	product_id = _donation_product_id(currency)
	price = stripe.Price.create(
		product=product_id,
		currency=currency.lower(),
		unit_amount=minor,
		recurring={"interval": "month", "interval_count": 1},
		lookup_key=lookup_key,
		nickname=f"{currency} {amount:.2f} monthly",
		metadata={"glia_donation": "monthly", "amount": f"{amount:.2f}", "currency": currency},
	)
	return price.id


def _find_price(stripe, lookup_key: str):
	try:
		prices = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
		if prices.data:
			return prices.data[0].id
		return None
	except Exception:
		# Older SDKs/API versions do not accept lookup_keys on list; fall back to
		# scanning active prices for the key.
		frappe.log_error(frappe.get_traceback(), "Stripe Price.list lookup_keys fallback")
		for price in stripe.Price.list(active=True, limit=100).auto_paging_iter():
			if getattr(price, "lookup_key", None) == lookup_key:
				return price.id
		return None


def _donation_product_id(currency: str) -> str:
	stripe = configure()
	cfg = settings()
	name = cfg.donation_product_name or "Glia Donation"
	existing = stripe.Product.search(query=f'metadata["glia_product"]:"donation"', limit=1)
	if existing.data:
		return existing.data[0].id
	product = stripe.Product.create(
		name=name,
		description="Recurring monthly gift supporting Glia's medical solidarity work.",
		metadata={"glia_product": "donation"},
	)
	return product.id


def ensure_contact(email: str, full_name: str | None = None) -> str:
	"""Find or create a Contact for the donor (Donation.contact is mandatory)."""
	from frappe.utils import validate_email_address

	email = (email or "").strip()
	validate_email_address(email, throw=True)
	name = (full_name or "").strip() or email.split("@")[0]

	contact_name = frappe.db.get_value("Contact Email", {"email_id": email}, "parent")
	if contact_name:
		return contact_name

	contact = frappe.new_doc("Contact")
	contact.update(
		{
			"first_name": name[:140],
			"email_ids": [{"email_id": email, "is_primary": 1}],
		}
	)
	contact.flags.ignore_permissions = True
	contact.insert(ignore_permissions=True)
	return contact.name


def start(email: str, full_name: str | None, amount, currency: str | None = None,
          campaign: str | None = None, frequency: str = "Monthly"):
	"""Create a Pending Recurring Donation and a Stripe Checkout (subscription) URL."""
	cfg = settings()
	if not cfg.enabled:
		frappe.throw("Recurring donations are not available right now.")

	frequency = frequency or "Monthly"
	if frequency not in SUPPORTED_FREQUENCIES:
		frappe.throw("Only monthly recurring gifts are available at the moment.")

	stripe = configure()
	value = _normalise_amount(amount)
	currency = (currency or cfg.default_currency or "CAD").upper()

	email = (email or "").strip()
	full_name = (full_name or "").strip() or email.split("@")[0]
	contact = ensure_contact(email, full_name)

	doc = frappe.new_doc("Recurring Donation")
	doc.update(
		{
			"donor_name": full_name,
			"email": email,
			"contact": contact,
			"campaign": campaign or "",
			"amount": value,
			"currency": currency,
			"frequency": frequency,
			"status": "Pending",
			"source": "Website",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	price_id = ensure_donation_price(value, currency)
	base_url = get_url().rstrip("/")
	token = doc.cancel_token
	session = stripe.checkout.Session.create(
		mode="subscription",
		line_items=[{"price": price_id, "quantity": 1}],
		customer_email=email,
		client_reference_id=doc.name,
		success_url=(
			f"{base_url}{cfg.success_path or '/recurring_donation'}"
			f"?token={token}&session_id={{CHECKOUT_SESSION_ID}}"
		),
		cancel_url=f"{base_url}{cfg.cancel_path or '/recurring_donation'}?token={token}&status=cancelled",
		metadata={"recurring_donation": doc.name, "token": token},
		subscription_data={"metadata": {"recurring_donation": doc.name, "token": token}},
		allow_promotion_codes=False,
		billing_address_collection="auto",
	)
	doc.db_set(
		{
			"stripe_checkout_session": session.id,
			"stripe_price_id": price_id,
			"notes": f"Checkout session created ({'live' if is_live() else 'test'} mode).",
		},
		update_modified=False,
	)

	return {
		"url": session.url,
		"recurring_donation": doc.name,
		"token": token,
		"amount": value,
		"currency": currency,
		"live": is_live(),
	}


def by_token(token: str):
	if not token:
		return None
	name = frappe.db.get_value("Recurring Donation", {"cancel_token": token}, "name")
	if not name:
		return None
	return frappe.get_doc("Recurring Donation", name)


def donor_view(doc):
	"""Fields safe to show on the public manage page."""
	return {
		"status": doc.status,
		"donor_name": doc.donor_name,
		"amount": flt(doc.amount),
		"currency": doc.currency,
		"frequency": doc.frequency,
		"start_date": str(doc.start_date) if doc.start_date else None,
		"next_charge_date": str(doc.next_charge_date) if doc.next_charge_date else None,
		"charges_count": cint(doc.charges_count),
		"total_collected": flt(doc.total_collected),
		"cancel_token": doc.cancel_token,
		"can_cancel": doc.status in ("Active", "Past Due", "Pending"),
	}


def cancel(token: str, reason: str | None = None):
	doc = by_token(token)
	if not doc:
		frappe.throw("This donation link is not valid.")
	if doc.status in ("Cancelled", "Completed"):
		return donor_view(doc)

	stripe = configure()
	if doc.stripe_subscription_id:
		try:
			stripe.Subscription.cancel(doc.stripe_subscription_id)
		except stripe.error.InvalidRequestError as exc:
			# Already gone at Stripe — reflect that rather than lying to the donor.
			frappe.log_error(f"Stripe cancel failed for {doc.name}: {exc}", "Recurring donation cancel")
			if "canceled" not in str(exc).lower() and "no such subscription" not in str(exc).lower():
				raise

	doc.status = "Cancelled"
	doc.cancelled_on = frappe.utils.now_datetime()
	doc.cancellation_reason = (reason or "Cancelled by donor on the website")[:500]
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return donor_view(doc)


def date_from_period_end(period_end) -> str | None:
	"""Stripe returns period ends as UTC unix timestamps (or ISO strings)."""
	from datetime import datetime, timezone

	from frappe.utils import getdate

	if not period_end:
		return None
	try:
		if isinstance(period_end, (int, float)):
			return str(getdate(datetime.fromtimestamp(period_end, tz=timezone.utc)))
		return str(getdate(str(period_end)[:19]))
	except Exception:
		return None
