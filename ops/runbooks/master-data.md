# Master data

Items, suppliers, customers (dealers) and the plant structure: the rules that keep them
clean, their lifecycle and history, and how to load or extract them in bulk (ERP-01).
Who may create or change each master is in [the permission matrix](../security/permission-matrix.md)
and the role profiles ([access-review.md](access-review.md)).

## Duplicate prevention

Enforced on every save, whatever the source (form, API, integration, bulk import):

| Master | Refused when |
|---|---|
| Supplier, Customer | another active one has the same **tax ID** or **partner code**, or a name **92 % or more similar** after normalisation (case, punctuation, and legal forms such as Co., Ltd., Inc., KK, GmbH, 株式会社, 合同会社 removed) |
| Item | another active item has the same **supplier part number** for the same supplier, or the same **name in the same item group** (a template's variants excepted) |

The rules come from `automotive_core.access.master_data`. A near-duplicate partner name that
is legitimate (two plants of one group, say) can only be accepted in a reviewed onboarding
flow (Supplier Onboarding Approval, Dealer Registration), which records the acceptance in the
audit chain as `master_data.similar_name_accepted` with the similar record and the score. A
bulk import cannot accept one: such rows are refused.

## Lifecycle, ownership and history

- **Disable, do not delete.** An item, supplier or customer that is no longer used is
  disabled; documents keep their links, and ERPNext refuses it on new documents. The
  duplicate rules compare only active records, so a disabled one does not block its
  successor.
- **Onboarding.** New suppliers come through Supplier Onboarding Approval and new dealers
  through Dealer Registration; both are approved before the party is created.
- **History.** Changes are kept as Versions (the form's activity) on the master DocTypes;
  audited DocTypes also write Audit Events to the hash chain
  ([monitoring.md](monitoring.md#audit-chain-verification)).
- **Bills of materials** change through engineering change orders, not by editing a
  submitted BOM.
- **Substitutes** too: an ECO change *Approve Alternative* creates ERPNext's Item Alternative
  for a component, linked to the ECO and valid from its effective date. Production may consume
  an alternative only when an ECO approved it and it is valid on the posting date; an Item
  Alternative entered by hand is refused at consumption. Cancelling the ECO removes it, unless
  it has been consumed.

## Bulk import

Use Frappe's **Data Import** (`/desk/data-import`, or *Import* in a DocType's list menu):

1. Choose the DocType and *Insert New Records* (or *Update Existing Records*, matched on the
   record's ID).
2. *Download Template*: a CSV or Excel file with the mandatory columns; add the columns you
   need. Fill one row per record (child table rows below their parent).
3. Upload the file and check the preview, then *Start Import*. It runs as a background job;
   the import log lists each row's result and the reason of every failure.
4. *Export Errored Rows* gives a file of the refused rows only: fix them there and import
   that file, so the rows that succeeded are not loaded twice.

Every row is saved as a form save would be: the duplicate rules above, mandatory fields,
link validation and the approval rules apply. Documents that need an approval are imported
as drafts (leave *Submit After Import* off) and go through it.

**Who.** Importing needs the DocType's *Import* right. ERPNext gives it to System Manager and
its master-manager roles (Item Manager for items, Purchase Master Manager for suppliers, Sales
Master Manager for customers). Of the personas only IT Operator holds it, through System
Manager; the business personas do not. For a migration by a business user, grant the
master-manager role through a User Provisioning Request with an expiry
([access-review.md](access-review.md#provisioning-internal-users)) and let it lapse.

**Practice.** Import into a staging site first and check the result there (counts, a few
records, the duplicate refusals), then repeat on production. Keep the import files: with the
import log they are the record of what was loaded.

## Bulk export

The list and report views export what they show (*Menu → Export*, CSV or Excel), for users
with the DocType's *Export* right; row-level scope (company, plant, partner) applies, so a
user exports only what they may read. Keep exports to the columns needed (data
minimisation, §6.3) and store them as the data they contain requires.
