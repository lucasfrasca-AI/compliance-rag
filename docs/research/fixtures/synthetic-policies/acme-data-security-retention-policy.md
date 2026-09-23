---
title: Acme Pty Ltd — Data Security and Retention Policy (Internal)
author: synthetic-fixture (drafted by Claude Sonnet 5 for issue #11)
reviewed_by: pending — see docs/research/synthetic-policy-set.md
date: 2026-09-23
---

# Acme Pty Ltd — Data Security and Retention Policy (Internal)

This internal policy sets out the technical and organisational controls
Acme applies to personal information it holds, referenced by section 5 of
our public Privacy Policy.

## 1. Encryption

1.1 Personal information is encrypted at rest using AES-256 and in transit
using TLS 1.2 or higher, across all production systems.

## 2. Access controls

2.1 Access to systems holding customer personal information is restricted
to employees whose role requires it, granted on a least-privilege basis
and reviewed quarterly by IT Security.

2.2 All access to the customer database is logged, and logs are retained
for 12 months.

## 3. Retention

3.1 Customer account information is retained for as long as the account
is active, plus 7 years after closure to meet our obligations under tax
and consumer law, after which it is securely destroyed.

3.2 Payment card details are never retained by Acme; these are handled
exclusively by our PCI-DSS-compliant payment provider.

## 4. Breach response

4.1 Any suspected data breach must be reported to IT Security immediately.
IT Security will assess the breach within 24 hours and, where the breach is
likely to result in serious harm, escalate to the Privacy Officer for
notification under the Notifiable Data Breaches scheme.

4.2 Notifications to affected individuals and to the OAIC will be made as
soon as practicable after Acme becomes aware of an eligible data breach.
