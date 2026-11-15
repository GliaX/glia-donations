# glia-donations

Recurring (monthly) donations for **glia.org**, built as a Frappe app for
ERPNext v16 + Stripe Billing. Each successful monthly charge becomes a
`Donation` record, so donor reporting keeps working after the move off Shopify.

Built for the Glia Shopify → ERPNext migration; pairs with
[`glia-guest-checkout`](https://github.com/GliaX/glia-guest-checkout) (anonymous
one-off shop/donation checkout) but is independent of it.

---

## Why a separate app

Upstream WebShop + ERPNext have no recurring-billing flow: a `Payment Request`
is a single charge, and Stripe subscriptions would need custom code anyway. This
app keeps that code (subscriptions, webhook, donor self-service) out of the shop
apps, versioned and testable on its own.

## How it works

```
donation bar (Monthly) → start_recurring_donation
      │                       creates: Recurring Donation (Pending) + Stripe Price (reused)
      ↓                       returns: hosted Stripe Checkout URL (mode=subscription)
Stripe Checkout  ── donor pays ──►  checkout.session.completed ─► status Active
                                    invoice.paid               ─► Donation record + totals
                                    invoice.payment_failed     ─► status Past Due
                                    customer.subscription.deleted ─► status Cancelled
```

- **`Recurring Donation`** — one doc per donor subscription: amount, currency,
  status (`Pending` → `Active` → `Past Due` → `Cancelled`), Stripe ids, next
  charge, charges count, total collected, and a `cancel_token` for the public
  manage page.
- **`Donation Settings`** (single) — enable switch, min/max monthly amount,
  Stripe product name, price lookup prefix, success/cancel paths, webhook secret,
  optional receipt email.
- **`Donation`** — the Glia CRM doctype (a database-level custom doctype, not an
  app). This app only adds three fields (`stripe_invoice_id`, `stripe_payment_intent`,
  `recurring_donation`), relaxes the Shopify-only requirements and adds the
  `Website` source — never redefines the doctype.
- **Manage page** — `/recurring_donation?token=…`: shows status/totals and lets
  the donor cancel with one click (cancels the Stripe subscription immediately).

Idempotency: every webhook handler checks state first (Stripe retries and can
deliver out of order); `invoice.paid` skips if a `Donation` already exists for
that invoice id.

## Endpoints

| Method | Auth | Purpose |
|---|---|---|
| `glia_donations.api.start_recurring_donation(email, full_name, amount, currency, campaign)` | guest | Create the subscription checkout, return the Stripe URL |
| `glia_donations.api.recurring_donation_status(token)` | guest | Status/totals for the manage page |
| `glia_donations.api.cancel_recurring_donation(token, reason)` | guest | Cancel at Stripe and mark the record |
| `glia_donations.webhooks.stripe_webhook` | guest (signed) | Stripe webhook receiver |

## Install

```sh
bench get-app https://github.com/GliaX/glia-donations --branch v0.1.0
bench --site <site> install-app glia_donations
bench --site <site> migrate  # applies the Donation custom fields
```

Then, in the desk:

1. **Stripe Settings** — make sure the secret key is set (the same key the
   ERPNext Stripe gateway uses).
2. **Donation Settings** — set the webhook signing secret, review min/max and
   the receipt switch.
3. **Stripe → Developers → Webhooks** — add
   `https://<site>/api/method/glia_donations.webhooks.stripe_webhook` with the
   events `checkout.session.completed`, `invoice.paid`,
   `invoice.payment_failed`, `customer.subscription.deleted`, and paste its
   `whsec_…` into Donation Settings.

## Donation bar wiring

The sitewide bar (in `Website Settings.banner_html` for this site) posts the
monthly choice to `start_recurring_donation` and redirects to the returned URL;
the one-time choice keeps going to the donated product page. See
`~/workspace/glia-server/glia-theme/donation-bar.html`.

## Verifying without charging a card

```sh
# create a checkout session and expire it right away (no charge)
bench --site <site> execute glia_donations.api.start_recurring_donation \
  --kwargs '{"email":"test@example.com","full_name":"Test","amount":10}'
# then: stripe.checkout.Session.expire(<session id>)
```

Webhook handling can be exercised with synthetic events signed with the same
secret (`t=<ts>`, `v1=HMAC_SHA256(secret, "<ts>.<payload>")`).

## Limits / next steps

- Monthly only (the code has quarter/year interval support in the model but the
  bar exposes monthly).
- No donor portal beyond cancel: changing amount/card happens through Stripe's
  receipt/billing links.
- Accounting: charges create `Donation` records, not Payment Entries; booking
  to the ledger is still a separate task (Stripe fees, party accounts).
- Receipts: optional simple inline email; not the Glia-branded template yet.

## Licence

MIT — see `license.txt`.
