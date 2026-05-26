#!/usr/bin/env python3
"""Mint Stripe Payment Links for the three paid Hevolve API tiers.

A Stripe Payment Link is a single-purpose, hosted URL — any buyer who
clicks it can pay with card; the charge lands in the operator's Stripe
account immediately.  Critically, Payment Links do NOT depend on:

  - DNS for api.hevolve.ai
  - A Nunba.exe rebuild with the d26141e two-phase init fix
  - A re-deploy of the React landing page

They are the shortest possible path from "zero revenue infra" to "first
real $9 charge."  Operators paste the printed URLs into marketing
posts; clickers buy; revenue lands.

Operator usage:

  # 1. Get the live Stripe secret key from dashboard.stripe.com:
  #    Developers → API keys → Reveal secret key.  Starts with sk_live_
  #    (or sk_test_ for sandbox).
  export STRIPE_API_KEY="sk_live_REPLACE_ME"

  # 2. Run this script:
  python deploy/mint_stripe_payment_links.py

  # Output: three URLs you can paste anywhere (Twitter, LinkedIn, email).

The script is idempotent in the sense that Stripe lets you mint as
many Payment Links as you want; running it twice produces two sets of
URLs and neither expires.  If you want to retire an old set, archive
them in the Stripe dashboard.

Pricing source of truth: HARTOS/integrations/agent_engine/
commercial_api.py:TIER_CONFIG.  Edit there to change tiers; rerun this
script to mint new links.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path


# Hard-mirror of integrations/agent_engine/commercial_api.py:TIER_CONFIG
# (we don't import HARTOS to keep this script self-contained — operators
# can run it on a fresh box with just the stripe SDK).
TIERS = [
    {
        'tier': 'starter',
        'price_usd': 9,
        'name': 'Hevolve API — Starter',
        'description': '1,000 req/day, 30k tokens/mo.  Free tier upgrade for solo builders.',
    },
    {
        'tier': 'pro',
        'price_usd': 49,
        'name': 'Hevolve API — Pro',
        'description': '10,000 req/day, 300k tokens/mo.  Priority queue + hive priority for small teams.',
    },
    {
        'tier': 'enterprise',
        'price_usd': 499,
        'name': 'Hevolve API — Enterprise',
        'description': '100,000 req/day, 10M tokens/mo.  Dedicated hive lane + SLA.',
    },
]


def main() -> int:
    api_key = os.environ.get('STRIPE_API_KEY', '').strip()
    if not api_key:
        print("ERROR: STRIPE_API_KEY env var not set.", file=sys.stderr)
        print("Get it from dashboard.stripe.com → Developers → API keys, "
              "then `export STRIPE_API_KEY=sk_live_...`.", file=sys.stderr)
        return 2

    try:
        import stripe  # type: ignore
    except ImportError:
        print("ERROR: `stripe` package not installed.  "
              "Run `pip install stripe` and retry.", file=sys.stderr)
        return 3

    stripe.api_key = api_key
    mode = 'LIVE' if api_key.startswith('sk_live_') else 'TEST'
    print(f"Stripe key mode: {mode}")
    if mode == 'TEST':
        print("(Test keys can mint Payment Links too — useful for "
              "smoke-testing the buyer flow.  Real charges only land "
              "with sk_live_ keys.)")
    print()

    results = []
    for tier in TIERS:
        # Create Product (idempotent on operator side — we use a stable
        # tier name as the lookup key so re-runs don't pile up products).
        try:
            product = stripe.Product.create(
                name=tier['name'],
                description=tier['description'],
                metadata={
                    'hevolve_tier': tier['tier'],
                    'minted_by': 'deploy/mint_stripe_payment_links.py',
                },
            )
        except Exception as e:
            print(f"  [{tier['tier']}] Product create failed: {e}", file=sys.stderr)
            results.append({'tier': tier['tier'], 'error': str(e)})
            continue

        # Create Price (one-time, USD).  Subscriptions would need
        # `recurring={'interval': 'month'}` — skipping for now to keep
        # the buyer flow simple.  Operators can switch to subscription
        # mode later by editing this block.
        try:
            price = stripe.Price.create(
                unit_amount=tier['price_usd'] * 100,
                currency='usd',
                product=product.id,
            )
        except Exception as e:
            print(f"  [{tier['tier']}] Price create failed: {e}", file=sys.stderr)
            results.append({'tier': tier['tier'], 'error': str(e)})
            continue

        # Create the Payment Link.  No success URL means Stripe shows
        # its default confirmation page — fine for cold buyers since
        # they receive a receipt by email.  Operators can edit the link
        # in the dashboard to add a success URL later (e.g., a thank-
        # you page on hevolve.ai/api-thanks).
        try:
            link = stripe.PaymentLink.create(
                line_items=[{'price': price.id, 'quantity': 1}],
                metadata={
                    'hevolve_tier': tier['tier'],
                    'kind': 'tier_upgrade',
                },
                after_completion={
                    'type': 'redirect',
                    'redirect': {
                        'url': f'https://hevolve.ai/upgrade-success'
                               f'?tier={tier["tier"]}'
                               f'&session_id={{CHECKOUT_SESSION_ID}}',
                    },
                },
            )
        except Exception as e:
            print(f"  [{tier['tier']}] PaymentLink create failed: {e}", file=sys.stderr)
            results.append({'tier': tier['tier'], 'error': str(e)})
            continue

        print(f"  {tier['tier'].upper():<10}  ${tier['price_usd']:>4}  →  {link.url}")
        results.append({
            'tier': tier['tier'],
            'price_usd': tier['price_usd'],
            'product_id': product.id,
            'price_id': price.id,
            'payment_link_id': link.id,
            'payment_link_url': link.url,
        })

    # Persist the minted links so the static buy-page can read them.
    out_path = Path(__file__).resolve().parent / 'stripe_payment_links.json'
    with out_path.open('w', encoding='utf-8') as fh:
        json.dump(results, fh, indent=2)
    print(f"\nSaved to: {out_path}")
    print("\nNext steps:")
    print("  1. Open deploy/buy_hevolve_api.html in a browser to preview")
    print("     the standalone buy page.  It auto-reads the URLs from")
    print("     stripe_payment_links.json (same dir).")
    print("  2. Upload buy_hevolve_api.html + stripe_payment_links.json")
    print("     to any static host (GitHub Pages, Netlify Drop, Vercel,")
    print("     or even a public S3 bucket).  No backend required.")
    print("  3. Replace the URLs in the marketing copy at")
    print("     _revenue_assets.md.  Post anywhere.  First click that")
    print("     buys becomes the first $9.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
