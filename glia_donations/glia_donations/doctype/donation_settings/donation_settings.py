import frappe
from frappe.model.document import Document


class DonationSettings(Document):
	def validate(self):
		if self.min_amount and self.max_amount and self.min_amount > self.max_amount:
			frappe.throw("Minimum amount cannot exceed the maximum amount.")
