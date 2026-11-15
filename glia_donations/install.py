"""Install hooks: custom fields and small adjustments to the Glia `Donation` doctype.

`Donation` is a *custom* doctype created in the database by the Glia sync
tooling (module "Glia"), so it is not defined by any app in the image. We never
redefine it here; we only add the two fields a website donation needs, relax the
Shopify-only requirements, and allow the Website source.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def after_install():
	ensure_donation_fields()
	relax_shopify_requirements()
	add_website_source()


def ensure_donation_fields():
	fields = [
		{
			"fieldname": "stripe_invoice_id",
			"label": "Stripe Invoice ID",
			"fieldtype": "Data",
			"insert_after": "shopify_line_item_id",
			"read_only": 1,
			"no_copy": 1,
			"description": "Stripe invoice that created this donation (recurring gifts).",
		},
		{
			"fieldname": "recurring_donation",
			"label": "Recurring Donation",
			"fieldtype": "Link",
			"options": "Recurring Donation",
			"insert_after": "stripe_invoice_id",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "sales_order",
			"label": "Sales Order",
			"fieldtype": "Link",
			"options": "Sales Order",
			"insert_after": "recurring_donation",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "sales_invoice",
			"label": "Sales Invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"insert_after": "sales_order",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "payment_entry",
			"label": "Payment Entry",
			"fieldtype": "Link",
			"options": "Payment Entry",
			"insert_after": "sales_invoice",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "item_code",
			"label": "Item",
			"fieldtype": "Link",
			"options": "Item",
			"insert_after": "payment_entry",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "stripe_payment_intent",
			"label": "Stripe Payment Intent",
			"fieldtype": "Data",
			"insert_after": "stripe_invoice_id",
			"read_only": 1,
			"no_copy": 1,
		},
	]
	if not frappe.db.exists("DocType", "Donation"):
		return
	for spec in fields:
		if not frappe.get_meta("Donation").has_field(spec["fieldname"]):
			create_custom_field("Donation", spec, ignore_validate=True)


def relax_shopify_requirements():
	"""Website donations have no Shopify order/line item; make those optional."""
	if not frappe.db.exists("DocType", "Donation"):
		return
	for fieldname in ("shopify_order_id", "shopify_line_item_id"):
		frappe.db.sql(
			"update `tabDocField` set reqd=0 where parent='Donation' and fieldname=%s",
			(fieldname,),
		)
	frappe.clear_cache(doctype="Donation")


def add_website_source():
	if not frappe.db.exists("DocType", "Donation"):
		return
	row = frappe.db.sql(
		"select options from `tabDocField` where parent='Donation' and fieldname='source'",
		as_dict=True,
	)
	if not row:
		return
	options = [opt for opt in (row[0].options or "").split("\n") if opt]
	if "Website" not in options:
		options.append("Website")
		frappe.db.sql(
			"update `tabDocField` set options=%s where parent='Donation' and fieldname='source'",
			("\n".join(options),),
		)
		frappe.clear_cache(doctype="Donation")
