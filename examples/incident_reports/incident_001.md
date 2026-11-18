# INCIDENT REPORT

**Incident No.:** INC-2025-001  
**Severity:** SEV2  
**Date Reported:** January 21, 2025  

---

## REPORTER
**Name:** Mara Lindqvist  
**Team:** Platform Reliability  
**Role:** On-call engineer  
**Email:** mara.lindqvist@lumenpay.example  

## AFFECTED SERVICE
**Service:** Payments API  
**Owning Team:** Payments Core  
**Environment:** Production  
**Customer Facing:** Yes  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - Error-rate alert fired at 09:12 UTC.
   - The first customer ticket arrived at 09:20 UTC.

2. **Response**
   - The on-call engineer paged the payments team.
   - A rollback of the 09:00 release was started.

3. **Resolution**
   - The rollback finished at 09:58 UTC.
   - Error rates returned to normal at 10:03 UTC.

4. **Customer Communication**
   - A status page notice was posted at 09:25 UTC.
   - Affected merchants received an email the next morning.

### II. ROOT CAUSE

1. **Primary Cause**
   - A schema migration in the 09:00 release locked the transactions table.
   - Requests queued until the connection pool was exhausted.

2. **Contributing Factors**
   - The migration was not rehearsed on production-sized data.
   - The release had no canary stage.

### III. IMPACT

1. **Duration:**
   - Time to detect: 12 minutes
   - Time to mitigate: 46 minutes
   - **Subtotal: 58 minutes**

2. **Users and Data:**
   - About 8,200 payment attempts failed.
   - No payment data was lost.

**TOTAL DOWNTIME: 58 minutes**

### IV. ACTION ITEMS

The team agreed on the following actions:

1. **Rehearse migrations**
   - Run every migration on a production-sized copy first.
   - Owner: Payments Core, due February 14, 2025.

2. **Add a canary stage**
   - Release to 5% of traffic for ten minutes before full rollout.
   - Owner: Platform Reliability, due March 7, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Alert timeline export
- B. Release 2025.01.21 change log
- C. Status page notices

### Reviewers:
1. **Owen Baptiste** - Head of Payments Core
2. **Liv Hargreaves** - Director of Engineering

---

## SIGN-OFF

This report was reviewed and accepted by the reviewers named above.

**Incident Commander**  
Mara Lindqvist  
Platform Reliability  
Email: mara.lindqvist@lumenpay.example  

---

**Date:** January 28, 2025  
**Signature:** [Mara Lindqvist]
