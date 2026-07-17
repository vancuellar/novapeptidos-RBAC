# SES production-access request (ready to submit once DKIM verifies)

Submit with:

```bash
aws sesv2 put-account-details \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url https://novapeptidos.mx \
  --use-case-description "$(cat <<'EOF'
Nova Peptides (novapeptidos.mx) is an e-commerce store serving customers in Mexico.
We send TRANSACTIONAL email only, triggered by explicit customer actions:
(1) account-creation confirmations and (2) order status notifications.

- Recipients: only customers who created an account or placed an order on our site
  (double opt-in by nature of the action). No marketing, no bulk mail, no purchased lists.
- Volume: very low - under 50 emails/day initially, well under 200/day at scale.
- From address: hola@novapeptidos.mx on our DKIM-verified domain.
- Bounce/complaint handling: SES account-level suppression list is enabled for both
  BOUNCE and COMPLAINT. Hard-bounced and complaining addresses are never mailed again.
- Unsubscribe/contact: every email footer includes our contact address and a note to
  contact us to deactivate the account; transactional-only so no marketing list exists.
- Content: account confirmation in the customer's language (es/en/pt), sender clearly
  identified, physical business contact info in footer.
EOF
)" \
  --additional-contact-email-addresses christiancuellar@gmail.com \
  --contact-language EN \
  --region us-east-1
```

Note: a previous request on this AWS account (jadalegal.com, case 176849385500450) was
DENIED. If this one is denied too, reply to the support case with the details above and
answer their specific questions; if still denied, fall back to a dedicated sender
(e.g. Resend/Brevo free tier) — templates and code are sender-agnostic except
`_send_email_sync()` in emails.py.
