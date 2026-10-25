# Access administration and periodic access review

How access is modelled, how to provision and deprovision internal and partner users, and how to run a periodic access review.

Sources:

- `apps/automotive_core/automotive_core/setup/roles.py` (personas, role profiles, privileged roles)
- `setup/permissions.py` (least-privilege grants)
- `automotive_setup/access_sync.py` (plant and partner access)
- `governance/provisioning.py`, `governance/sod.py`, `governance/delegation.py`, `governance/approvals.py`
- the tenant definition `setup/tenants/hinode.yaml`

All paths are under `apps/automotive_core/automotive_core/` unless shown in full.

## Access model

### Role profiles (personas)

Each persona is a **Role Profile**: one persona role plus the ERPNext roles it needs. Role profiles are upserted on every `bench migrate` (`after_migrate`). Users holding a changed profile are re-synced in the same step.

| Role profile | Roles granted |
|---|---|
| Automotive Executive | Automotive Executive |
| Group Controller | Group Controller, Accounts Manager |
| Plant Director | Plant Director, Manufacturing Manager, Stock User |
| Production Planner | Production Planner, Manufacturing User, Stock User |
| Production Supervisor | Production Supervisor, Manufacturing User |
| Line Operator | Line Operator |
| Warehouse Clerk | Warehouse Clerk, Stock User |
| Quality Inspector | Quality Inspector |
| Quality Manager | Quality Manager |
| Buyer | Buyer, Purchase User |
| Procurement Manager | Procurement Manager, Purchase Manager |
| AP Clerk | AP Clerk, Accounts User |
| Treasury Approver | Treasury Approver |
| Sales Manager | Sales Manager, Sales User |
| Dealer Liaison | Dealer Liaison, Sales User |
| IT Operator | IT Operator, System Manager |
| Auditor | Auditor |
| Product Engineer | Product Engineer |
| Engineering Manager | Engineering Manager, Manufacturing Manager |
| Logistics Coordinator | Logistics Coordinator, Stock User, Delivery User |
| Supplier Portal User | Supplier Portal User (no desk access) |
| Dealer Portal User | Dealer Portal User (no desk access) |

