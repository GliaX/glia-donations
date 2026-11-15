app_name = "glia_donations"
app_title = "Glia Donations"
app_publisher = "Glia"
app_description = "Recurring donations for glia.org — Stripe Billing subscriptions that write Donation records"
app_email = "info@glia.org"
app_license = "MIT"

required_apps = ["payments"]

after_install = "glia_donations.install.after_install"

# Keep the Donation doctype (a DB-level custom doctype) adjusted on every migrate,
# because a restore or a fresh site may not carry the custom fields.
after_migrate = ["glia_donations.install.after_install"]
