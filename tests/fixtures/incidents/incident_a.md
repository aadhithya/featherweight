# INCIDENT REPORT

**Incident No.:** INC-FX-201  
**Severity:** SEV2  
**Date Reported:** August 5, 2025  

---

## REPORTER
**Name:** Greta Holm  
**Team:** Search  
**Role:** On-call engineer  
**Email:** greta.holm@pebblecart.example  

## AFFECTED SERVICE
**Service:** Product Search  
**Owning Team:** Search  
**Environment:** Production  
**Customer Facing:** Yes  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - Latency alert fired at 16:02 UTC.

2. **Response**
   - The index was found to be rebuilding on all nodes at once.

3. **Resolution**
   - Rebuilds were staggered and latency recovered at 16:55 UTC.

4. **Customer Communication**
   - The status page was updated at 16:15 UTC.

### II. ROOT CAUSE

1. **Primary Cause**
   - A scheduled rebuild started on every node together.

2. **Contributing Factors**
   - The schedule had no jitter.

### III. IMPACT

1. **Duration:**
   - Time to detect: 5 minutes
   - Time to mitigate: 48 minutes
   - **Subtotal: 53 minutes**

2. **Users and Data:**
   - Search results took over ten seconds for 40 minutes.

**TOTAL DOWNTIME: 53 minutes**

### IV. ACTION ITEMS

The team agreed on these actions:

1. **Stagger rebuilds**
   - Add random jitter to the schedule.
   - Owner: Search, due August 29, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Latency graphs
- B. Scheduler configuration

### Reviewers:
1. **Rune Dahl** - Head of Search

---

## SIGN-OFF

The report was reviewed and accepted.

**Incident Commander**  
Greta Holm  
Search  
Email: greta.holm@pebblecart.example  

---

**Date:** August 12, 2025  
**Signature:** [Greta Holm]