- **Least-privilege grants.** Persona roles get rights on standard ERPNext DocTypes only as listed in `setup/permissions.py`. These are Custom DocPerm rows, re-applied on every migrate.
- **Restricted fields** (permlevel 1; [permission matrix](../security/permission-matrix.md#restricted-fields-and-exports-sec-4)):
  - Supplier bank account number, IBAN and branch code: Treasury Approver and Group Controller read them; Accounts Manager, AP Clerk and System Manager read and write them.
  - An employee's personal details (date of birth, gender, private contact details and addresses, emergency contact, passport, health, family, education and work history, salary and bank details, terms of employment, exit): only ERPNext's HR User and HR Manager roles, which no persona includes. Colleagues, who hold ERPNext's "Employee" role once linked to an Employee record, read the directory fields only.
  - Lists, reports, exports and the API leave these fields out for everyone else.
- **Privileged roles** (flag *Two Factor Authentication* on the Role): System Manager, IT Operator, Treasury Approver, Group Controller, Automotive Executive. They need an OTP app when 2FA is enabled in System Settings. The tenant setup enables it only on sites with `automotive_environment` = `production`. See [security-operations.md](security-operations.md#privileged-mfa).

### Row-level scope

- **User Permissions** on Company, Plant, Warehouse, Supplier or Customer restrict what a user sees and edits. This covers list views, reports, API and link fields. The tenant enables *Apply Strict User Permissions* in System Settings.
- **Managed permissions.** Permissions created by the application carry **Managed By** (`automotive_managed_by`). Values:
  - `Plant Assignment`
  - `Partner User Access`
  - `User Provisioning Request`

  Each source keeps exactly its own set in sync and never touches permissions from another source or ones created by hand.
- **Plant scoping.** Plant-scoped documents carry a Plant that is derived when empty:
  - the scoped DocTypes are Work Order, Job Card, Production Plan, Material Request, Purchase Order, Purchase Receipt, Stock Entry, Quality Inspection, Downtime Entry, Pick List and Delivery Note;
  - when *Enforce Plant Scope* (Automotive Settings) is on, the Plant is required wherever the company operates plants.
- **Employees.** ERPNext's automatic company permission for employees linked to a user is suppressed. Scope comes only from provisioning and plant assignments (DECISIONS.md D-007).

### Segregation of duties (SoD)

The tenant creates these **Segregation of Duties Rules** (`/desk/segregation-of-duties-rule`):

| Rule | Roles | Enforcement |
|---|---|---|
| Buyer vs AP Clerk | Buyer + AP Clerk | Block |
| AP Clerk vs Treasury Approver | AP Clerk + Treasury Approver | Block |
| Buyer vs Treasury Approver | Buyer + Treasury Approver | Block |
| IT Operator vs Treasury Approver | IT Operator + Treasury Approver | Block |
| Auditor vs AP Clerk | Auditor + AP Clerk | Block |
| Warehouse Clerk vs Quality Manager | Warehouse Clerk + Quality Manager | Warn |

- **Block:** saving a User that would hold both roles is refused ("Segregation of duties: ... cannot hold both ..."). A User Provisioning Request asking for both is refused as well.
- **Warn:** the save goes through with an orange message, and the accepted warning is written to the audit chain (`sod.warning_accepted`).
- **Who maintains the rules:** System Manager and IT Operator create and edit them; Group Controller edits; Auditor reads.
- **Delegated authority counts like a held role** (`governance/delegation.py`):
  - An Approval Delegation whose role would break a Block rule for the delegate is refused ("Segregation of duties: … may not act as …"), and a Warn rule shows a warning.
  - An active delegation that becomes a Block conflict later, because the delegate gained a role, grants nothing from then on.
- **Limitation to review manually:** role changes made by `bench migrate` through role profiles skip the SoD validation (it runs in install mode). The [SoD review report](#periodic-access-review) catches them.

### Approval policies and maker/checker

Approval policies from the tenant (`/desk/approval-policy`):

- Purchase Order (JPY and USD companies, amount steps)
- Supplier Payment Release (two Treasury Approver steps, plus Group Controller from 50,000,000)
- Manual Journal Approval
- Period Close Approval
- Account Provisioning (step "IT security review", role IT Operator)

Every policy has:

- **maker cannot approve:** the document owner and the requester are excluded;
- **distinct approvers:** each step needs a different person;
- **allow delegation**;
- **bind to document hash:** changing protected fields invalidates the approval.

Decisions are immutable Approval Decision records; database triggers refuse updates and deletes ([database.md](database.md#immutability-triggers)). A step pending longer than its *Escalate After Hours* is escalated once by the hourly job: its approvers are reminded and the next level is alerted (the next step's approvers; the group controllers after the last step), recorded on the step and in the audit chain.

## Provisioning internal users

Use a **User Provisioning Request** (`/desk/user-provisioning-request`).

- **Create:** System Manager, IT Operator, or Plant Director (Plant Director cannot submit).
- **Submit:** System Manager or IT Operator.

1. Create a request.
   - *Request Type*: Create.
   - Email, First Name, Last Name.
   - *User Type*: System User for staff.
   - *Role Profile*, for example Production Planner.
   - Optional *Extra Roles*.
   - *Scopes*: rows of Company, Plant, Warehouse, Supplier or Customer with a value; mark one as default.
   - Optional *Access Expiry Date* for contractors and temporary staff.
   - *Justification* (required).
2. Save. Validation refuses:
   - an existing email ("use a Modify request");
   - requested roles that break a Block SoD rule;
   - scopes that do not exist;
   - an expiry date in the past.
3. **Request Approval.** A *different* IT Operator approves (policy *Account Provisioning*; the maker cannot approve).
4. **Submit.** The change is applied in one transaction and the request status becomes **Applied**. *Result* shows what was done, for example "Created … with N role(s).". On Create:
   - the user is created with the role profile, or with the explicit role list when extra roles are given;
   - managed User Permissions are created for the scopes;
   - a welcome email is sent (needs an outgoing Email Account; see [install-fresh-host.md](install-fresh-host.md#10-enable-backups-clamav-and-monitoring));
   - the audit event `user.create` is written.

**Modify** an existing user with *Request Type* Modify and *User*. Roles are replaced by the requested set, scopes re-synced and the expiry updated (`user.modify`).

## Deprovisioning

| Situation | Action |
|---|---|
| Normal leaver | User Provisioning Request, *Request Type* **Deactivate**, with justification. Approve and submit as above. |
| Known end date | Set **Access Expires On** on the User, or *Access Expiry Date* in a Create/Modify request. A daily job disables the account after that date (`automotive_core.governance.provisioning.expire_accounts`; reason "access expiry date passed"). |
| Emergency, e.g. compromised account | Disable immediately from the host (below), then record a Deactivate request for the trail. |
| Return | Request Type **Reactivate**. |

**Deactivate** disables the account, ends all its sessions, removes its API key and secret, and writes `user.deactivated`. Administrator and Guest are never touched.

Emergency deactivation from the host (same effect as a Deactivate request, without the approval step):

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
  automotive_core.governance.provisioning.deactivate --kwargs "{'user': 'jane.doe@hinode.example', 'reason': 'compromised account, ticket 1234'}"
```

Disabling a user only via the *Enabled* checkbox on the User form keeps their open sessions and API keys. Prefer the paths above.

## Plant access (internal users)

**Plant Assignment** (`/desk/plant-assignment`):

- **Create:** System Manager or IT Operator; Plant Director can also create and edit.
- **Fields:** Employee, User, Plant, Company, optional Production Line and Department, Assignment Role, From/To Date.
- While an assignment is **Active** and *Grant Plant Access* is ticked (default), the user gets managed User Permissions on the Plant and its Company. The most recent assignment is the default. The Employee's plant follows the most recent active assignment.
- Status (Scheduled, Active, Ended) is recomputed on save and **hourly**. Future-dated assignments activate by themselves, and ended ones remove their permissions. Audit events: `access.plant_assignment.<status>`.
- Overlapping assignments of the same employee to the same plant are refused.
- **To end access:** set *To Date* and save. Deleting the record also ends it.

## Partner (supplier / dealer) portal access

Partner users are **Website Users**. They use only `/supplier-portal` or `/dealer-portal` and the party-scoped portal APIs.

Access is granted by **Partner User Access** (`/desk/partner-user-access`):

- **Full control:** System Manager and IT Operator.
- **Create and edit:** Procurement Manager (suppliers) and Dealer Liaison (dealers).
- **Read:** Auditor.

To give a partner user access:

1. Make sure the User exists with *User Type* Website User. Supplier onboarding creates users and access rows for contacts flagged for portal access. Dealer onboarding does the same.
2. Create a Partner User Access row with:
   - User;
   - Party Type: Supplier, or Customer for dealers;
   - the Supplier or Customer;
   - Partner Role;
   - From Date and optional To Date.
3. Save. While the row is **Active**, the application keeps three things in place:
   - a Contact linked to the partner (created if missing);
   - the portal role (Supplier Portal User or Dealer Portal User);
   - a managed User Permission on that Supplier or Customer.

   ERPNext's generic Supplier and Customer portal roles are removed from partner users on every save.

Rules and behaviour:

- **One partner per portal user:** a second active or scheduled row for a different partner is refused. A desk (System) user cannot get partner access.
- **Status** (Scheduled, Active, Ended, Revoked) is recomputed on save and hourly. Audit events: `access.partner.<status>`.
- **Partner Role** (Administrator, Sales, Logistics, Quality, Finance, Viewer) is recorded but **not used to restrict portal functions yet**. Every active user of a partner sees that partner's portal.
- **To end access:** set *To Date* to the last day and save. The row becomes Ended after that day, or at once if the date is in the past. The contact link, role and permission are removed. Deleting the row also revokes it. There is no Revoke button in the desk; the controller's `revoke()` is not exposed.
- **To block login entirely:** also deactivate the user (User Provisioning Request with *User Type* Website User, Request Type Deactivate).

## Delegated approval authority

**Approval Delegation** (`/desk/approval-delegation`) passes one approver role from a *delegator* to a *delegate*. It covers a date range and can be limited to a Company, a DocType and a maximum amount. It requires a reason.

- The delegator or an IT Operator / System Manager creates and **submits** it.
- Limits:
  - at most *Max Delegation Days* (Automotive Settings, default 90);
  - cannot start in the past;
  - the delegator must hold the role;
  - the delegate must be enabled;
  - no overlap with another delegation of the same role by the same delegator;
  - the role must not break a Block SoD rule for the delegate (Warn rules show a warning).
- **Expiry:** a delegation applies only between From Date and To Date inclusive, only while the delegator is enabled and still holds the role, and only while it does not break a Block SoD rule for the delegate. This is checked when a decision is made, so expiry needs no job. The *Status* field (Draft, Scheduled, Active, Expired, Revoked) is refreshed **hourly** for display.
- **Revoke:** cancel the delegation. A reason is required and recorded (`delegation.revoked`).
- Decisions taken under a delegation record *On Behalf Of* and the delegation on the Approval Decision.

## Periodic access review

Run at the interval your control framework requires, for example quarterly, and after every reorganisation or tenant change. Roles needed: Auditor, IT Operator, System Manager or Group Controller (for the SoD report).

1. **SoD conflicts.** Open the report **Segregation of Duties Review** (`/desk/query-report/Segregation of Duties Review`).
   - It lists every enabled System User whose current roles break an active rule: user, name, rule, both roles and the enforcement.
   - Expected result: no *Block* rows. Remove one of the roles with a Modify request. For *Warn* rows, confirm the rationale is accepted.
   - API equivalent: `GET /api/method/automotive_core.governance.sod.report`.
2. **Privileged accounts.**
   - In the User list, filter on the roles System Manager, IT Operator, Treasury Approver, Group Controller and Automotive Executive, and confirm each holder still needs the role.
   - On production, confirm System Settings → *Enable Two Factor Auth* is on and these roles have *Two Factor Authentication* ticked. Migrations set the flag.
3. **Hand-made permissions.** In User Permission (`/desk/user-permission`), filter *Managed By* = not set. Each row bypasses provisioning: replace it with a provisioning scope or remove it.
4. **Expiring and stale accounts.**
   - Check enabled users with an *Access Expires On* date, and contractors without one.
   - Check users who have not logged in for a long time: the *Last Login* / *Last Active* fields on User.
5. **Partner access.**
   - List Partner User Access with Status Active and confirm each with the partner's owner (Procurement Manager or Dealer Liaison).
   - Confirm that partners which ended the relationship have only Ended or Revoked rows, and that their users are disabled.
6. **Plant access.** List Plant Assignments with Status Active per plant with the Plant Director.
7. **Exports.** Open the **Access Log** (`/desk/access-log`), which records every export (report view, Data Export, report downloads) with the user, DocType or report, file type and filters.
   - Filter on the review period and look for exports of finance and party data (Payment Entry, Journal Entry, Bank Account, Supplier, Customer) and for unusually large or repeated exports.
   - Confirm each exporting user still needs the export right of the persona ([exports per persona](../security/permission-matrix.md#restricted-fields-and-exports-sec-4)).
8. **Delegations.**
   - List Approval Delegations with Status Active or Scheduled, and confirm each is still needed.
   - Block conflicts are refused, or ignored once they arise. Review delegations that were accepted with a *Warn* SoD warning.
9. **Evidence.**
   - Export the SoD report and the lists used (Menu → Export).
   - Record the outcome in your access-review register. **There is no review-record DocType in the application (not implemented yet).**
   - Changes made as a result go through User Provisioning Requests, so they are approved and audited.

## Account recovery

- **Password:** the Reset Password action on the User form (System Manager), or the "Forgot Password" link on `/login`. Both send email, so an outgoing Email Account is needed.
- **Lost OTP device (2FA):** a System Manager opens the User and uses **Password → Reset OTP Secret**. The user enrols again at next login. Verify the person's identity through your own channel before resetting.
- **Administrator password:** see [security-operations.md](security-operations.md#administrator-password).
- **Users who sign in through an identity provider:** recovery is the provider's; the account here only has to stay enabled ([security-operations.md](security-operations.md#identity-provider-sso-and-ldap)).

## Related

- [security-operations.md](security-operations.md): MFA, secrets, partner signing secrets
- [period-close.md](period-close.md): maker/checker in finance
- [troubleshooting.md](troubleshooting.md)
