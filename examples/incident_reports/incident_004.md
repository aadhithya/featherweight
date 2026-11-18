# INCIDENT REPORT

**Incident No.:** INC-2025-004  
**Severity:** SEV3  
**Date Reported:** May 12, 2025  

---

## REPORTER
**Name:** Jonas Eklund  
**Team:** Mobile  
**Role:** Release manager  
**Email:** jonas.eklund@lumenpay.example  

## AFFECTED SERVICE
**Service:** Mobile App Notifications  
**Owning Team:** Mobile  
**Environment:** Production  
**Customer Facing:** Yes  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - A support agent noticed missing push notifications at 11:40 UTC.

2. **Response**
   - The Mobile team confirmed the provider key had been rotated.
   - The new key was added to the configuration.

3. **Resolution**
   - Notifications resumed at 12:25 UTC.

4. **Customer Communication**
   - Affected users were told in the app on the next launch.

### II. ROOT CAUSE

1. **Primary Cause**
   - The push provider key was rotated by Security without notice.
   - The app configuration still held the old key.

2. **Contributing Factors**
   - There was no shared calendar of key rotations.

### III. IMPACT

1. **Duration:**
   - Time to detect: 40 minutes
   - Time to mitigate: 45 minutes
   - **Subtotal: 85 minutes**

2. **Users and Data:**
   - About 5,400 users missed payment confirmations by push.
   - Email confirmations were sent as usual.

**TOTAL DOWNTIME: 85 minutes**

### IV. ACTION ITEMS

The following actions were recorded:

1. **Share the rotation calendar**
   - Publish planned key rotations in the engineering calendar.
   - Owner: Security, due May 30, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Provider console audit log
- B. Support ticket list

### Reviewers:
1. **Ines Carvalho** - Head of Mobile
2. **Liv Hargreaves** - Director of Engineering

---

## SIGN-OFF

The reviewers accepted this report and the actions above.

**Incident Commander**  
Jonas Eklund  
Mobile  
Email: jonas.eklund@lumenpay.example  

---

**Date:** May 16, 2025  
**Signature:** [Jonas Eklund]
