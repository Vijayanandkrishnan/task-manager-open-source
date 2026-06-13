# Agency Task Manager Complete Feature Guide

This guide documents the public open-source Agency Task Manager feature set as shipped in this repository. It is written for owners, admins, team members, sales users, accountants, implementers, and contributors.

The app is a self-hosted Flask and SQLite operations system for small agencies and service teams. It combines tasks, attendance, leave, clients, projects, invoices, leads, reports, Telegram workflows, and JSON APIs in one application.

## Table of Contents

- [What The System Does](#what-the-system-does)
- [Core Concepts](#core-concepts)
- [Roles And Permissions](#roles-and-permissions)
- [First Run And Login](#first-run-and-login)
- [Daily Workflows](#daily-workflows)
- [Task Management](#task-management)
- [Attendance And Check-in/Check-out](#attendance-and-check-incheck-out)
- [Leave Management](#leave-management)
- [Clients And Projects](#clients-and-projects)
- [Lead Management And Follow-ups](#lead-management-and-follow-ups)
- [Lead Intake From Website, Telegram, And WhatsApp](#lead-intake-from-website-telegram-and-whatsapp)
- [Telegram Integration](#telegram-integration)
- [Invoices And Billing](#invoices-and-billing)
- [Reports And Analytics](#reports-and-analytics)
- [Project Reviews](#project-reviews)
- [Admin And Team Management](#admin-and-team-management)
- [Settings, Branding, And System Configuration](#settings-branding-and-system-configuration)
- [Notifications, Comments, And Activity Log](#notifications-comments-and-activity-log)
- [Import, Export, And Backup](#import-export-and-backup)
- [API Guide](#api-guide)
- [Deployment Notes](#deployment-notes)
- [Data Safety](#data-safety)
- [Recommended Setup For A New Agency](#recommended-setup-for-a-new-agency)

## What The System Does

Agency Task Manager is designed around everyday agency operations:

- Staff check in and check out.
- Employees complete recurring and one-time tasks.
- Managers see pending work, backlog, attendance, and performance.
- Admins manage clients, projects, tasks, leaves, reports, and users.
- Sales or owners track enquiries through a lead pipeline.
- Leads can enter from website forms, Telegram, WhatsApp webhooks, or the API.
- Follow-up reminders can go to Telegram.
- Approved leads can be converted into clients.
- Invoices can be created, sent, marked paid, exported as PDF, and auto-generated for recurring billing.
- HR and activity reports can be delivered to Telegram as text or image summaries.
- Most major modules are available through JSON APIs for automation.

The app uses SQLite by default, so a small agency can self-host it without running a separate database server.

## Core Concepts

### Employees

Employees are user accounts. Each employee has:

- Name and username
- Password
- Role
- Optional email, phone, department
- Optional Telegram chat ID
- Optional profile image
- Birthday, joining date, and wedding anniversary for celebration reminders
- Optional shift assignment
- Optional hourly rate for cost/performance reporting

### Roles

Roles control access. The built-in roles are:

- `employee`
- `sales`
- `accountant`
- `subadmin`
- `manager`
- `admin`
- `superadmin`

The UI labels these as Member, Sales, Accountant, Team Lead, Manager, Admin, and Owner.

### Clients

Clients represent customers or companies the agency serves. Clients can have contact person, email, phone, address, notes, projects, invoices, and converted leads.

### Projects

Projects group work under a client. Tasks and reports should be linked to projects so completed work can be measured and billed properly.

### Tasks

The app has three work buckets:

- Recurring tasks: routine work that repeats daily, weekly, monthly, or on selected days.
- One-time tasks: ad hoc work that should be done once.
- Backlog tasks: pending work ideas or unassigned work that can later become recurring or one-time tasks.

### Attendance

Attendance records are check-in and check-out events by employee, date, and time. Admins can add, edit, delete, and export records where permitted.

### Leads

Leads track sales opportunities before they become clients. The default lead stages are:

- `enquiry`
- `discussion`
- `confirmation`
- `on_hold`
- `converted`

By default, lead access is owner-only in the open-source seed policy. The Permissions page and API can be used to grant lead permissions to other roles.

### Invoices

Invoices contain client, invoice number, invoice date, due date, line items, discounts, total, currency, remarks, status, company profile, and optional recurring billing settings.

### Settings

Settings store branding, Telegram configuration, SMTP/email configuration, public base URL, timezone, currency defaults, HR report destinations, and lead reminder destinations.

## Roles And Permissions

Permissions are not hardcoded only in templates. The app has a permission catalog and a `role_permissions` table. Superadmin always has all permissions. Other roles can be changed from the Permissions page or by API.

### Default role behavior

| Role | Default purpose |
| --- | --- |
| Employee | Personal dashboard, own tasks, own backlog, own KPI, calendar, own leave application |
| Sales | Employee permissions plus client visibility by default |
| Accountant | Employee permissions plus invoices, paid marking/PDF access, and leave review defaults |
| Subadmin | Team lead access: team list, team tasks, attendance view, reports view, reviews, project/client visibility |
| Manager | Subadmin plus KPI, audit log, invoice view, leave approvals, review management, attendance edits |
| Admin | Manager plus team creation/deletion/import, shifts, clients/projects/invoices CRUD, report scheduling/email |
| Superadmin | Full owner access to every permission |

### Permission groups

Personal:

- View Kanban Board
- View Calendar
- View Own Performance
- Manage Own Tasks
- Manage Own Backlog

Team:

- View Team Member List
- View Other Members' Tasks
- Add/Edit/Delete Team Members
- Reset Member Passwords
- Import Team from Excel
- Assign Tasks to Others

Attendance:

- View Team Attendance
- Edit Attendance Records
- Delete Attendance Records
- Export Attendance Data
- Manage Shift Timings
- Manage Holidays and Weekly Off
- Apply for Own Leave
- View Team Leave Requests
- Approve or Reject Leave

Insights:

- View Team Performance/KPIs
- Export Performance Data
- View Activity/Audit Log
- Export Audit Log

Clients:

- View/Add/Edit/Delete Clients
- Import Clients from Excel

Leads:

- View Lead Pipeline
- Add/Edit/Delete Leads
- Manage Lead Follow-ups
- Convert Leads to Clients

Projects:

- View/Add/Edit/Delete Projects
- Manage Project Links and Files
- Import Projects from Excel

Invoices:

- View/Create/Edit/Delete Invoices
- Send Invoices to Clients or Accountant
- Mark Invoices as Paid
- Generate Invoice PDFs
- Import Invoices from Excel
- Manage Recurring Billing

Reports:

- View Reports
- Generate Custom Reports
- Schedule Auto-Reports
- Export/Download Reports
- Email Reports to Stakeholders

Reviews:

- View Project Reviews
- Schedule and Manage Reviews

System:

- Manage Branding and Logo
- Manage Email/SMTP Settings
- Manage Telegram Integration
- Manage Currencies
- Manage Company Profiles
- Manage Other System Settings
- Manage Role Permissions

## First Run And Login

On first run, the app creates a local SQLite database named `tasks.db`.

It also creates two first-run users if they do not exist:

- `admin`
- `superadmin`

Passwords are generated and printed once to the terminal or service logs. You can override them before the first start:

```bash
export INITIAL_ADMIN_PASSWORD="change-this-admin-password"
export INITIAL_OWNER_PASSWORD="change-this-superadmin-password"
```

The app marks these accounts as `must_change_password`, so users must rotate the password after first login.

Security defaults include:

- Password hashing with bcrypt
- CSRF protection for non-API form posts
- API authentication by `X-API-Key`
- SQLite WAL mode
- Max POST body size
- Security headers
- Login failure tracking

## Daily Workflows

### Employee daily flow

1. Log in.
2. Check in from the daily page.
3. Review today's recurring tasks.
4. Complete recurring tasks with time and notes.
5. Complete one-time tasks with project, time, and notes.
6. Add comments when needed.
7. Check out at the end of the day.

### Manager/admin daily flow

1. Review Dashboard or Admin Dashboard.
2. Check attendance and late check-ins.
3. Review backlog and unassigned tasks.
4. Review daily task completions and time logged.
5. Approve or reject leave requests.
6. Check lead follow-ups and sales pipeline.
7. Review daily HR report, monthly attendance, invoices, and reports.

### Owner/superadmin daily flow

1. Review team performance and activity.
2. Track lead pipeline and overdue follow-ups.
3. Review invoices, unpaid reminders, and recurring billing.
4. Manage permissions, integrations, and company settings.
5. Use Telegram notifications to reduce manual checking.

## Task Management

### Today page

The Today page is the main employee work surface. It shows:

- Current check-in/check-out status
- Tasks due today
- Recurring tasks
- One-time tasks
- Backlog items
- Task completion controls
- Timer save flow
- Comments

### My Dashboard

The personal dashboard gives each employee their own view of:

- Task progress
- Recurring task completion
- One-time task status
- Backlog
- Activity and daily work
- Performance signals

### Recurring tasks

Recurring tasks are for routine work such as daily posting, weekly reports, monthly checks, SEO tasks, or repeated client operations.

Supported task fields include:

- Employee
- Title
- Description
- Frequency
- Frequency day
- Frequency month
- Selected frequency days
- Scheduled time
- Drive link
- Project
- Billable flag
- Active/inactive status

Recurring tasks can be completed for the current date and then uncompleted if a correction is needed.

Completion records store:

- Task ID
- Completion date
- Time spent in minutes
- Notes

### One-time tasks

One-time tasks are for ad hoc work. They support:

- Employee
- Title
- Description
- Drive link
- Project
- Completion status
- Completion date
- Time spent
- Notes

One-time tasks are useful when a client asks for something outside the recurring plan.

### Backlog

Backlog is a holding area for pending work that should not disappear. A backlog item can later be converted into:

- Recurring task
- One-time task

Backlog fields include:

- Employee
- Title
- Description
- Drive link
- Project
- Priority

### Unassigned task backfill

Admins can open the unassigned tasks page to find recurring or one-time tasks without a project. From there they can assign projects individually or in bulk.

### Kanban

The Kanban page gives a board-style view of work. It is useful for scanning active and pending work without only relying on lists.

### Calendar

The calendar gives a date-based view of completions, attendance, or task activity depending on the template data in use.

### Task comments

Comments can be added to recurring and one-time tasks. This gives teams a lightweight discussion trail without leaving the task record.

## Attendance And Check-in/Check-out

### Check-in

Employees can check in from the app. A check-in creates an attendance row with:

- Employee ID
- `checkin` action
- Date
- Time

The app prevents duplicate open check-in behavior from confusing the active state.

### Check-out

Employees can check out when finished. A check-out creates an attendance row with:

- Employee ID
- `checkout` action
- Date
- Time

### Admin attendance

Admins with permission can:

- View attendance records
- Add attendance entries
- Edit attendance entries
- Delete attendance entries
- Export attendance
- Import attendance from Excel

### Shifts

Admins can manage shift timings. Shifts include:

- Name
- Start time
- End time

Employees can be assigned to a shift. Late check-in reminders use the employee shift start time.

### Holidays and weekly off

Admins can manage:

- Holiday list
- Monthly holiday planner
- Weekly off days
- Holiday import template
- Holiday import from Excel

Attendance and leave calculations respect holidays and weekly off days.

The monthly holiday planner lets an admin select a month and mark specific dates, such as a local holiday or a second/third Saturday, before monthly attendance summaries are generated. By default it only adds newly selected dates and skips duplicates. Admins can optionally replace the selected month's specific holidays when they want the month to exactly match the checked calendar.

### Late check-in reminder

The app can remind employees one hour after shift start if no check-in is recorded.

Example:

- Shift starts at `09:00`
- Reminder triggers after `10:00`
- Approved leave and off days are skipped

Reminders can go to:

- Employee private Telegram chat
- General Telegram group, if enabled for attendance events

### Checkout reminder

If someone remains checked in for 10 hours, the app can send a checkout reminder.

The reminder can go to:

- Employee private Telegram chat
- General Telegram group, if enabled for attendance events

This helps catch missed check-outs without automatically assuming bad intent.

### Midnight continuity reminder

If a team member continues work past midnight, the app can remind them to check in again for the new attendance day.

This is useful because many attendance systems reset at midnight. The reminder asks the employee to create a new day record instead of losing continuity.

### Absentee report

The app can send a daily attendance report to Telegram. It includes:

- Present count
- Approved leave count
- Absent count
- Total staff
- Approved leave list
- Absent list grouped by shift

Reports are skipped on holidays and weekly off days.

### Monthly attendance summary

Monthly attendance can be sent to a configured HR/attendance Telegram chat. It counts:

- Working days
- Present days
- Approved leave days
- Absent days
- Joining-date adjustment for new employees

Monthly HR reports should use a dedicated HR group rather than a general employee group.

## Leave Management

The leave system lets employees apply for leave and lets reviewers approve, reject, or cancel requests.

### Leave types

Default seeded types include common leave categories such as paid and unpaid leave. Admins with leave permission can:

- Add leave types
- Toggle active/inactive leave types
- Use codes for API creation

### Applying for leave

Employees can apply with:

- Leave type
- Start date
- End date
- Start day part
- End day part
- Reason
- Handover notes

Day parts include:

- Full day
- First half
- Second half

### Working-day calculation

Leave days are calculated from working days only. The system excludes:

- Holidays
- Weekly off days

Half-day requests count as `0.5`.

### Overlap protection

The app blocks overlapping pending or approved leave requests for the same employee.

### Review workflow

Reviewers can:

- Approve
- Reject
- Cancel

Each review can include notes. The app stores leave history for status changes.

### Leave notifications

The app creates in-app notifications for reviewers and employees. If Telegram chat IDs are configured, employees can also receive leave status updates on Telegram.

## Clients And Projects

### Clients

Client records store:

- Name
- Contact person
- Email
- Phone
- Address
- Notes

Client features include:

- Add/edit/delete
- Excel import
- Template download
- Project association
- Invoice association
- Lead conversion target

Clients with invoices are protected from deletion until related invoices are handled.

### Projects

Project records store:

- Client
- Name
- Description
- Services
- Status
- Project links/files

Project features include:

- Add/edit/delete
- Excel import
- Template download
- Link management
- Task association
- Report generation

Project linkage matters because reports, cost estimates, and completed work summaries depend on project assignment.

## Lead Management And Follow-ups

Leads are intended for enquiries before they become clients.

### Lead fields

Each lead can store:

- Company name
- Contact person
- Email
- Phone
- Source
- Stage
- Estimated value
- Requirement
- Notes
- Assigned owner
- Created by
- Converted client
- Converted timestamp
- Next follow-up time

### Lead stages

The default stages are:

| Stage | Meaning |
| --- | --- |
| Enquiry | New or initial lead |
| Discussion | Conversation is happening |
| Confirmation | Close to final confirmation |
| On Hold | Paused or waiting |
| Converted | Won and converted to client |

### Lead pipeline page

The lead page supports:

- Stage filters
- Assigned-to filters
- Due follow-up filters
- Search by company/contact/email/phone/requirement
- Pipeline value
- Stage counts
- Unassigned count
- Due today count
- Overdue count

### Lead detail page

The detail page includes:

- Full lead record
- Stage history
- Follow-up list
- Assigned owner
- Contact details
- Requirement and notes
- Conversion controls

### Follow-ups

Follow-ups can store:

- Due date/time
- Note
- Status
- Created by
- Telegram sent timestamp
- Completed timestamp

Follow-ups can be pending, done, or cancelled.

### Convert lead to client

When a lead is converted:

- A new client is created from lead contact fields.
- The lead stage becomes `converted`.
- Pending follow-ups are cancelled.
- Stage history records the conversion.
- Activity is logged.

## Lead Intake From Website, Telegram, And WhatsApp

Lead intake sources are token-protected public endpoints. Each source has:

- Name
- Source type: website, Telegram, or WhatsApp
- Token
- Enabled flag
- Default lead stage
- Default assignee
- Allowed origins
- Success message
- Event log

### Website form intake

Website forms can post to:

```text
POST /api/v1/lead-intake/<token>
```

Accepted field names are flexible. The app maps aliases such as:

- `company`, `business`, `organization` to `company_name`
- `name`, `full_name`, `contact` to `contact_person`
- `phone`, `mobile`, `whatsapp` to `phone`
- `message`, `requirement`, `service`, `enquiry`, `inquiry` to `requirement`
- `budget`, `value`, `deal_value` to `estimated_value`
- `followup_date`, `follow_up_date`, `reminder_date` to follow-up date

Supported UTM fields:

- `utm_source`
- `utm_medium`
- `utm_campaign`
- `utm_term`
- `utm_content`

The public form endpoint supports CORS with the source's configured allowed origins.

It also supports a honeypot field:

- `_lead_hp`
- `_hp`
- `honeypot`

If present, the request is treated as spam and ignored while still returning a success-style response.

### Telegram intake

Telegram intake receives bot webhook updates:

```text
POST /api/v1/lead-intake/telegram/<token>
```

The webhook can process:

- Lead creation
- Sales group connection
- Lead edit commands
- Lead stage commands
- Follow-up commands
- HR group connection
- Wrong HR report deletion command

### WhatsApp intake

WhatsApp webhook verification and lead creation use:

```text
GET  /api/v1/lead-intake/whatsapp/<token>
POST /api/v1/lead-intake/whatsapp/<token>
```

GET handles webhook verification using:

- `hub.mode`
- `hub.verify_token`
- `hub.challenge`

POST parses WhatsApp text messages. Messages must look like lead messages, such as:

```text
Lead:
Company: Acme Industries
Name: Ravi
Phone: +91...
Requirement: Website redesign
```

## Telegram Integration

Telegram is one of the standout workflow features. It lets the system send operational reminders where teams already respond.

### Telegram settings

Configure:

- Bot token
- General group chat ID
- Accountant/finance chat ID
- Sales Telegram group ID
- Lead reminder chat ID
- HR/monthly attendance chat ID
- Daily staff report chat ID
- Daily staff report time
- General attendance notifications toggle
- General celebrations toggle
- General notifications toggle

### General group

The general group can be used for:

- Check-in events
- Check-out events
- Late check-in reminders
- Checkout reminders
- Midnight continuity reminders
- Celebrations

HR and sales reports should use separate groups where possible.

### HR group

Add the bot to a Telegram group with `hr` or `humanresources` in the title. Send:

```text
/hrgroup
```

Aliases:

```text
/attendancegroup
/reportsgroup
```

The app connects monthly attendance and daily staff reports to that group.

### Sales group

Add the bot to a Telegram group with `sales` in the title. Send:

```text
/salesgroup
```

Aliases:

```text
/connectsales
/saleschat
```

The app connects lead notifications and lead commands to that group.

### Telegram lead creation

Example:

```text
/lead
Company: Acme Industries
Name: Ravi
Phone: +91...
Email: ravi@example.com
Requirement: Website redesign
Followup date: 2026-05-05
Followup time: 10:00
```

Compact pipe format is also supported:

```text
/lead Acme Industries | Ravi | +91... | ravi@example.com | Website redesign
```

### Telegram lead commands

```text
/leadhelp
/leads
/leads due
/leads today
/leadinfo 12
/leadstage 12 discussion
/followup 12 2026-05-05 10:00 Call decision maker
/leadedit 12
Stage: confirmation
Phone: +91...
Requirement: Updated requirement
```

### Telegram HR report deletion

If the bot posts a wrong HR report, an authorized admin or superadmin can reply to the bot's report message with:

```text
/deletereport
```

Aliases:

```text
/deletewrongreport
/deletebotreport
```

The bot must be an admin in the Telegram group with delete permission.

## Invoices And Billing

Invoices are built for small service teams that need lightweight billing inside the same operations tool.

### Invoice list

The invoice page supports:

- Client filter
- Status filter
- Monthly summary
- INR/USD style dual-currency totals
- Paid and pending totals

### Invoice creation

Invoices include:

- Client
- Invoice number
- Invoice date
- Due date
- Currency
- Company profile
- Drive link
- Line items
- Quantity
- Unit price
- Discount percentage
- Discount amount
- Total
- Remarks
- Billing frequency
- Billing days

### Invoice statuses

Common statuses include:

- Draft
- Sent
- Paid

### PDF generation

Invoice PDFs use:

- Invoice details
- Client details
- Line items
- Currency symbol
- Company profile branding if selected

### Sending invoices

Sending an invoice marks it as sent and can notify the accountant chat ID on Telegram.

### Mark paid/unpaid

Superadmins can toggle paid status. Paid invoices store a paid date.

### Public invoice share

Invoices can generate a share token and expose a public client portal URL:

```text
/portal/invoice/<token>
```

### Recurring billing

Invoices can be configured to auto-generate future invoices by billing frequency. The scheduler checks due templates and creates the next invoice.

### Unpaid invoice reminders

The scheduler can send unpaid invoice summaries to the accountant chat ID.

## Reports And Analytics

### Project reports

Reports aggregate completed recurring and one-time tasks by project and date range.

They include:

- Entries
- Employees
- Tasks
- Daily totals
- Total minutes/hours
- Cost estimates based on hourly rate
- Task count
- Contributor count
- Currency

### Client reports

Client reports can aggregate across all projects for a client.

### Report date ranges

Supported ranges include:

- This week
- Last week
- This month
- Last month
- Last 30 days
- Custom range

### PDF reports

Reports can be rendered as PDF when the PDF library is installed. If PDF rendering is unavailable, the app falls back to print-styled HTML.

### Email reports

Reports can be emailed with:

- To recipients
- CC recipients
- Subject
- Summary
- Optional branding
- Optional PDF attachment

SMTP must be configured first.

### Scheduled reports

Admins can schedule project reports:

- Weekly
- Biweekly
- Monthly

Schedules include:

- Project
- Frequency
- Frequency day
- Recipients
- CC
- Subject template
- Branding toggle
- Active/inactive status

Reports can also be manually run from the schedule page.

### Daily staff activity report

The daily staff activity report is designed for HR or leadership. It can summarize:

- Attendance span
- Logged task count
- Logged work minutes
- Planned recurring task completion
- Main work logged
- Review/quality note
- People needing follow-up

The app can send this to Telegram as an image if Pillow is available, with text fallback.

### KPI and performance pages

KPI views help inspect:

- Team performance
- Task throughput
- Completion behavior
- Time logged
- Activity trends
- Member-level contribution

### Activity/audit log

The audit log tracks important user actions, task completions, lead changes, settings changes, and admin actions.

## Project Reviews

Project reviews help agencies schedule internal client/project reviews.

Features include:

- Review configuration by project
- Frequency: weekly, biweekly, monthly, quarterly
- Participant selection
- Scheduled review instances
- Pending/completed status
- Minutes of Meeting
- Completion notifications
- Review deletion

## Admin And Team Management

### Admin home

Admins can manage:

- Employees
- Imports
- Tasks
- Telegram test messages
- Password resets
- User roles

### Employee management

Employee operations include:

- Add employee
- Edit employee
- Delete employee
- Reset password
- Assign role
- Assign shift
- Add Telegram chat ID
- Add department/contact/profile data
- Import from Excel
- Download template

Admin deletion cleans up related tasks, attendance, leave, activity, and logs for that employee. Admin/superadmin deletion is protected in API deletion.

### Employee profile

Employees can update:

- Email
- Phone
- Department
- Birthday
- Joining date
- Wedding anniversary
- Profile image

Profile images are stored as data URIs and limited to 2 MB.

### Celebrations

The app can detect and notify upcoming:

- Birthdays
- Work anniversaries
- Wedding anniversaries

Telegram celebration notifications can be sent to the general group when enabled.

## Settings, Branding, And System Configuration

### General settings

Superadmins can configure:

- App name
- Public base URL
- Timezone
- Primary color
- Dark mode setting
- App logo
- Company name
- Company logo URL
- Company address
- Company email
- Company phone

### Telegram settings

Configure:

- Bot token
- General group chat ID
- Accountant chat ID
- Sales chat ID
- Lead reminder chat ID
- HR/monthly report chat ID
- Daily staff report chat ID/time
- General notification toggles

### SMTP settings

Configure:

- SMTP host
- SMTP port
- SMTP user
- SMTP password
- From email
- Enabled flag

SMTP is required for email reports and outgoing client emails.

### Currency settings

Currency management supports:

- Seeded currencies
- Add currency
- Delete non-default currency
- Set default currency
- Manual exchange rate update
- Exchange rate refresh

Seeded currencies include INR, USD, EUR, GBP, AED, SGD, AUD, CAD, and JPY.

### Company profiles

Company profiles allow white-label invoice/report branding. Profiles include:

- Name
- Logo URL
- Address
- Email
- Phone
- Default flag

This is useful when one installation serves multiple agency brands or legal entities.

## Notifications, Comments, And Activity Log

### In-app notifications

Notifications can be created for:

- Leave review
- Leave status changes
- Review schedules
- Review completion
- General system events

### Telegram notifications

Telegram can send:

- Task reminders
- Check-in reminders
- Checkout reminders
- Midnight continuity reminders
- Attendance reports
- Daily HR reports
- Lead intake alerts
- Lead follow-up reminders
- Invoice reminders
- Celebrations
- Custom API notifications

### Comments

Task comments attach to recurring or one-time tasks.

### Activity log

The activity log records actions with:

- Employee ID
- Action
- Task type
- Task title
- Description
- Completion date
- Time minutes
- Notes
- Actor ID
- Actor role

## Import, Export, And Backup

### Excel imports

The app includes import flows and templates for:

- Employees
- Tasks
- Clients
- Projects
- Attendance
- Invoices
- Holidays

### Exports

Export features include:

- Attendance export
- KPI/performance export where enabled
- Audit log export where enabled
- All data export
- Invoice PDF
- Report PDF
- SQLite backup download

### Backups

Admins with system permission can download a SQLite snapshot. The app uses SQLite's online backup API so snapshots are safer than copying the active database file directly.

Runtime backups are ignored by Git.

## API Guide

The app exposes two kinds of HTTP endpoints:

- Authenticated operational API endpoints under `/api/v1`
- Public lead intake endpoints protected by source tokens

### API authentication

Authenticated API requests require:

```http
X-API-Key: <deploy_key>
```

The deploy key is stored in settings as `deploy_key`. A superadmin can retrieve it from:

```text
GET /api/deploy-key
```

Do not expose this key publicly.

### API response format

Most endpoints return JSON with either:

```json
{"status": "created"}
```

or:

```json
{"error": "not_found"}
```

Common status codes:

| Code | Meaning |
| --- | --- |
| 200 | Success |
| 201 | Created |
| 400 | Invalid request |
| 401 | Missing or invalid API key |
| 403 | Permission or policy denied |
| 404 | Record not found |
| 409 | Conflict |
| 413 | Payload too large |
| 503 | Health check degraded |

### Health endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Basic app/database health |
| GET | `/api/health/full` | Full health: DB quick check, scheduler lock, disk |

### Lead intake source API

Authenticated source management:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/lead-intake/sources` | List sources with event counts and endpoint URLs |
| POST | `/api/v1/lead-intake/sources` | Create source |
| PUT | `/api/v1/lead-intake/sources/<source_id>` | Update source |
| POST | `/api/v1/lead-intake/sources/<source_id>/rotate` | Rotate token |
| DELETE | `/api/v1/lead-intake/sources/<source_id>` | Delete source |

Source payload fields:

```json
{
  "name": "Website Contact Form",
  "source_type": "website",
  "enabled": true,
  "default_stage": "enquiry",
  "assigned_to": 0,
  "allowed_origins": "https://example.com",
  "success_message": "Thanks, we received your enquiry."
}
```

Public intake endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST/OPTIONS | `/api/v1/lead-intake/<token>` | Website form lead intake |
| POST | `/api/v1/lead-intake/telegram/<token>` | Telegram bot webhook |
| GET/POST | `/api/v1/lead-intake/whatsapp/<token>` | WhatsApp verification and message webhook |

Website form example:

```bash
curl -X POST "https://tasks.example.com/api/v1/lead-intake/<token>" \
  -H "Content-Type: application/json" \
  -d '{
    "company": "Acme Industries",
    "name": "Ravi",
    "phone": "+91...",
    "email": "ravi@example.com",
    "requirement": "Website redesign",
    "utm_source": "google"
  }'
```

### Leads API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/leads/stages` | List allowed lead stages |
| GET | `/api/v1/leads/stats` | Lead pipeline stats |
| GET | `/api/v1/leads` | List leads |
| POST | `/api/v1/leads` | Create lead |
| GET | `/api/v1/leads/<lead_id>` | Get lead with follow-ups/history |
| PUT | `/api/v1/leads/<lead_id>` | Update lead |
| DELETE | `/api/v1/leads/<lead_id>` | Delete lead |
| POST/PUT | `/api/v1/leads/<lead_id>/stage` | Update stage |
| GET | `/api/v1/leads/<lead_id>/history` | List stage history |
| GET | `/api/v1/leads/<lead_id>/followups` | List follow-ups |
| POST | `/api/v1/leads/<lead_id>/followups` | Create follow-up |
| PUT | `/api/v1/leads/followups/<followup_id>` | Update follow-up |
| POST | `/api/v1/leads/followups/<followup_id>/complete` | Mark follow-up done/cancelled |
| DELETE | `/api/v1/leads/followups/<followup_id>` | Delete follow-up |
| POST | `/api/v1/leads/<lead_id>/convert` | Convert lead to client |

Lead list filters:

- `stage`
- `assigned_to` or `assigned`
- `due=due`
- `due=today`
- `due=none`
- `q`
- `limit`

Create lead example:

```bash
curl -X POST "https://tasks.example.com/api/v1/leads" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <deploy_key>" \
  -d '{
    "company_name": "Acme Industries",
    "contact_person": "Ravi",
    "phone": "+91...",
    "email": "ravi@example.com",
    "source": "Website",
    "stage": "enquiry",
    "estimated_value": 50000,
    "requirement": "Website redesign",
    "followup": {
      "due_at": "2026-07-01 10:00",
      "note": "Call decision maker"
    }
  }'
```

### Roles and permissions API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/roles` | List roles, permissions, matrix |
| PUT | `/api/v1/roles/<role>/permissions` | Update permissions |

Update example:

```json
{
  "permissions": {
    "view_leads": true,
    "create_lead": true,
    "manage_lead_followups": true
  }
}
```

Superadmin permissions cannot be disabled.

### Employees API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/employees` | List employees |
| GET | `/api/v1/employees/<emp_id>` | Get employee |
| POST | `/api/v1/employees` | Create employee |
| PUT | `/api/v1/employees/<emp_id>` | Update employee |
| DELETE | `/api/v1/employees/<emp_id>` | Delete non-admin employee |

Create employee required fields:

- `name`
- `username`
- `password`

Optional fields:

- `role`
- `telegram_chat_id`
- `email`
- `phone`
- `department`
- `birthday`
- `joining_date`
- `wedding_anniversary`

### Clients API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/clients` | List clients |
| GET | `/api/v1/clients/<client_id>` | Get client |
| POST | `/api/v1/clients` | Create client |
| PUT | `/api/v1/clients/<client_id>` | Update client |
| DELETE | `/api/v1/clients/<client_id>` | Delete client |

Client fields:

- `name`
- `contact_person`
- `email`
- `phone`
- `address`
- `notes`

### Projects API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/projects` | List projects |
| GET | `/api/v1/projects/<project_id>` | Get project |
| POST | `/api/v1/projects` | Create project |
| PUT | `/api/v1/projects/<project_id>` | Update project |
| DELETE | `/api/v1/projects/<project_id>` | Delete project |

Create project requires:

- `name`
- `client_id`

Optional:

- `description`
- `services`
- `status`

### Tasks API

Recurring tasks:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/tasks/recurring` | List recurring tasks |
| POST | `/api/v1/tasks/recurring` | Create recurring task |
| PUT | `/api/v1/tasks/recurring/<task_id>` | Update recurring task |
| DELETE | `/api/v1/tasks/recurring/<task_id>` | Delete recurring task |

One-time tasks:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/tasks/oneoff` | List one-time tasks |
| POST | `/api/v1/tasks/oneoff` | Create one-time task |
| PUT | `/api/v1/tasks/oneoff/<task_id>` | Update one-time task |
| DELETE | `/api/v1/tasks/oneoff/<task_id>` | Delete one-time task |

Task list filters:

- `employee_id`
- `status=pending`
- `status=completed`

Task creation requires:

- `employee_id`
- `title`
- `project_id`

### Invoices API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/invoices` | List invoices |
| GET | `/api/v1/invoices/<inv_id>` | Get invoice with items |
| POST | `/api/v1/invoices` | Create invoice |
| PUT | `/api/v1/invoices/<inv_id>` | Update invoice |
| DELETE | `/api/v1/invoices/<inv_id>` | Delete invoice |

Filters:

- `client_id`
- `status`

Create invoice example:

```json
{
  "client_id": 1,
  "invoice_number": "INV-202607-0001",
  "invoice_date": "2026-07-01",
  "due_date": "2026-07-15",
  "currency": "INR",
  "status": "draft",
  "items": [
    {"description": "Monthly SEO retainer", "quantity": 1, "unit_price": 25000}
  ]
}
```

### Attendance API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/attendance` | List attendance records |
| POST | `/api/v1/attendance` | Record check-in/check-out |

Filters:

- `employee_id`
- `from`
- `to`

Create attendance example:

```json
{
  "employee_id": 1,
  "action": "checkin",
  "date": "2026-07-01",
  "time": "09:00:00"
}
```

### Leave API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/leave-types` | List active leave types |
| GET | `/api/v1/leaves` | List leave requests |
| POST | `/api/v1/leaves` | Create leave request |
| PUT/POST | `/api/v1/leaves/<leave_id>/status` | Approve/reject/cancel leave |

Leave filters:

- `status`
- `employee_id`
- `from`
- `to`
- `limit`

Create leave example:

```json
{
  "employee_id": 1,
  "leave_type_code": "casual",
  "start_date": "2026-07-10",
  "end_date": "2026-07-10",
  "start_day_part": "full",
  "end_day_part": "full",
  "reason": "Personal work",
  "handover_notes": "Daily tasks covered by Meena"
}
```

Update leave status example:

```json
{
  "status": "approved",
  "reviewed_by": 2,
  "review_note": "Approved"
}
```

### Notifications and activity API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/celebrations` | Upcoming birthdays/anniversaries |
| POST | `/api/v1/send-notification` | Send Telegram notification |
| GET | `/api/v1/activity-log` | Activity log |
| GET | `/api/v1/settings` | App settings except deploy key |
| PUT | `/api/v1/settings` | Update app settings |
| GET | `/api/v1/comments` | List task comments |
| POST | `/api/v1/comments` | Create task comment |
| GET | `/api/v1/notifications` | List in-app notifications |

Send notification example:

```json
{
  "target": "employee",
  "employee_id": 1,
  "message": "Please check today's tasks."
}
```

Supported notification targets:

- `employee`
- `group`
- `accountant`
- `chat_id`

### Deploy API

The app includes a deploy endpoint:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/deploy` | Write files from a base64 payload |
| GET | `/api/deploy-key` | Superadmin-only deploy key view |

This is powerful and should be disabled, firewall-protected, or carefully reviewed before public production use.

## Deployment Notes

The repo includes `setup_instance.sh` for a basic Linux/systemd deployment:

```bash
sudo ./setup_instance.sh acme-agency 5051 tasks.example.com
```

It copies the checked-out source to `/opt/task-manager-<client>`, creates a virtualenv, installs requirements, writes a systemd service, and optionally creates an Nginx config.

Recommended production practices:

- Use HTTPS.
- Set a strong `SECRET_KEY`.
- Set predictable first-run passwords only through secure environment variables.
- Rotate first-run credentials immediately.
- Keep `tasks.db`, `.secret_key`, `.env`, logs, and backups out of Git.
- Configure a backup strategy.
- Restrict access to deploy endpoints.
- Use a dedicated HR Telegram group.
- Use a dedicated Sales Telegram group.
- Use separate accountant/finance notifications where appropriate.

## Data Safety

The open-source repo intentionally excludes:

- Production database
- Logs
- Secrets
- Backups
- Handoff notes
- Deployment credentials

The `.gitignore` blocks common runtime files, including:

- `tasks.db`
- `.secret_key`
- `.env`
- logs
- SQLite WAL/SHM files
- backups
- Python caches

Before publishing or sharing a fork, run:

```bash
find . -maxdepth 3 \( -name "*.db" -o -name ".secret_key" -o -name "*.log" -o -name "*.bak" \) -print
```

The command should print nothing for a clean source release.

## Recommended Setup For A New Agency

1. Install and start the app.
2. Log in as superadmin and rotate credentials.
3. Set app name, company branding, timezone, and public base URL.
4. Create roles and adjust permissions.
5. Add employees and assign shifts.
6. Configure weekly off days and set monthly holidays before reports run.
7. Add clients and projects.
8. Add recurring tasks for routine client work.
9. Use one-time tasks for ad hoc requests.
10. Use backlog for work that needs triage.
11. Configure Telegram bot token.
12. Connect general group only for safe team-wide events.
13. Connect HR group with `/hrgroup`.
14. Connect Sales group with `/salesgroup`.
15. Configure lead intake sources for website, Telegram, and WhatsApp.
16. Configure accountant chat ID for invoice reminders.
17. Add company profiles and currencies.
18. Test invoices and PDFs.
19. Configure SMTP if reports must be emailed.
20. Schedule reports and daily HR summaries.
21. Download a backup and verify restore process.

## Maintenance Notes For Contributors

When adding a feature, update this guide if the change affects:

- User workflows
- Roles or permissions
- Database-backed modules
- Telegram commands
- Public lead intake behavior
- API payloads or endpoints
- Reports, reminders, or scheduler behavior
- Deployment or security practices

The app is intentionally practical and compact. Prefer changes that improve day-to-day agency operations without turning the system into a heavy enterprise platform.
