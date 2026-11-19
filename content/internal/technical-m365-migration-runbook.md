---
title: Microsoft 365 migration runbook
kind: technical
---
## Before migration
- Inventory mailboxes, shared mailboxes, distribution lists and their sizes.
- Check licences: Business Premium is our default for firms under 300 users (Intune, Defender, Conditional Access).
- Lower DNS TTL on MX and autodiscover to 300 seconds at least 48 hours ahead.
- Enable MFA for all admin accounts before anything else.

## Pilot
- 5 to 10 users from different teams, including one heavy calendar user and one mobile-only user.
- Run the pilot for at least 3 business days; collect issues in the client's ticket queue.

## Cutover
- Migrate mail in batches outside business hours; final delta sync before switching MX.
- Switch MX, autodiscover and SPF; add DKIM and DMARC (start DMARC at p=none, move to quarantine after 2 weeks of clean reports).
- Re-point scanners and line-of-business apps that send mail (SMTP relay or authenticated submission).

## After
- Conditional Access: block legacy authentication, require MFA, require compliant devices for admins.
- Hand over: admin guide, licence list, and a 30-minute walkthrough for the client's IT contact.
- Hypercare for 10 business days, then normal support.
