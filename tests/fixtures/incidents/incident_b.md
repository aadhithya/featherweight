# INCIDENT REPORT

**Incident No.:** INC-FX-202  
**Severity:** SEV3  
**Date Reported:** September 9, 2025  

---

## REPORTER
**Name:** Theo Vance  
**Team:** Internal Tools  
**Role:** Engineer  
**Email:** theo.vance@pebblecart.example  

## AFFECTED SERVICE
**Service:** Warehouse Label Printing  
**Owning Team:** Internal Tools  
**Environment:** Production  
**Customer Facing:** No  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - Warehouse staff reported blank labels at 07:15 UTC.

2. **Response**
   - A font update on the print server was identified.

3. **Resolution**
   - The font was rolled back at 09:00 UTC.

### II. ROOT CAUSE

1. **Primary Cause**
   - A server update replaced the barcode font.

2. **Contributing Factors**
   - Label output was not covered by any automated check.
   - The update ran without a maintenance notice.

### III. IMPACT

1. **Duration:**
   - Time to detect: 15 minutes
   - Time to mitigate: 105 minutes
   - **Subtotal: 120 minutes**

2. **Users and Data:**
   - Around 600 parcels were labelled by hand.

**TOTAL DOWNTIME: 120 minutes**

### IV. ACTION ITEMS

The following actions were agreed:

1. **Add a label check**
   - Print and scan a test label after every server update.
   - Owner: Internal Tools, due September 30, 2025.

2. **Announce maintenance**
   - Post every server update in the warehouse channel.
   - Owner: Internal Tools, due September 19, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Print server change log
- B. Photos of the blank labels

### Reviewers:
1. **Rune Dahl** - Head of Internal Tools
2. **Kaya Brandt** - Warehouse Operations Lead

---

## SIGN-OFF

The reviewers accepted this report.

**Incident Commander**  
Theo Vance  
Internal Tools  
Email: theo.vance@pebblecart.example  

---

**Date:** September 16, 2025  
**Signature:** [Theo Vance]
