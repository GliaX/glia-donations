"""Create Donation records when a shop order is actually paid.

One-off donations bought through the shop (the donation product page → guest
cart → Stripe) end up as a Sales Order, a Payment Request, then — when Stripe
confirms — a Sales Invoice plus a Payment Entry. Without this hook those gifts
never reach the donor CRM, which is exactly the reporting gap Shopify left
behind.

The hook runs on **Payment Entry submit**, i.e. when money has arrived, so
unpaid or abandoned orders never create donations. It is defensive on purpose:
a failure here must never break payment processing, so everything is logged and
swallowed.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from glia_donations.recurring import ensure_contact

DONATION_GROUP = "Donation"
FINANCIAL_STATUS_PAID = "PAID"

# Fields this app adds to the (DB-level, sync-owned) Donation doctype.
LINK_FIELDS = ("sales_invoice", "sales_order", "payment_entry", "item_code")


def is_donation_item(item_code: str, item_group: str | None = None) -> bool:
	"""Donations are the items in the Donation group (templates and variants),
	plus anything the Shopify sync explicitly flagged."""
	if item_group == DONATION_GROUP:
		return True
	row = frappe.db.get_value("Item", item_code, ["item_group", "shopify_is_donation"], as_dict=True)
	if not row:
		return False
	return row.item_group == DONATION_GROUP or bool(row.shopify_is_donation)


def _template_of(item_code: str) -> str:
	return frappe.db.get_value("Item", item_code, "variant_of") or item_code


def _contact_email(invoice, order) -> str | None:
	email = invoice.get("contact_email") if invoice else None
	if not email and order:
		email = order.get("contact_email")
	if not email and invoice:
		email = frappe.db.get_value("Customer", invoice.customer, "email_id")
	return (email or "").strip() or None


def _invoice_already_recorded(invoice_name: str) -> bool:
	"""A donation booked by glia_donations already represents this invoice.

	Recurring charges are booked through the same machinery (Sales Invoice +
	Payment Entry); without this guard the Payment Entry submit would be treated
	as a fresh shop donation and create a duplicate Donation record.
	"""
	return bool(frappe.db.exists("Donation", {"sales_invoice": invoice_name}))


def _already_recorded(payment_entry: str, invoice: str | None, item_code: str) -> bool:
	filters = {"payment_entry": payment_entry, "item_code": item_code}
	if invoice:
		filters["sales_invoice"] = invoice
	return bool(frappe.db.exists("Donation", filters))


def _make_donation(payment_entry, invoice, order, item, amount):
	if amount <= 0:
		return None
	email = _contact_email(invoice, order)
	if not email:
		frappe.log_error(
			f"Donation line {item.get('item_code')} on {invoice.name if invoice else order.name} "
			"has no contact email; donation not recorded.",
			"Donation from payment: missing email",
		)
		return None

	contact = ensure_contact(email, (invoice or order).customer_name)
	template = _template_of(item.item_code)
	donation = frappe.new_doc("Donation")
	donation.update(
		{
			"contact": contact,
			"source": "Website",
			"donor_name": (invoice or order).customer_name or contact,
			"donor_email": email,
			"donation_date": payment_entry.posting_date,
			"amount": amount,
			"currency": payment_entry.paid_to_account_currency or invoice.currency,
			"donation_type": "One-time",
			"campaign": (frappe.db.get_value("Item", template, "item_name") or template)[:140],
			"tier": (item.get("item_name") or item.item_code)[:140],
			"financial_status": FINANCIAL_STATUS_PAID,
			"payment_entry": payment_entry.name,
			"sales_invoice": invoice.name if invoice else None,
			"sales_order": item.get("sales_order") or (order.name if order else None),
			"item_code": item.item_code,
		}
	)
	if frappe.get_meta("Item").has_field("shopify_is_recurring") and frappe.db.get_value(
		"Item", template, "shopify_is_recurring"
	):
		donation.donation_type = "Recurring"
	donation.flags.ignore_permissions = True
	donation.insert(ignore_permissions=True)
	return donation.name


def _ratio(allocated, total):
	total = flt(total)
	if total <= 0:
		return 1.0
	return min(1.0, max(0.0, flt(allocated) / total))


def from_invoice(payment_entry, invoice_name, allocated_amount):
	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if invoice.docstatus != 1:
		return []
	if _invoice_already_recorded(invoice.name):
		return []
	ratio = _ratio(allocated_amount, invoice.grand_total)
	orders = {}
	created = []
	for item in invoice.items:
		if not is_donation_item(item.item_code, item.item_group):
			continue
		order = None
		if item.get("sales_order"):
			order = orders.get(item.sales_order)
			if order is None:
				order = frappe.get_doc("Sales Order", item.sales_order)
				orders[item.sales_order] = order
		if _already_recorded(payment_entry.name, invoice.name, item.item_code):
			continue
		name = _make_donation(payment_entry, invoice, order, item, flt(item.amount) * ratio)
		if name:
			created.append(name)
	return created


def from_order(payment_entry, order_name, allocated_amount):
	order = frappe.get_doc("Sales Order", order_name)
	if order.docstatus != 1:
		return []
	ratio = _ratio(allocated_amount, order.grand_total)
	created = []
	for item in order.items:
		if not is_donation_item(item.item_code, item.item_group):
			continue
		if _already_recorded(payment_entry.name, None, item.item_code):
			continue
		name = _make_donation(payment_entry, None, order, item, flt(item.amount) * ratio)
		if name:
			created.append(name)
	return created


def on_payment_entry_submit(doc, method=None):
	"""Payment Entry on_submit hook: record donated amounts against the donor."""
	try:
		if doc.get("payment_type") != "Receive" or doc.get("party_type") != "Customer":
			return
		created = []
		for ref in doc.get("references") or []:
			if ref.reference_doctype == "Sales Invoice":
				created += from_invoice(doc, ref.reference_name, ref.allocated_amount)
			elif ref.reference_doctype == "Sales Order":
				created += from_order(doc, ref.reference_name, ref.allocated_amount)
		if created:
			frappe.logger("glia_donations").info(
				f"Payment Entry {doc.name}: created {len(created)} donation record(s): {', '.join(created)}"
			)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Donation from payment entry {doc.name}")


def backfill(create=False, limit=500):
	"""Record donations for donated invoices that predate the hook.

	Walks submitted Sales Invoices containing donation items, finds the Payment
	Entries that paid them, and runs the same idempotent path as the live hook.
	With create=False it only reports what would be created.
	"""
	invoice_names = frappe.get_all(
		"Sales Invoice Item",
		filters={"item_group": DONATION_GROUP, "docstatus": 1},
		pluck="parent",
		distinct=True,
		limit=limit,
	)
	report = []
	created = []
	for name in invoice_names:
		pe_rows = frappe.db.sql(
			"""
			select pe.name, pe.posting_date, per.allocated_amount
			from `tabPayment Entry` pe
			join `tabPayment Entry Reference` per
			  on per.parent = pe.name and per.reference_doctype = 'Sales Invoice' and per.reference_name = %s
			where pe.docstatus = 1 and pe.payment_type = 'Receive'
			""",
			(name,),
			as_dict=True,
		)
		if not pe_rows:
			report.append({"invoice": name, "paid": False, "payment_entries": []})
			continue
		report.append({"invoice": name, "paid": True, "payment_entries": [r.name for r in pe_rows]})
		if create:
			for row in pe_rows:
				created += from_invoice(
					frappe.get_doc("Payment Entry", row.name), name, row.allocated_amount
				)
	return {
		"create": bool(create),
		"invoices_found": len(report),
		"donations_created": len(created),
		"created": created[:50],
		"report": report[:50],
	}


@frappe.whitelist()
def run_backfill(create=0):
	frappe.only_for(("System Manager", "Accounts Manager"))
	return backfill(create=bool(int(create)))
