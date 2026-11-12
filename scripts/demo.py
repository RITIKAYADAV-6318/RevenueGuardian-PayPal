#!/usr/bin/env python3
"""Run the Revenue Guardian invoice loop against a running API, step by step.

  python scripts/demo.py --base https://revenue-guardian-api.onrender.com --email sb-xxxx@personal.example.com --wait 120
  python scripts/demo.py --simulate-payment            # local mock mode, no PayPal account needed

The password is read from the RG_PASSWORD environment variable or asked for (never passed on the command line).
Needs only the `httpx` package.
"""
import argparse
import getpass
import os
import sys
import time

import httpx


class Failure(Exception):
    pass


def call(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise Failure(f"Could not reach the API ({exc}). Is the address right and the service awake?") from exc
    if response.status_code >= 400:
        raise Failure(f"{method} {path} -> HTTP {response.status_code}: {response.text[:400]}")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("RG_BASE_URL", "http://localhost:8000"), help="API address")
    parser.add_argument("--user", default="cfo", help="demo user (cfo, admin or sales)")
    parser.add_argument("--email", default="buyer@example.com", help="the BUYER's email (a sandbox Personal account in sandbox mode)")
    parser.add_argument("--name", default="Test Buyer")
    parser.add_argument("--amount", type=float, default=100.0)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--simulate-payment", action="store_true", help="local mock mode only: mark the invoice paid with a SIMULATED event")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="after sending, wait up to this long for PayPal to confirm payment")
    parser.add_argument("--sweep", action="store_true", help="also run the overdue-invoice sweep and show the AI-drafted reminders")
    args = parser.parse_args()

    password = os.environ.get("RG_PASSWORD") or getpass.getpass(f"Password for '{args.user}': ")
    with httpx.Client(base_url=args.base.rstrip("/"), timeout=60.0) as client:
        try:
            health = call(client, "GET", "/health")
            print(f"1. API is up. PayPal mode: {health.get('paypal_mode')}, Gemini configured: {health.get('gemini_configured')}")

            token = call(client, "POST", "/auth/login", json={"username": args.user, "password": password})["access_token"]
            client.headers["Authorization"] = f"Bearer {token}"
            print(f"2. Logged in as {args.user}.")

            payload = {"customer_email": args.email, "customer_name": args.name, "amount": args.amount, "currency": args.currency,
                       "description": "Revenue Guardian demo invoice", "due_days": 7}
            created = call(client, "POST", "/api/approvals", json={"action_type": "create_paypal_invoice", "target": args.name, "payload": payload, "reason": "Demo script"})
            approval_id = created["approval_id"]
            print(f"3. Invoice proposal #{approval_id} created. Status: {created['status']} (nothing has been sent yet; a human must approve).")

            decided = call(client, "POST", f"/api/approvals/{approval_id}/decision", json={"decision": "approved"})
            print(f"4. Approved. Result status: {decided['status']}")
            if decided["status"] != "approved":
                print("   PayPal or the policy check refused it:")
                print("  ", (decided.get("execution_result") or {}).get("message"))
                return 1

            invoice = next((r for r in call(client, "GET", "/api/invoices") if r["proposal_id"] == approval_id), {})
            print(f"5. Invoice {invoice.get('paypal_invoice_id') or '(mock)'} is {invoice.get('status')} ({invoice.get('mode')}).")
            if invoice.get("payer_link"):
                print(f"   Pay it as the sandbox BUYER (private browser window): {invoice['payer_link']}")

            if args.simulate_payment:
                result = call(client, "POST", f"/api/invoices/{approval_id}/simulate-payment")
                print(f"6. Simulated payment: {result['status']} (labelled SIMULATED, never counted as a real payment).")

            if args.wait:
                print(f"6. Waiting up to {args.wait}s for PayPal's verified payment event ...")
                deadline = time.time() + args.wait
                while time.time() < deadline:
                    row = next((r for r in call(client, "GET", "/api/invoices") if r["proposal_id"] == approval_id), {})
                    if row.get("status") == "PAID":
                        print(f"   PAID. Confirmation: {row.get('paid_provenance')}")
                        break
                    time.sleep(5)
                else:
                    print("   Not paid yet. Pay the invoice, then run this script's checks again (events/KPIs below).")

            if args.sweep:
                swept = call(client, "POST", "/api/invoices/sweep")
                print(f"7. Overdue sweep: {swept['proposed']} reminder(s) proposed, {swept['ai_drafted']} drafted by Gemini, {swept['template_fallback']} from the template.")
                for proposal in swept["approvals"]:
                    ai = proposal["payload"].get("ai_draft", {})
                    print(f"   [{ai.get('provider')}] {proposal['payload']['note']}")

            events = call(client, "GET", "/api/events", params={"limit": 5})
            print("8. Latest webhook events:", [(e["event_type"], e["provenance"]) for e in events] or "none yet")
            kpis = call(client, "GET", "/api/kpis")
            print("9. KPIs: sent={invoices_sent} paid={invoices_paid} paid_after_guardian_action={paid_after_guardian_action} (verified amount {amount_paid_verified}) simulated/unverified={paid_unverified_or_simulated}".format(**kpis))
            audit = call(client, "GET", "/api/audit/verify")
            print(f"10. Audit chain valid: {audit['valid']} ({audit['checked']} records).")
            return 0
        except Failure as exc:
            print(f"\nStopped: {exc}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
