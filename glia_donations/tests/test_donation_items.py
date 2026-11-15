"""The shop hook must only ever record real donation lines."""

import unittest

import frappe

from glia_donations.donation_from_sales import is_donation_item


class TestDonationItems(unittest.TestCase):
	def test_known_donation_item(self):
		if not frappe.db.exists("Item", "misc-monthly-donation"):
			self.skipTest("misc-monthly-donation not installed")
		self.assertTrue(is_donation_item("misc-monthly-donation"))

	def test_merch_is_not_a_donation(self):
		merch = frappe.get_all("Item", filters={"item_group": "Products"}, pluck="name", limit=1)
		if not merch:
			self.skipTest("no Products items")
		self.assertFalse(is_donation_item(merch[0]))

	def test_unknown_item_is_not_a_donation(self):
		self.assertFalse(is_donation_item("this-item-does-not-exist-xyz"))

	def test_gray_area_accounts_are_not_donations(self):
		# the Gray Area child group holds GL/fee items and must never be treated as gifts
		gray = frappe.get_all("Item", filters={"item_group": "Gray Area"}, pluck="name", limit=3)
		for item in gray:
			self.assertFalse(is_donation_item(item), item)
