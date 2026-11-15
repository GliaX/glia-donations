import frappe

from glia_donations import recurring


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	token = (frappe.form_dict.get("token") or "").strip()
	session_status = (frappe.form_dict.get("status") or "").strip()
	context.token = token
	context.session_status = session_status
	context.donation = None
	context.error = None
	context.page_title = "Your monthly gift to Glia"

	doc = recurring.by_token(token) if token else None
	if doc:
		context.donation = recurring.donor_view(doc)
	else:
		context.error = "We could not find that donation link. Please check the link in your receipt email."
	return context
