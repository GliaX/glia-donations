import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from glia_donations.recurring import new_cancel_token


class RecurringDonation(Document):
	def validate(self):
		self.amount = flt(self.amount)
		if self.status == "Cancelled" and not self.cancelled_on:
			self.cancelled_on = now_datetime()

	def before_insert(self):
		if not self.cancel_token:
			self.cancel_token = new_cancel_token()
