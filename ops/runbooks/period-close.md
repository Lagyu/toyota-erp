# Period close

Closing an accounting period for one company. Use the **Period Close Checklist**, which:

- runs automated ledger checks,
- records waivers with reasons,
- needs approval by a second controller,
- closes an ERPNext **Accounting Period** when submitted.

This runbook also covers the group consolidation view and the FX revaluation check.

Source: `apps/automotive_core/automotive_core/finance_controls/period_close.py` (core checks and workflow) and `apps/automotive_supply_chain/automotive_supply_chain/finance_checks.py` (supply-chain checks registered through the `automotive_period_close_checks` hook).

## Prerequisites

- **Two different users with the Group Controller role.**
  - The tenant baseline creates the approval policy **Period Close Approval**: one step, "Second controller review", role Group Controller.
  - Like every tenant policy, it sets *maker cannot approve*, *distinct approvers* and *bind to document hash*. The person who created the checklist or requested approval cannot approve it.
  - An active **Approval Delegation** of the Group Controller role can stand in for the second controller (policies allow delegation). See [access-review.md](access-review.md#delegated-approval-authority).
- The period lies inside one Fiscal Year of the company (tenant fiscal years: FY2025 to FY2027, April to March).
- **Exchange rates.** Currency Exchange records exist for the period if the company holds foreign-currency balances (FX check) or you run the consolidation view in another currency.
- Desk access: `https://<site>/desk/period-close-checklist`.
  - Group Controller and System Manager can create, run checks and submit.
  - Auditor, Automotive Executive, Plant Director, Procurement Manager and Sales Manager can read.

## The checks

Every new checklist gets one task per registered check:

| Code | What it verifies | Waivable |
|---|---|---|
| `TB-BALANCED` | Sum of GL debits = sum of credits up to period end (tolerance 0.01) | no |
| `NO-DRAFTS` | No draft Sales Invoice, Purchase Invoice, Journal Entry or Payment Entry dated in the period (lists up to 5 per type) | yes |
| `AR-AP-LEDGER` | Receivable and payable GL balances equal the Payment Ledger (tolerance 1) | no |
| `STOCK-GL` | Stock ledger value equals the GL balance of the warehouses' stock accounts | no |
| `FX-REVALUED` | If the company has foreign-currency receivable, payable, bank or cash balances, a submitted **Exchange Rate Revaluation** is dated in the period (or the day after period end) | yes |
| `AUDIT-CHAIN` | Seals pending audit events, then verifies the whole audit hash chain | no |
| `MATCH-EXCEPTIONS` | No purchase invoice of the period has an Open or Rejected three-way-match exception | no |
| `INTERCOMPANY` | Intercompany sales and purchase invoices agree for every group partner pair involving the company | yes |

## Procedure

1. **Create the checklist.** In the desk, open Period Close Checklist and click New.
   - Fill Company, Fiscal Year, Period Start and Period End, then save.
   - Validation refuses a period outside the fiscal year, or one overlapping a submitted checklist of the same company.
   - On save, status is **Open** and every task is **Pending**.
2. **Run the checks.** Click **Run Checks** (Group Controller or System Manager).
   - Each task becomes **Passed** or **Failed** and shows its result text, for example `debit 123.0, credit 123.0`.
   - The checklist status becomes **Ready For Approval** when every task is Passed or Waived, otherwise **Checks Failed**.
   - *Checks Run At* is set and an audit event `period_close.checks_run` is written.
3. **Resolve failures**, then click Run Checks again. Typical fixes:
   - `NO-DRAFTS`: submit or delete the listed drafts.
   - `FX-REVALUED`: create and submit an Exchange Rate Revaluation for the company with a posting date inside the period.
   - `MATCH-EXCEPTIONS`: decide the listed Invoice Match Exceptions.
   - `INTERCOMPANY`: reconcile the unmatched intercompany invoices named in the result.
   - `TB-BALANCED`, `AR-AP-LEDGER`, `STOCK-GL`: treat a failure as a ledger integrity incident. Do not close the period. Escalate to the finance system owner.
   - `AUDIT-CHAIN`: a break means audit records were altered. Follow [monitoring.md](monitoring.md#audit-chain-verification).
4. **Waive a check (only if justified).**
   - For waivable checks only (`NO-DRAFTS`, `FX-REVALUED`, `INTERCOMPANY`), a Group Controller clicks **Waive a Check**, picks the failed check and enters a reason of at least 10 characters.
   - The task becomes **Waived** with *Waiver Reason* and *Waived By*, and an audit event `period_close.check_waived` is written.
   - Running the checks again keeps the waiver while the check still fails. If the check now passes, it becomes Passed and the waiver fields are cleared.
5. **Request approval.**
   - With status Ready For Approval, use **Approval → Request Approval** on the form. This creates an Approval Request under *Period Close Approval*.
   - The second Group Controller approves with **Approval → Approve**, or from the approval inbox (`/desk/approval-inbox`). Rejecting requires a comment.
6. **Submit** the checklist. Submission is refused unless:
   - status is Ready For Approval and every task is Passed or Waived ("Run the checks and resolve or waive every failure before closing the period."), and
   - an Approved request exists for it ("... needs approval under policy Period Close Approval before it can be submitted.").

   On submit:
   - An **Accounting Period** named `<company> <start> - <end>` is created. If one already exists for exactly these dates it is re-enabled instead.
   - All period-closing document types of the installed apps are marked closed. ERPNext's list includes Sales/Purchase Invoice, Journal Entry, Payment Entry, Stock Entry, Delivery Note, Purchase Receipt, Stock Reconciliation and others.
   - The checklist gets status **Closed** and a link to the Accounting Period, and an audit event `period_close.closed` is written.

## Verification

- The checklist shows status Closed and the *Accounting Period* link. The Accounting Period (`/desk/accounting-period`) is not disabled and every closed-documents row is ticked.
- Try to post (or cancel) a closed document type dated inside the period, for example a draft Journal Entry. ERPNext must refuse it because of the closed accounting period.
- The audit trail (Audit Event, filter Reference Name = the checklist) shows `period_close.checks_run`, any `period_close.check_waived`, the `approval.*` events and `period_close.closed`.

## Re-opening a period (rollback)

1. **Cancel** the submitted checklist (Group Controller or System Manager).
   - Period Close Checklist is a governed cancellation: the Cancel action asks for a reason of at least 10 characters, which goes to the audit chain and the timeline.
   - On cancel, the linked Accounting Period is **disabled**. The record stays, so postings in the period are possible again.
   - The checklist status becomes **Reopened** and an audit event `period_close.reopened` is written.
2. Post the corrections.
3. Close again: **Amend** the cancelled checklist, or create a new one for the same dates. Run the checks and request approval again (approval is per document), then submit. Submitting re-enables the same Accounting Period.

## Group consolidation view

Report **Group Consolidation** (`/desk/query-report/Group Consolidation`). Roles: Group Controller, Automotive Executive, Auditor, System Manager.

- **Filters:**
  - *Period From*: defaults to the fiscal-year start for *As Of*.
  - *As Of*: defaults to today.
  - *Presentation Currency*: defaults to JPY.
  - *Company*: optional. Without it, every company the user may see; company User Permissions apply.
- **Rows and columns:**
  - Rows: Asset, Liability, Equity, Income, Expense, Net Profit (Income - Expense), Currency Translation Difference.
  - Columns: one per company, *Group Total*, *of which Intercompany*, *Group excl. Intercompany*.
- **Translation:**
  - Balance-sheet lines use the closing rate: the latest Currency Exchange on or before *As Of*.
  - Profit-and-loss lines use the average Currency Exchange rate of the period, falling back to the closing rate.
  - Without any Currency Exchange record for a needed pair, the report stops with "No exchange rate X → Y on or before <date>". Add the rate and rerun.
- Every figure is a GL Entry sum.
- **Limitation (by design, DECISIONS.md D-021):** unrealised profit in intercompany inventory is not eliminated. This is a management view, not statutory consolidation.

## Failure handling

| Symptom | Cause / action |
|---|---|
| Run Checks raises a permission error | Only Group Controller or System Manager may run checks; only Group Controller may waive |
| "Check X cannot be waived." | The check is not waivable; fix the underlying data |
| "The maker of ... cannot approve it (maker/checker)." | A second Group Controller (or an active delegate) must approve |
| Approval shows "Approved ... but the document changed afterwards" | Request approval again; the request is bound to a hash of the protected fields |
| Submit refused after approval | Run Checks again: a task may have changed to Failed after new postings |

## Related

- [access-review.md](access-review.md): approval policies, delegation, SoD
- [monitoring.md](monitoring.md#audit-chain-verification): audit chain
- [troubleshooting.md](troubleshooting.md)
