"""Platform-level org subscription billing (Paystack adapter).

Separate from the existing per-campaign Stitch payment links
(integrations/stitch.py, unrelated) - this package is the platform's own
subscription billing for an organisation's use of Kryx itself: plans,
add-ons, coupons, a Paystack payment provider adapter, and the engine
that drives subscription lifecycle (start, confirm, plan changes,
recurring billing, manual comp overrides).
"""
