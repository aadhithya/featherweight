# INCIDENT REPORT

**Incident No.:** INC-2025-002  
**Severity:** SEV3  
**Date Reported:** February 18, 2025  

---

## REPORTER
**Name:** Kofi Mensah  
**Team:** Data Engineering  
**Role:** Data engineer  
**Email:** kofi.mensah@lumenpay.example  

## AFFECTED SERVICE
**Service:** Nightly Reporting Pipeline  
**Owning Team:** Data Engineering  
**Environment:** Production  
**Customer Facing:** No  

---

## INCIDENT SUMMARY

### I. TIMELINE

1. **Detection**
   - The morning freshness check failed at 06:30 UTC.

2. **Response**
   - The data engineer on duty traced the failure to the ingest job.
   - The job was restarted with a larger memory limit.

3. **Resolution**
   - Dashboards were fully refreshed at 08:10 UTC.

### II. ROOT CAUSE

1. **Primary Cause**
   - A partner file arrived with three times the usual number of rows.
   - The ingest job ran out of memory.

2. **Contributing Factors**
   - The memory limit had not changed in two years.

### III. IMPACT

1. **Duration:**
   - Time to detect: 30 minutes
   - Time to mitigate: 70 minutes
   - **Subtotal: 100 minutes**

2. **Users and Data:**
   - Finance dashboards were stale for the morning.
   - No data was lost.

**TOTAL DOWNTIME: 100 minutes**

### IV. ACTION ITEMS

The following actions were agreed:

1. **Raise the memory limit**
   - Set the ingest limit from row volume.
   - Owner: Data Engineering, due February 28, 2025.

---

## FOLLOW-UP

### Related Documents:
- A. Pipeline run log
- B. Partner file size history

### Reviewers:
1. **Ines Carvalho** - Head of Data Engineering

---

## SIGN-OFF

The report was reviewed by the Head of Data Engineering and closed.

**Incident Commander**  
Kofi Mensah  
Data Engineering  
Email: kofi.mensah@lumenpay.example  

---

**Date:** February 21, 2025  
**Signature:** [Kofi Mensah]
