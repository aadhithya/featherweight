# INCIDENT REPORT

**Incident No.:** INC-2025-003  
**Severity:** SEV1  
**Date Reported:** April 7, 2025  

---

## REPORTER
**Name:** Ana Petrov  
**Team:** Infrastructure  
**Role:** Incident commander  
**Email:** ana.petrov@lumenpay.example  

## AFFECTED SERVICE
**Service:** Customer Login  
**Owning Team:** Identity  
**Environment:** Production  
**Customer Facing:** Yes  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - Synthetic login checks failed from 14:03 UTC.
   - Support reported a surge of lockout complaints at 14:10 UTC.

2. **Response**
   - A SEV1 bridge was opened at 14:12 UTC.
   - The identity team found an expired signing certificate.

3. **Resolution**
   - A new certificate was deployed at 14:41 UTC.
   - Logins fully recovered at 14:49 UTC.

4. **Customer Communication**
   - The status page was updated at 14:20 UTC and every 30 minutes after.
   - A summary was sent to enterprise customers the same evening.

### II. ROOT CAUSE

1. **Primary Cause**
   - The token signing certificate expired without being renewed.
   - The renewal reminder went to a mailbox nobody read.

### III. IMPACT

1. **Duration:**
   - Time to detect: 3 minutes
   - Time to mitigate: 46 minutes
   - **Subtotal: 49 minutes**

2. **Users and Data:**
   - Roughly 31,000 users could not sign in.
   - No credentials were exposed.

**TOTAL DOWNTIME: 49 minutes**

### IV. ACTION ITEMS

The review committee set these actions:

1. **Automate renewal**
   - Renew signing certificates automatically.
   - Owner: Identity, due May 2, 2025.

2. **Monitor expiry**
   - Alert 30 days before any certificate expires.
   - Owner: Infrastructure, due April 25, 2025.

3. **Fix the reminder route**
   - Send renewal reminders to the team channel.
   - Owner: Infrastructure, due April 18, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Bridge call notes
- B. Certificate inventory
- C. Customer summary email

### Reviewers:
1. **Owen Baptiste** - Head of Identity
2. **Liv Hargreaves** - Director of Engineering
3. **Tariq Aziz** - Chief Technology Officer

---

## SIGN-OFF

This report was reviewed and accepted by the review committee.

**Incident Commander**  
Ana Petrov  
Infrastructure  
Email: ana.petrov@lumenpay.example  

---

**Date:** April 15, 2025  
**Signature:** [Ana Petrov]
