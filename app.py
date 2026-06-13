import sqlite3
import os
import hashlib
import secrets
import threading
import time as _time
import json
import io
import base64
import calendar
import bcrypt
from datetime import datetime, date, timedelta
from functools import wraps
from statistics import median
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, send_file, make_response, jsonify

APP_VERSION = "4.7.21"

app = Flask(__name__)

# Structured file logging — written to ./app.log
import logging
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
_log_fmt = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_log_fmt,
    handlers=[
        logging.FileHandler(_log_path),
        logging.StreamHandler(),  # also to stdout for journalctl
    ]
)
logger = logging.getLogger("task-manager")
logger.info("Task Manager starting v%s", APP_VERSION)

# SECRET_KEY: prefer env; persist to a file so sessions survive restarts
_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    app.secret_key = _env_secret
elif os.path.exists(_SECRET_FILE):
    with open(_SECRET_FILE, "r") as _f:
        app.secret_key = _f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    try:
        with open(_SECRET_FILE, "w") as _f:
            _f.write(app.secret_key)
        os.chmod(_SECRET_FILE, 0o600)
    except Exception:
        pass  # fall back to ephemeral secret — sessions won't survive restart

# Session / cookie security
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "auto").lower() in ("1", "true", "yes", "auto") and False  # auto-enabled per-request below
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 12  # 12 hours

# Global upload cap — protects every POST from oversize bodies
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

# Trust Proxy headers from nginx / Caddy so request.is_secure works behind HTTPS
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = 30000


def open_db(row_factory=True):
    """Open SQLite with production-safe pragmas for concurrent web + scheduler use."""
    db = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    if row_factory:
        db.row_factory = sqlite3.Row
    for pragma in (
        "PRAGMA foreign_keys=ON",
        f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
    ):
        try:
            db.execute(pragma)
        except sqlite3.DatabaseError:
            pass
    return db

# Custom Jinja2 filter: parse JSON strings in templates
import json as _json_module
@app.template_filter("from_json")
def from_json_filter(value):
    try:
        return _json_module.loads(value or "[]")
    except Exception:
        return []


# ══════════════════════════════════════════════════════════
# ══  Access Control List (ACL)  ══════════════════════════
# ══════════════════════════════════════════════════════════

# Permission catalogue — (id, label, group)
PERMISSIONS = [
    # ── Personal ────────────────────────────────────────
    ("view_kanban",            "View Kanban Board",             "personal"),
    ("view_calendar",          "View Calendar",                 "personal"),
    ("view_own_kpi",           "View Own Performance",          "personal"),
    ("manage_own_tasks",       "Manage Own Tasks",              "personal"),
    ("manage_own_backlog",     "Manage Own Backlog",            "personal"),

    # ── Team ────────────────────────────────────────────
    ("view_team_list",         "View Team Member List",         "team"),
    ("view_team_tasks",        "View Other Members\' Tasks",     "team"),
    ("create_team_member",     "Add New Team Members",          "team"),
    ("edit_team_member",       "Edit Team Members",             "team"),
    ("delete_team_member",     "Delete Team Members",           "team"),
    ("reset_member_password",  "Reset Member Passwords",        "team"),
    ("import_team",            "Import Team from Excel",        "team"),
    ("assign_tasks_to_others", "Assign Tasks to Other Members", "team"),

    # ── Attendance ──────────────────────────────────────
    ("view_attendance",        "View Team Attendance",          "attendance"),
    ("edit_attendance",        "Edit Attendance Records",       "attendance"),
    ("delete_attendance",      "Delete Attendance Records",     "attendance"),
    ("export_attendance",      "Export Attendance Data",        "attendance"),
    ("manage_shifts",          "Manage Shift Timings",          "attendance"),
    ("manage_holidays",        "Manage Holidays & Weekly Off",  "attendance"),
    ("apply_leave",            "Apply for Own Leave",           "attendance"),
    ("view_team_leave",        "View Team Leave Requests",      "attendance"),
    ("manage_leave",           "Approve / Reject Leave",        "attendance"),

    # ── Performance & Audit ─────────────────────────────
    ("view_team_kpi",          "View Team Performance / KPIs",  "insights"),
    ("export_kpi",             "Export Performance Data",       "insights"),
    ("view_audit_log",         "View Activity / Audit Log",     "insights"),
    ("export_audit_log",       "Export Audit Log",              "insights"),

    # ── Clients ─────────────────────────────────────────
    ("view_clients",           "View Client List",              "clients"),
    ("create_client",          "Add New Clients",               "clients"),
    ("edit_client",            "Edit Client Details",           "clients"),
    ("delete_client",          "Delete Clients",                "clients"),
    ("import_clients",         "Import Clients from Excel",     "clients"),

    # ── Leads ───────────────────────────────────────────
    ("view_leads",             "View Lead Pipeline",            "leads"),
    ("create_lead",            "Add New Leads",                 "leads"),
    ("edit_lead",              "Edit Lead Details",             "leads"),
    ("delete_lead",            "Delete Leads",                  "leads"),
    ("manage_lead_followups",  "Manage Lead Follow-ups",        "leads"),
    ("convert_lead",           "Convert Leads to Clients",      "leads"),

    # ── Projects ────────────────────────────────────────
    ("view_projects",          "View Project List",             "projects"),
    ("create_project",         "Add New Projects",              "projects"),
    ("edit_project",           "Edit Projects",                 "projects"),
    ("delete_project",         "Delete Projects",               "projects"),
    ("manage_project_links",   "Manage Project Links & Files",  "projects"),
    ("import_projects",        "Import Projects from Excel",    "projects"),

    # ── Invoices ────────────────────────────────────────
    ("view_invoices",          "View Invoice List",             "invoices"),
    ("create_invoice",         "Create New Invoices",           "invoices"),
    ("edit_invoice",           "Edit Invoices",                 "invoices"),
    ("delete_invoice",         "Delete Invoices",               "invoices"),
    ("send_invoice",           "Send Invoices to Clients",      "invoices"),
    ("mark_invoice_paid",      "Mark Invoices as Paid",         "invoices"),
    ("generate_invoice_pdf",   "Generate Invoice PDFs",         "invoices"),
    ("import_invoices",        "Import Invoices from Excel",    "invoices"),
    ("manage_recurring_billing","Manage Recurring Billing",     "invoices"),

    # ── Reports ─────────────────────────────────────────
    ("view_reports",           "View Reports",                  "reports"),
    ("generate_reports",       "Generate Custom Reports",       "reports"),
    ("schedule_reports",       "Schedule Auto-Reports",         "reports"),
    ("export_reports",         "Export / Download Reports",     "reports"),
    ("email_reports",          "Email Reports to Stakeholders", "reports"),

    # ── Reviews ─────────────────────────────────────────
    ("view_reviews",           "View Project Reviews",          "reviews"),
    ("manage_reviews",         "Schedule & Manage Reviews",     "reviews"),

    # ── System ──────────────────────────────────────────
    ("manage_branding",        "Manage Branding & Logo",        "system"),
    ("manage_email_settings",  "Manage Email / SMTP Settings",  "system"),
    ("manage_telegram",        "Manage Telegram Integration",   "system"),
    ("manage_currencies",      "Manage Currencies",             "system"),
    ("manage_company_profiles","Manage Company Profiles",       "system"),
    ("manage_app_settings",    "Manage Other System Settings",  "system"),
    ("manage_permissions",     "Manage Role Permissions",       "system"),
]

# Default permission matrix by role
_ALL_PERMS = {p[0] for p in PERMISSIONS}
_LEAD_PERMS = {
    "view_leads", "create_lead", "edit_lead", "delete_lead",
    "manage_lead_followups", "convert_lead",
}

_MEMBER_PERMS = {
    "view_kanban", "view_calendar", "view_own_kpi",
    "manage_own_tasks", "manage_own_backlog", "apply_leave",
}

_TEAM_LEAD_PERMS = _MEMBER_PERMS | {
    "view_team_list", "view_team_tasks", "view_attendance",
    "view_clients", "view_projects", "view_reports",
    "view_reviews", "assign_tasks_to_others",
}

_MANAGER_PERMS = _TEAM_LEAD_PERMS | {
    "edit_team_member", "reset_member_password",
    "edit_attendance", "export_attendance",
    "view_team_kpi", "export_kpi", "view_audit_log",
    "view_invoices", "generate_reports", "export_reports",
    "manage_reviews", "manage_holidays", "view_team_leave", "manage_leave",
}

_ADMIN_PERMS = _MANAGER_PERMS | {
    "create_team_member", "delete_team_member", "import_team",
    "delete_attendance", "manage_shifts", "export_audit_log",
    "create_client", "edit_client", "delete_client", "import_clients",
    "create_project", "edit_project", "delete_project",
    "manage_project_links", "import_projects",
    "create_invoice", "edit_invoice", "delete_invoice",
    "send_invoice", "mark_invoice_paid", "generate_invoice_pdf",
    "import_invoices", "manage_recurring_billing",
    "schedule_reports", "email_reports",
}

_SALES_PERMS = _MEMBER_PERMS | {"view_clients"}
_ACCOUNTANT_PERMS = _MEMBER_PERMS | {
    "view_team_list", "view_team_leave", "manage_leave",
    "view_invoices", "mark_invoice_paid", "generate_invoice_pdf",
}

DEFAULT_ROLE_PERMS = {
    "employee":   _MEMBER_PERMS,
    "sales":      _SALES_PERMS,
    "accountant": _ACCOUNTANT_PERMS,
    "subadmin":   _TEAM_LEAD_PERMS,
    "manager":    _MANAGER_PERMS,
    "admin":      _ADMIN_PERMS,
    "superadmin": _ALL_PERMS,  # Owner gets everything
}

ROLE_ORDER = ["employee", "sales", "accountant", "subadmin", "manager", "admin", "superadmin"]
ROLE_LABELS = {
    "employee": "Member",
    "sales": "Sales",
    "accountant": "Accountant",
    "subadmin": "Team Lead",
    "manager": "Manager",
    "admin": "Admin",
    "superadmin": "Owner",
}

LEAD_STAGES = [
    ("enquiry", "Enquiry"),
    ("discussion", "Discussion"),
    ("confirmation", "Confirmation"),
    ("on_hold", "On Hold"),
    ("converted", "Converted"),
]
LEAD_STAGE_VALUES = {stage for stage, _ in LEAD_STAGES}
LEAD_STAGE_LABELS = dict(LEAD_STAGES)
LEAD_STAGE_BADGES = {
    "enquiry": "badge-pending",
    "discussion": "badge-recurring",
    "confirmation": "badge-success",
    "on_hold": "badge-on-hold",
    "converted": "badge-done",
}

LEAVE_STATUSES = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
    ("cancelled", "Cancelled"),
]
LEAVE_STATUS_VALUES = {status for status, _ in LEAVE_STATUSES}
LEAVE_STATUS_LABELS = dict(LEAVE_STATUSES)
LEAVE_STATUS_BADGES = {
    "pending": "badge-pending",
    "approved": "badge-done",
    "rejected": "badge-deleted",
    "cancelled": "badge-on-hold",
}
LEAVE_DAY_PARTS = {
    "full": "Full day",
    "first_half": "First half",
    "second_half": "Second half",
}


def has_perm(role, permission):
    """Check if a role has a permission. Falls back to defaults if not in DB."""
    if role == "superadmin":
        return True  # Owner always has everything
    try:
        db = get_db()
        row = db.execute(
            "SELECT allowed FROM role_permissions WHERE role=? AND permission=?",
            (role, permission)
        ).fetchone()
        if row is not None:
            return bool(row["allowed"])
    except Exception:
        pass
    return permission in DEFAULT_ROLE_PERMS.get(role, set())


def current_has_perm(permission):
    """Shortcut for session-based permission check."""
    return has_perm(session.get("role", ""), permission)


def require_perm(permission):
    """Decorator that blocks access if the current user lacks the permission."""
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if not has_perm(session.get("role", ""), permission):
                flash("You don\'t have permission to access that.", "error")
                return redirect(url_for("my_dashboard"))
            return f(*args, **kwargs)
        return decorated
    return wrapper


@app.context_processor
def inject_perms():
    """Make has_perm available in all templates."""
    role = session.get("role", "")
    return {
        "has_perm": lambda perm: has_perm(role, perm),
        "current_role": role,
    }


# Wire permissions to key routes: the decorator is additive — existing
# role-based decorators still apply. require_perm is used where the old
# decorators were too coarse or where per-action control is needed.


def seed_permissions(db):
    """Ensure every (role, permission) combo exists in the table.
    Existing rows are preserved — we only INSERT rows that are missing.
    This lets us expand the permission catalog without wiping user edits."""
    existing = set()
    # Use positional indexing — init_db's db connection doesn't set row_factory
    for row in db.execute("SELECT role, permission FROM role_permissions").fetchall():
        try:
            existing.add((row["role"], row["permission"]))
        except (TypeError, IndexError):
            existing.add((row[0], row[1]))
    rows_to_add = []
    for role, perms in DEFAULT_ROLE_PERMS.items():
        for perm_id, _, _ in PERMISSIONS:
            if (role, perm_id) not in existing:
                rows_to_add.append((role, perm_id, 1 if perm_id in perms else 0))
    if rows_to_add:
        db.executemany(
            "INSERT OR IGNORE INTO role_permissions (role, permission, allowed) VALUES (?,?,?)",
            rows_to_add
        )
        db.commit()

# ── Telegram Config ─────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")
TELEGRAM_ACCOUNTANT_CHAT_ID = os.environ.get("TELEGRAM_ACCOUNTANT_CHAT_ID", "")
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN)

# ── Timezone Helpers ────────────────────────────────────

def get_app_setting(key, default=""):
    try:
        db = open_db()
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        db.close()
        return row[0] if row else default
    except Exception:
        return default

def set_app_setting(key, value):
    db = open_db()
    db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value))
    db.commit()
    db.close()

def get_currency_symbol(code):
    try:
        db = open_db()
        row = db.execute("SELECT symbol FROM currencies WHERE code=?", (code,)).fetchone()
        db.close()
        return row[0] if row else code
    except Exception:
        return code

def get_currencies_list():
    try:
        db = open_db()
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM currencies ORDER BY is_default DESC, code").fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception:
        return [{"code": "INR", "name": "Indian Rupee", "symbol": "\u20b9", "is_default": 1}]



def fetch_exchange_rates():
    """Fetch live exchange rates from frankfurter.app (ECB data, free, no key needed)."""
    import urllib.request as ureq
    try:
        db = open_db()
        db.row_factory = sqlite3.Row
        default = db.execute("SELECT code FROM currencies WHERE is_default=1").fetchone()
        if not default:
            db.close()
            return False, "No default currency set"
        base_code = default["code"]
        all_codes = [r["code"] for r in db.execute("SELECT code FROM currencies").fetchall()]
        target_codes = [c for c in all_codes if c != base_code]
        if not target_codes:
            db.close()
            return True, "Only one currency, no rates needed"

        url = f"https://api.frankfurter.app/latest?from={base_code}&to={','.join(target_codes)}"
        req = ureq.Request(url, headers={"User-Agent": "TaskManager/4.1"})
        resp = ureq.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        rates = data.get("rates", {})

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute("UPDATE currencies SET exchange_rate=1.0, rates_updated_at=? WHERE code=?", (now_str, base_code))
        updated = 0
        for code, rate in rates.items():
            db.execute("UPDATE currencies SET exchange_rate=?, rates_updated_at=? WHERE code=?",
                      (round(rate, 6), now_str, code))
            updated += 1
        db.commit()
        db.close()
        return True, f"Updated {updated} rates (base: {base_code}, source: ECB)"
    except Exception as e:
        try:
            db.close()
        except Exception:
            pass
        return False, str(e)


def get_telegram_bot_token():
    val = get_app_setting("telegram_bot_token", "")
    return val if val else TELEGRAM_BOT_TOKEN

def get_telegram_group_chat_id():
    val = get_app_setting("telegram_group_chat_id", "")
    return val if val else TELEGRAM_GROUP_CHAT_ID

def is_telegram_enabled():
    return bool(get_telegram_bot_token())

def get_tz():
    tz_name = get_app_setting("timezone", "Asia/Kolkata")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def get_tz_now():
    return datetime.now(get_tz())


def get_db():
    if "db" not in g:
        g.db = open_db()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        # Fallback: check SHA-256 for legacy passwords
        return hashlib.sha256(password.encode()).hexdigest() == hashed


def init_db():
    db = open_db()
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'employee',
            telegram_chat_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS recurring_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            active INTEGER DEFAULT 1,
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS recurring_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            completion_date TEXT NOT NULL,
            time_minutes REAL NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (task_id) REFERENCES recurring_tasks(id)
        );

        CREATE TABLE IF NOT EXISTS oneoff_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            completed INTEGER DEFAULT 0,
            completion_date TEXT,
            time_minutes REAL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS login_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            login_date TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            task_type TEXT NOT NULL,
            task_title TEXT NOT NULL,
            task_description TEXT DEFAULT '',
            completion_date TEXT,
            time_minutes REAL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            action_date TEXT NOT NULL,
            action_time TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS attendance_checkout_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attendance_id INTEGER NOT NULL UNIQUE,
            employee_id INTEGER NOT NULL,
            action_date TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (attendance_id) REFERENCES attendance(id) ON DELETE CASCADE,
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS attendance_checkin_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            action_date TEXT NOT NULL,
            shift_id INTEGER DEFAULT 0,
            shift_start_time TEXT DEFAULT '',
            sent_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(employee_id, action_date),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS attendance_midnight_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            previous_action_date TEXT NOT NULL,
            new_action_date TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(employee_id, previous_action_date, new_action_date),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS leave_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            paid INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            leave_type_id INTEGER DEFAULT 0,
            leave_type_name TEXT DEFAULT '',
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            start_day_part TEXT DEFAULT 'full',
            end_day_part TEXT DEFAULT 'full',
            total_days REAL DEFAULT 0,
            reason TEXT NOT NULL,
            handover_notes TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            submitted_at TEXT DEFAULT (datetime('now','localtime')),
            created_by INTEGER DEFAULT 0,
            reviewed_by INTEGER DEFAULT 0,
            reviewed_at TEXT DEFAULT '',
            review_note TEXT DEFAULT '',
            cancelled_at TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id),
            FOREIGN KEY (leave_type_id) REFERENCES leave_types(id)
        );

        CREATE TABLE IF NOT EXISTS leave_request_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            leave_request_id INTEGER NOT NULL,
            actor_id INTEGER DEFAULT 0,
            from_status TEXT DEFAULT '',
            to_status TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (leave_request_id) REFERENCES leave_requests(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_person TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            contact_person TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            source TEXT DEFAULT '',
            stage TEXT DEFAULT 'enquiry',
            estimated_value REAL DEFAULT 0,
            requirement TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            assigned_to INTEGER DEFAULT 0,
            next_followup_at TEXT DEFAULT '',
            created_by INTEGER DEFAULT 0,
            converted_client_id INTEGER DEFAULT 0,
            converted_at TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS lead_followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            note TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            telegram_sent_at TEXT DEFAULT '',
            completed_at TEXT DEFAULT '',
            created_by INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_stage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            from_stage TEXT DEFAULT '',
            to_stage TEXT NOT NULL,
            note TEXT DEFAULT '',
            changed_by INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_intake_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'website',
            token TEXT NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 1,
            default_stage TEXT DEFAULT 'enquiry',
            assigned_to INTEGER DEFAULT 0,
            allowed_origins TEXT DEFAULT '*',
            success_message TEXT DEFAULT 'Thanks, we received your enquiry.',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS lead_intake_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER DEFAULT 0,
            provider TEXT NOT NULL,
            external_id TEXT DEFAULT '',
            lead_id INTEGER DEFAULT 0,
            status TEXT DEFAULT 'created',
            payload_json TEXT DEFAULT '',
            remote_addr TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            services TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            invoice_number TEXT NOT NULL,
            invoice_date TEXT NOT NULL,
            due_date TEXT,
            subtotal REAL DEFAULT 0,
            discount_percent REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            remarks TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            billing_frequency INTEGER DEFAULT 0,
            auto_generate INTEGER DEFAULT 0,
            next_generate_date TEXT,
            template_invoice_id INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            total REAL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS company_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            logo_url TEXT DEFAULT '',
            address TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS currencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            is_default INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            employee_id INTEGER NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            read INTEGER DEFAULT 0,
            link TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );

        CREATE TABLE IF NOT EXISTS review_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'monthly',
            participants TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_config_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            scheduled_date TEXT NOT NULL,
            scheduled_time TEXT DEFAULT '10:00',
            status TEXT DEFAULT 'pending',
            mom TEXT DEFAULT '',
            completed_at TEXT,
            completed_by INTEGER,
            last_reminder_date TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (review_config_id) REFERENCES review_configs(id),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS report_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'weekly',
            frequency_day INTEGER DEFAULT 1,
            recipients TEXT DEFAULT '',
            cc TEXT DEFAULT '',
            subject_template TEXT DEFAULT '',
            include_branding INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1,
            last_sent_date TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            recipients TEXT DEFAULT '',
            cc TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            sent_by INTEGER,
            sent_via TEXT DEFAULT 'manual',
            success INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS daily_staff_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL UNIQUE,
            chat_id TEXT DEFAULT '',
            status TEXT DEFAULT 'sent',
            error TEXT DEFAULT '',
            sent_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)

    # Safe column additions for existing tables
    for table, col, coltype in [
        ("employees", "email", "TEXT DEFAULT ''"),
        ("employees", "phone", "TEXT DEFAULT ''"),
        ("employees", "department", "TEXT DEFAULT ''"),
        ("invoices", "currency", "TEXT DEFAULT 'INR'"),
        ("invoices", "paid_date", "TEXT"),
        ("invoices", "last_reminder_date", "TEXT"),
        ("recurring_tasks", "frequency", "TEXT DEFAULT 'daily'"),
        ("recurring_tasks", "frequency_day", "INTEGER DEFAULT 0"),
        ("activity_log", "actor_id", "INTEGER"),
        ("activity_log", "actor_role", "TEXT DEFAULT ''"),
        ("recurring_tasks", "frequency_month", "INTEGER DEFAULT 0"),
        ("invoices", "company_profile_id", "INTEGER DEFAULT 0"),
        ("employees", "birthday", "TEXT"),
        ("employees", "joining_date", "TEXT"),
        ("employees", "wedding_anniversary", "TEXT"),
        ("employees", "must_change_password", "INTEGER DEFAULT 0"),
        ("recurring_tasks", "drive_link", "TEXT DEFAULT ''"),
        ("oneoff_tasks", "drive_link", "TEXT DEFAULT ''"),
        ("invoices", "drive_link", "TEXT DEFAULT ''"),
        ("invoices", "share_token", "TEXT DEFAULT ''"),
        ("employees", "profile_image", "TEXT DEFAULT ''"),
        ("currencies", "exchange_rate", "REAL DEFAULT 1.0"),
        ("currencies", "rates_updated_at", "TEXT DEFAULT ''"),
        ("recurring_tasks", "project_id", "INTEGER DEFAULT 0"),
        ("recurring_tasks", "billable", "INTEGER DEFAULT 1"),
        ("oneoff_tasks", "project_id", "INTEGER DEFAULT 0"),
        ("oneoff_tasks", "billable", "INTEGER DEFAULT 1"),
        ("leave_requests", "created_by", "INTEGER DEFAULT 0"),
        ("employees", "hourly_rate", "REAL DEFAULT 0"),
        ("employees", "hourly_rate_currency", "TEXT DEFAULT ''"),
        ("recurring_tasks", "scheduled_time", "TEXT DEFAULT ''"),
        ("projects", "project_links", "TEXT DEFAULT ''"),
        ("recurring_tasks", "frequency_days", "TEXT DEFAULT ''"),
        ("invoices", "billing_frequency_type", "TEXT DEFAULT 'monthly'"),
        ("invoices", "billing_days", "TEXT DEFAULT ''"),
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Create backlog_tasks table
    db.execute("""
        CREATE TABLE IF NOT EXISTS backlog_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            drive_link TEXT DEFAULT '',
            project_id INTEGER DEFAULT 0,
            priority TEXT DEFAULT 'medium',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
    """)

    # Create shifts table
    db.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL DEFAULT '09:00',
            end_time TEXT NOT NULL DEFAULT '18:00',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)

    # Create holidays table
    db.execute("""
        CREATE TABLE IF NOT EXISTS holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holiday_date TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)

    # Create login_attempts table for DB-backed rate limiting (shared across workers)
    db.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            attempted_at REAL NOT NULL,
            username TEXT DEFAULT ''
        );
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip, attempted_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_leads_assigned_to ON leads(assigned_to);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lead_followups_due ON lead_followups(status, due_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lead_intake_sources_token ON lead_intake_sources(token);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lead_intake_sources_type ON lead_intake_sources(source_type, enabled);")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lead_intake_events_external ON lead_intake_events(provider, external_id) WHERE external_id != '';")
    db.execute("CREATE INDEX IF NOT EXISTS idx_leave_requests_employee_dates ON leave_requests(employee_id, start_date, end_date);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_leave_requests_status_dates ON leave_requests(status, start_date, end_date);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_leave_history_request ON leave_request_history(leave_request_id, created_at);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_daily_staff_reports_date ON daily_staff_reports(report_date);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_midnight_reminders_dates ON attendance_midnight_reminders(previous_action_date, new_action_date);")

    for source_name, source_type, message in [
        ("Website Forms", "website", "Thanks, we received your enquiry."),
        ("Telegram Leads", "telegram", "Lead saved in Task Manager."),
        ("WhatsApp Leads", "whatsapp", "Lead saved in Task Manager."),
    ]:
        existing = db.execute("SELECT id FROM lead_intake_sources WHERE source_type=? LIMIT 1", (source_type,)).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO lead_intake_sources (name, source_type, token, success_message)
                VALUES (?,?,?,?)
            """, (source_name, source_type, secrets.token_urlsafe(24), message))

    for name, code, paid in [
        ("Casual Leave", "casual", 1),
        ("Sick Leave", "sick", 1),
        ("Earned Leave", "earned", 1),
        ("Unpaid Leave", "unpaid", 0),
        ("Work From Home", "wfh", 1),
    ]:
        db.execute("""
            INSERT OR IGNORE INTO leave_types (name, code, paid, active)
            VALUES (?,?,?,1)
        """, (name, code, paid))

    # Create role_permissions table (sparse — only stored when explicitly set)
    db.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            role TEXT NOT NULL,
            permission TEXT NOT NULL,
            allowed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (role, permission)
        );
    """)

    # Current policy: leads are owner-only. Do this once so old explicit
    # role_permissions rows from the earlier sales rollout do not keep access.
    lead_lock = db.execute(
        "SELECT value FROM app_settings WHERE key='lead_superadmin_only_migrated_v475'"
    ).fetchone()
    if not lead_lock:
        for role in ROLE_ORDER:
            if role == "superadmin":
                continue
            for perm_id in _LEAD_PERMS:
                db.execute("""
                    INSERT OR REPLACE INTO role_permissions (role, permission, allowed)
                    VALUES (?,?,0)
                """, (role, perm_id))
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('lead_superadmin_only_migrated_v475', '1')"
        )

    # Add shift_id to employees
    for table, col, coltype in [
        ("employees", "shift_id", "INTEGER DEFAULT 0"),
    ]:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass

    # Seed default shift if empty
    shift_count = db.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
    if shift_count == 0:
        db.executemany("INSERT INTO shifts (name, start_time, end_time) VALUES (?,?,?)", [
            ("General", "09:00", "18:00"),
            ("Morning", "06:00", "14:00"),
            ("Evening", "14:00", "22:00"),
        ])

    # Migrate projects table to allow client_id=0 (remove NOT NULL + FK constraints)
    try:
        cols = db.execute("PRAGMA table_info(projects)").fetchall()
        client_id_col = next((c for c in cols if c[1] == "client_id"), None)
        if client_id_col and client_id_col[3] == 1:  # notnull == 1
            db.execute("PRAGMA foreign_keys=OFF")
            # Preserve project_links column if it exists
            col_names = [c[1] for c in cols]
            has_links = "project_links" in col_names
            db.execute("""
                CREATE TABLE projects_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER DEFAULT 0,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    services TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    project_links TEXT DEFAULT ''
                )
            """)
            if has_links:
                db.execute("""INSERT INTO projects_new (id, client_id, name, description, services, status, created_at, project_links)
                              SELECT id, client_id, name, description, services, status, created_at, COALESCE(project_links,'') FROM projects""")
            else:
                db.execute("""INSERT INTO projects_new (id, client_id, name, description, services, status, created_at)
                              SELECT id, client_id, name, description, services, status, created_at FROM projects""")
            db.execute("DROP TABLE projects")
            db.execute("ALTER TABLE projects_new RENAME TO projects")
            db.execute("PRAGMA foreign_keys=ON")
    except Exception as _e:
        try:
            db.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass

    # Seed default currencies if empty
    cur_count = db.execute("SELECT COUNT(*) FROM currencies").fetchone()[0]
    if cur_count == 0:
        db.executemany("INSERT INTO currencies (code, name, symbol, is_default) VALUES (?,?,?,?)", [
            ("INR", "Indian Rupee", "\u20b9", 1),
            ("USD", "US Dollar", "$", 0),
            ("EUR", "Euro", "\u20ac", 0),
            ("GBP", "British Pound", "\u00a3", 0),
            ("AED", "UAE Dirham", "AED", 0),
            ("SGD", "Singapore Dollar", "S$", 0),
            ("AUD", "Australian Dollar", "A$", 0),
            ("CAD", "Canadian Dollar", "C$", 0),
            ("JPY", "Japanese Yen", "\u00a5", 0),
        ])

    # Create default admin if none exists.
    # Force must_change_password=1 so the first login rotates the seed password.
    existing = db.execute("SELECT id FROM employees WHERE role='admin'").fetchone()
    if not existing:
        # Allow override via env for first-time secure setup
        default_admin_pw = os.environ.get("INITIAL_ADMIN_PASSWORD", "").strip() or secrets.token_urlsafe(12)
        db.execute(
            "INSERT INTO employees (name, username, password, role, must_change_password) VALUES (?,?,?,?,?)",
            ("Admin", "admin", hash_password(default_admin_pw), "admin", 1),
        )
        print(f"[setup] Created admin account. Initial password: {default_admin_pw}  (MUST be changed on first login)")
    existing_sa = db.execute("SELECT id FROM employees WHERE role='superadmin'").fetchone()
    if not existing_sa:
        default_sa_pw = os.environ.get("INITIAL_OWNER_PASSWORD", "").strip() or secrets.token_urlsafe(12)
        db.execute(
            "INSERT INTO employees (name, username, password, role, must_change_password) VALUES (?,?,?,?,?)",
            ("Super Admin", "superadmin", hash_password(default_sa_pw), "superadmin", 1),
        )
        print(f"[setup] Created superadmin account. Initial password: {default_sa_pw}  (MUST be changed on first login)")

    # Seed default role permissions if table is empty
    seed_permissions(db)
    leave_acl = db.execute(
        "SELECT value FROM app_settings WHERE key='leave_acl_accountant_migrated_v4720'"
    ).fetchone()
    if not leave_acl:
        for role in ("admin", "accountant"):
            for perm_id in ("apply_leave", "view_team_leave", "manage_leave"):
                db.execute("""
                    INSERT OR REPLACE INTO role_permissions (role, permission, allowed)
                    VALUES (?,?,1)
                """, (role, perm_id))
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('leave_acl_accountant_migrated_v4720', '1')"
        )

    # Generate API deploy key if not set
    existing_key = db.execute("SELECT value FROM app_settings WHERE key='deploy_key'").fetchone()
    if not existing_key:
        db.execute("INSERT INTO app_settings (key, value) VALUES (?,?)",
                   ("deploy_key", secrets.token_hex(32)))

    # Set default settings
    defaults = {
        "timezone": "Asia/Kolkata",
        "company_name": "",
        "company_logo_url": "",
        "company_address": "",
        "company_email": "",
        "company_phone": "",
        "accountant_chat_id": "",
        "accountant_name": "Accountant",
        "sales_telegram_chat_id": "",
        "lead_reminder_chat_id": "",
        "general_telegram_notifications_enabled": "0",
        "telegram_general_attendance_enabled": "1",
        "telegram_general_celebrations_enabled": "1",
        "monthly_attendance_enabled": "0",
        "monthly_attendance_chat_id": "",
        "daily_staff_report_enabled": "0",
        "daily_staff_report_chat_id": "",
        "daily_staff_report_time": "09:30",
        "usd_to_inr_rate": "83.50",
        "default_currency": "INR",
        "app_name": "Task Manager",
        "public_base_url": "http://localhost:5050",
        "primary_color": "#2563eb",
        "app_logo_url": "",
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from": "",
        "smtp_enabled": "0",
        "dark_mode": "auto",
    }
    for key, val in defaults.items():
        existing = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if not existing:
            db.execute("INSERT INTO app_settings (key, value) VALUES (?,?)", (key, val))

    db.commit()
    db.close()


def create_notification(db, employee_id, message, notif_type="info", link=""):
    """Create an in-app notification for a user."""
    try:
        db.execute("INSERT INTO notifications (employee_id, message, type, link) VALUES (?,?,?,?)",
                  (employee_id, message, notif_type, link))
    except Exception:
        pass


def log_activity(db, employee_id, action, task_type, task_title, task_description="", completion_date=None, time_minutes=None, notes="", actor_id=None, actor_role=""):
    if actor_id is None:
        try:
            actor_id = session.get("user_id", employee_id)
            actor_role = session.get("role", "")
        except RuntimeError:
            actor_id = employee_id
            actor_role = ""
    db.execute(
        """INSERT INTO activity_log
           (employee_id, action, task_type, task_title, task_description, completion_date, time_minutes, notes, actor_id, actor_role)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (employee_id, action, task_type, task_title, task_description, completion_date, time_minutes, notes, actor_id, actor_role),
    )


def parse_iso_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def date_range_strings(start_day, end_day):
    current = start_day
    while current <= end_day:
        yield current.isoformat()
        current += timedelta(days=1)


def parse_year_month(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        return None


def month_end_for(month_start):
    return month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])


def holiday_display_row(row):
    holiday_day = parse_iso_date(row["holiday_date"])
    weekday = holiday_day.strftime("%A") if holiday_day else ""
    return {
        "id": row["id"],
        "holiday_date": row["holiday_date"],
        "name": row["name"],
        "created_at": row["created_at"] if "created_at" in row.keys() else "",
        "weekday": weekday,
    }


def weekly_off_day_indexes():
    weekly_off = get_app_setting("weekly_off_days", "6")
    try:
        return {int(x.strip()) for x in weekly_off.split(",") if x.strip()}
    except ValueError:
        return {6}


def is_non_working_date(db, day_str):
    day = parse_iso_date(day_str)
    if not day:
        return {"is_off": False, "reason": ""}
    holiday = db.execute("SELECT name FROM holidays WHERE holiday_date=?", (day_str,)).fetchone()
    if holiday:
        return {"is_off": True, "reason": holiday["name"]}
    if day.weekday() in weekly_off_day_indexes():
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return {"is_off": True, "reason": names[day.weekday()]}
    return {"is_off": False, "reason": ""}


def calculate_leave_days(db, start_date, end_date, start_part="full", end_part="full"):
    start_day = parse_iso_date(start_date)
    end_day = parse_iso_date(end_date)
    if not start_day or not end_day or start_day > end_day:
        return 0.0

    start_part = start_part if start_part in LEAVE_DAY_PARTS else "full"
    end_part = end_part if end_part in LEAVE_DAY_PARTS else "full"
    total = 0.0
    for day_str in date_range_strings(start_day, end_day):
        if is_non_working_date(db, day_str)["is_off"]:
            continue
        if start_day == end_day:
            total += 0.5 if start_part != "full" or end_part != "full" else 1.0
        elif day_str == start_day.isoformat():
            total += 0.5 if start_part != "full" else 1.0
        elif day_str == end_day.isoformat():
            total += 0.5 if end_part != "full" else 1.0
        else:
            total += 1.0
    return round(total, 1)


def leave_request_overlaps(db, employee_id, start_date, end_date, exclude_id=0):
    params = [employee_id, end_date, start_date]
    exclude_sql = ""
    if exclude_id:
        exclude_sql = "AND id != ?"
        params.append(exclude_id)
    return db.execute(f"""
        SELECT id, start_date, end_date, status
        FROM leave_requests
        WHERE employee_id=?
          AND status IN ('pending','approved')
          AND start_date <= ?
          AND end_date >= ?
          {exclude_sql}
        LIMIT 1
    """, params).fetchone()


def approved_leave_map(db, employee_ids, from_date, to_date):
    if not employee_ids:
        return {}
    placeholders = ",".join("?" for _ in employee_ids)
    rows = db.execute(f"""
        SELECT lr.*, COALESCE(lt.name, lr.leave_type_name, 'Leave') as type_name
        FROM leave_requests lr
        LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.status='approved'
          AND lr.employee_id IN ({placeholders})
          AND lr.start_date <= ?
          AND lr.end_date >= ?
        ORDER BY lr.start_date
    """, [*employee_ids, to_date, from_date]).fetchall()
    start_bound = parse_iso_date(from_date)
    end_bound = parse_iso_date(to_date)
    by_emp = {emp_id: {} for emp_id in employee_ids}
    if not start_bound or not end_bound:
        return by_emp
    for row in rows:
        leave_start = max(parse_iso_date(row["start_date"]) or start_bound, start_bound)
        leave_end = min(parse_iso_date(row["end_date"]) or end_bound, end_bound)
        for day_str in date_range_strings(leave_start, leave_end):
            if is_non_working_date(db, day_str)["is_off"]:
                continue
            by_emp.setdefault(row["employee_id"], {})[day_str] = row
    return by_emp


def employee_on_approved_leave(db, employee_id, day_str):
    row = db.execute("""
        SELECT lr.id
        FROM leave_requests lr
        WHERE lr.employee_id=?
          AND lr.status='approved'
          AND lr.start_date <= ?
          AND lr.end_date >= ?
        LIMIT 1
    """, (employee_id, day_str, day_str)).fetchone()
    if not row:
        return False
    return not is_non_working_date(db, day_str)["is_off"]


def leave_type_name(db, leave_type_id):
    row = db.execute("SELECT name FROM leave_types WHERE id=?", (leave_type_id,)).fetchone()
    return row["name"] if row else "Leave"


def add_leave_history(db, leave_id, actor_id, from_status, to_status, note=""):
    db.execute("""
        INSERT INTO leave_request_history (leave_request_id, actor_id, from_status, to_status, note)
        VALUES (?,?,?,?,?)
    """, (leave_id, actor_id or 0, from_status or "", to_status, note or ""))


def notify_leave_reviewers(db, leave_id, employee_name, leave_label, start_date, end_date):
    reviewers = db.execute("""
        SELECT id, role FROM employees
        WHERE id != ?
        ORDER BY role DESC, name
    """, (session.get("user_id", 0),)).fetchall()
    msg = f"{employee_name} requested {leave_label} leave from {start_date} to {end_date}."
    for reviewer in reviewers:
        if has_perm(reviewer["role"], "manage_leave"):
            create_notification(db, reviewer["id"], msg, "leave", f"/leaves?status=pending&focus={leave_id}")


def notify_leave_employee(db, leave, status_label, note=""):
    emp = db.execute("SELECT name, telegram_chat_id FROM employees WHERE id=?", (leave["employee_id"],)).fetchone()
    if not emp:
        return
    message = f"Your leave request for {leave['start_date']} to {leave['end_date']} was {status_label.lower()}."
    if note:
        message += f" Note: {note}"
    create_notification(db, leave["employee_id"], message, "leave", f"/leaves?focus={leave['id']}")
    if emp["telegram_chat_id"]:
        send_telegram(emp["telegram_chat_id"], f"<b>Leave {status_label}</b>\n\n{message}")


def get_accountant_chat_id():
    """Get accountant chat ID from settings, fallback to env var."""
    chat_id = get_app_setting("accountant_chat_id", "")
    if not chat_id:
        chat_id = TELEGRAM_ACCOUNTANT_CHAT_ID
    return chat_id


def get_first_superadmin_telegram_chat_id(db=None):
    close_db = False
    if db is None:
        db = open_db()
        close_db = True
    try:
        row = db.execute("""
            SELECT telegram_chat_id FROM employees
            WHERE role='superadmin' AND telegram_chat_id != ''
            ORDER BY id LIMIT 1
        """).fetchone()
        return row["telegram_chat_id"] if row and row["telegram_chat_id"] else ""
    except Exception:
        return ""
    finally:
        if close_db:
            db.close()


def get_sales_telegram_chat_id(db=None):
    """Return the dedicated sales Telegram group, never the general team group."""
    general_chat_id = get_telegram_group_chat_id()
    for key in ("sales_telegram_chat_id", "lead_reminder_chat_id"):
        if db is not None:
            try:
                row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                chat_id = (row["value"] if row else "").strip()
            except Exception:
                chat_id = ""
        else:
            chat_id = get_app_setting(key, "").strip()
        if chat_id and chat_id != general_chat_id:
            return chat_id
    return ""


def get_lead_reminder_chat_id(db=None):
    """Resolve private lead reminder destination.

    Lead reminders must never fall back to the general company Telegram group.
    Set lead_reminder_chat_id explicitly for a private owner chat or a separate
    sales group; otherwise we use the first superadmin Telegram chat ID.
    """
    chat_id = get_sales_telegram_chat_id(db)
    if chat_id:
        return chat_id
    return get_first_superadmin_telegram_chat_id(db)


def set_app_setting_on_db(db, key, value):
    db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value))


def parse_lead_followup_at(date_value, time_value=""):
    date_value = (date_value or "").strip()
    time_value = (time_value or "").strip() or "09:00"
    if not date_value:
        return ""
    try:
        datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return f"{date_value} {time_value}"


def refresh_lead_next_followup(db, lead_id):
    row = db.execute("""
        SELECT due_at FROM lead_followups
        WHERE lead_id=? AND status='pending'
        ORDER BY due_at ASC LIMIT 1
    """, (lead_id,)).fetchone()
    next_due = row["due_at"] if row else ""
    db.execute("UPDATE leads SET next_followup_at=?, updated_at=datetime('now','localtime') WHERE id=?",
               (next_due, lead_id))
    return next_due


def get_lead_intake_source(db, token=None, source_id=None, source_type=None, include_disabled=False):
    where = []
    params = []
    if token is not None:
        where.append("token=?")
        params.append(token)
    if source_id is not None:
        where.append("id=?")
        params.append(source_id)
    if source_type:
        where.append("source_type=?")
        params.append(source_type)
    if not include_disabled:
        where.append("enabled=1")
    if not where:
        return None
    return db.execute(f"SELECT * FROM lead_intake_sources WHERE {' AND '.join(where)} LIMIT 1", params).fetchone()


def telegram_lead_intake_enabled(db=None):
    close_db = False
    if db is None:
        db = open_db()
        db.row_factory = sqlite3.Row
        close_db = True
    try:
        row = db.execute("""
            SELECT id FROM lead_intake_sources
            WHERE source_type='telegram' AND enabled=1
            LIMIT 1
        """).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        if close_db:
            db.close()


def lead_intake_public_url(source):
    base = get_app_setting("public_base_url", "").strip().rstrip("/")
    if not base:
        base = request.url_root.rstrip("/") if request else ""
    source_type = source["source_type"]
    if source_type == "telegram":
        return f"{base}/api/v1/lead-intake/telegram/{source['token']}"
    if source_type == "whatsapp":
        return f"{base}/api/v1/lead-intake/whatsapp/{source['token']}"
    return f"{base}/api/v1/lead-intake/{source['token']}"


def lead_intake_source_dict(source):
    result = dict(source)
    result["endpoint_url"] = lead_intake_public_url(source)
    return result


def lead_intake_origin_allowed(source, origin):
    if not origin:
        return True
    allowed = (source["allowed_origins"] or "*").strip()
    if not allowed or allowed == "*":
        return True
    allowed_values = {item.strip().rstrip("/") for item in allowed.split(",") if item.strip()}
    return origin.rstrip("/") in allowed_values


def lead_intake_response(payload, source=None, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    origin = request.headers.get("Origin", "")
    if source and lead_intake_origin_allowed(source, origin):
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Requested-With"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


def lead_intake_request_payload():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = {}
        data.update(request.form.to_dict(flat=True))
    return {str(k): v for k, v in data.items()}


LEAD_INTAKE_FIELD_ALIASES = {
    "company_name": {"company", "company_name", "business", "business_name", "organization", "organisation", "org", "lead", "lead_name"},
    "contact_person": {"name", "full_name", "contact", "contact_name", "contact_person", "person", "customer", "customer_name"},
    "email": {"email", "mail", "email_address"},
    "phone": {"phone", "mobile", "whatsapp", "tel", "telephone", "contact_number"},
    "source": {"source", "lead_source", "channel"},
    "source_url": {"url", "page", "page_url", "source_url", "website_url", "referrer", "referer"},
    "requirement": {"requirement", "requirements", "message", "details", "service", "services", "query", "enquiry", "inquiry", "description", "need"},
    "notes": {"note", "notes", "remarks", "comments"},
    "estimated_value": {"value", "budget", "estimated_value", "deal_value"},
    "stage": {"stage", "status"},
    "assigned_to": {"assigned_to", "owner_id", "sales_id", "owner", "sales_owner", "sales", "assigned"},
    "followup_date": {"followup_date", "follow_up_date", "date", "reminder_date"},
    "followup_time": {"followup_time", "follow_up_time", "time", "reminder_time"},
    "followup_note": {"followup_note", "follow_up_note", "reminder", "reminder_note"},
    "due_at": {"due_at", "followup_due_at", "follow_up_due_at"},
}


def normalize_lead_intake_data(payload):
    normalized = {}
    reverse = {}
    for target, aliases in LEAD_INTAKE_FIELD_ALIASES.items():
        for alias in aliases:
            reverse[alias] = target
    for key, value in (payload or {}).items():
        clean_key = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        target = reverse.get(clean_key)
        if target and value not in (None, ""):
            normalized[target] = str(value).strip()
    for utm_key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        if utm_key in payload and payload.get(utm_key):
            normalized[utm_key] = str(payload.get(utm_key)).strip()
    return normalized


def parse_lead_field_lines(text):
    import re
    payload = {}
    for line in [line.strip() for line in (text or "").splitlines() if line.strip()]:
        match = re.match(r"^([A-Za-z][A-Za-z0-9 _/-]{1,32})\s*[:=-]\s*(.+)$", line)
        if match:
            payload[match.group(1)] = match.group(2)
    return normalize_lead_intake_data(payload) if payload else {}


def normalize_lead_stage(value, default="enquiry", allow_converted=False):
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "inquiry": "enquiry",
        "new": "enquiry",
        "discuss": "discussion",
        "talking": "discussion",
        "confirm": "confirmation",
        "confirmed": "confirmation",
        "hold": "on_hold",
        "onhold": "on_hold",
        "on_hold": "on_hold",
        "pause": "on_hold",
        "paused": "on_hold",
        "won": "converted",
    }
    stage = aliases.get(raw, raw)
    if stage == "converted" and not allow_converted:
        return default
    return stage if stage in LEAD_STAGE_VALUES else default


def parse_lead_text_message(text):
    import re
    text = (text or "").strip()
    text = re.sub(r"^/lead(@\w+)?\s*", "", text, flags=re.I).strip()
    text = re.sub(r"^lead\s*[:\-]?\s*", "", text, flags=re.I).strip()
    if not text:
        return {}

    payload = parse_lead_field_lines(text)
    if payload:
        return payload

    parts = [part.strip() for part in re.split(r"\s*\|\s*", text) if part.strip()]
    if len(parts) >= 3:
        return normalize_lead_intake_data({
            "company": parts[0],
            "name": parts[1] if len(parts) > 1 else "",
            "phone": parts[2] if len(parts) > 2 else "",
            "email": parts[3] if len(parts) > 3 else "",
            "message": " | ".join(parts[4:]) if len(parts) > 4 else "",
        })

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return normalize_lead_intake_data({
        "company": lines[0],
        "message": "\n".join(lines[1:]) if len(lines) > 1 else text,
    })


def should_create_text_lead(text):
    import re
    text = (text or "").strip()
    if not text:
        return False
    if re.match(r"^/lead(@\w+)?(\s|$)", text, flags=re.I):
        return True
    if re.match(r"^lead\s*[:\-]?\s+", text, flags=re.I):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in ("company:", "business:", "phone:", "requirement:", "enquiry:", "inquiry:"))


def lead_intake_help_text():
    return (
        "Send a lead like this:\n"
        "/lead\n"
        "Company: Acme Industries\n"
        "Name: Ravi\n"
        "Phone: +91...\n"
        "Email: ravi@example.com\n"
        "Requirement: Website redesign\n"
        "Followup date: 2026-05-05\n"
        "Followup time: 10:00"
    )


def telegram_sales_help_text():
    return (
        "<b>Sales Team lead commands</b>\n\n"
        "<b>Add lead</b>\n"
        "<code>/lead\n"
        "Company: Acme Industries\n"
        "Name: Ravi\n"
        "Phone: +91...\n"
        "Email: ravi@example.com\n"
        "Requirement: Website redesign\n"
        "Followup date: 2026-05-05\n"
        "Followup time: 10:00</code>\n\n"
        "<b>Edit lead</b>\n"
        "<code>/leadedit 12\n"
        "Stage: discussion\n"
        "Phone: +91...\n"
        "Requirement: Updated requirement</code>\n\n"
        "<b>Quick actions</b>\n"
        "<code>/leadstage 12 confirmation</code>\n"
        "<code>/followup 12 2026-05-05 10:00 Call decision maker</code>\n"
        "<code>/leadinfo 12</code>\n"
        "<code>/leads due</code>\n\n"
        "To connect the sales group, add this bot to <b>Sales Team</b> and send <code>/salesgroup</code> once."
    )


def lead_detail_url(lead_id):
    public_base = (get_app_setting("public_base_url", "http://localhost:5050") or "").rstrip("/")
    return f"{public_base}/leads/{lead_id}" if public_base else f"/leads/{lead_id}"


def telegram_open_lead_markup(lead_id):
    url = lead_detail_url(lead_id)
    if not url.startswith("http"):
        return None
    return {"inline_keyboard": [[{"text": "Open lead", "url": url}]]}


def format_lead_for_telegram(lead, title="Lead"):
    import html as _html
    stage_label = LEAD_STAGE_LABELS.get(lead["stage"], lead["stage"])
    contact_bits = []
    if lead["contact_person"]:
        contact_bits.append(lead["contact_person"])
    if lead["phone"]:
        contact_bits.append(lead["phone"])
    if lead["email"]:
        contact_bits.append(lead["email"])
    contact = " | ".join(contact_bits) or "No contact saved"
    owner = lead["assigned_name"] if "assigned_name" in lead.keys() and lead["assigned_name"] else "Unassigned"
    lines = [
        f"<b>{_html.escape(title)} #{lead['id']}</b>",
        "",
        f"<b>{_html.escape(lead['company_name'])}</b>",
        f"Stage: {_html.escape(stage_label)}",
        f"Owner: {_html.escape(owner)}",
        f"Contact: {_html.escape(contact)}",
    ]
    if lead["source"]:
        lines.append(f"Source: {_html.escape(lead['source'])}")
    if lead["estimated_value"]:
        lines.append(f"Value: {_html.escape(str(lead['estimated_value']))}")
    if lead["next_followup_at"]:
        lines.append(f"Next follow-up: {_html.escape(lead['next_followup_at'])}")
    if lead["requirement"]:
        lines.extend(["", _html.escape(lead["requirement"])])
    lines.extend([
        "",
        f"<code>/leadedit {lead['id']}</code>",
        f"<code>/leadstage {lead['id']} discussion</code>",
        f"<code>/followup {lead['id']} 2026-05-05 10:00 Call back</code>",
    ])
    return "\n".join(lines)


def telegram_command_parts(text):
    import re
    lines = (text or "").strip().splitlines()
    if not lines:
        return "", ""
    first = lines[0].strip()
    match = re.match(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@\w+)?(?:\s+(.*))?$", first, flags=re.I)
    if not match:
        return "", ""
    args = match.group(2) or ""
    rest = "\n".join(lines[1:]).strip()
    body = args if not rest else f"{args}\n{rest}".strip()
    return match.group(1).lower(), body


def telegram_extract_lead_id(args):
    import re
    match = re.match(r"^(?:lead\s*)?#?(\d+)\b\s*(.*)$", (args or "").strip(), flags=re.I | re.S)
    if not match:
        return 0, ""
    return int(match.group(1)), (match.group(2) or "").strip()


def telegram_chat_id_text(chat):
    chat_id = (chat or {}).get("id", "")
    return str(chat_id).strip() if chat_id not in (None, "") else ""


def telegram_chat_is_sales_team(chat):
    import re
    chat = chat or {}
    title = chat.get("title") or chat.get("username") or ""
    normalized = re.sub(r"[^a-z0-9]+", "", str(title).lower())
    return "sales" in normalized


def telegram_chat_is_hr_team(chat):
    import re
    chat = chat or {}
    title = chat.get("title") or chat.get("username") or ""
    normalized = re.sub(r"[^a-z0-9]+", "", str(title).lower())
    return "hr" in normalized or "humanresources" in normalized


def telegram_private_sender_is_superadmin(db, message):
    sender = (message or {}).get("from") or {}
    sender_id = str(sender.get("id", "")).strip()
    if not sender_id:
        return False
    try:
        row = db.execute("""
            SELECT id FROM employees
            WHERE role='superadmin' AND telegram_chat_id=?
            LIMIT 1
        """, (sender_id,)).fetchone()
        return bool(row)
    except Exception:
        return False


def telegram_sender_role(db, message):
    sender = (message or {}).get("from") or {}
    sender_id = str(sender.get("id", "")).strip()
    if not sender_id:
        return ""
    try:
        row = db.execute("""
            SELECT role FROM employees
            WHERE telegram_chat_id=?
            ORDER BY CASE role WHEN 'superadmin' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END
            LIMIT 1
        """, (sender_id,)).fetchone()
        return row["role"] if row else ""
    except Exception:
        return ""


def telegram_sender_can_delete_bot_report(db, message):
    return telegram_sender_role(db, message) in ("superadmin", "admin")


def handle_telegram_delete_report_command(db, source, update, message, text, external_id):
    cmd, args = telegram_command_parts(text)
    if cmd not in ("deletereport", "deletewrongreport", "deletebotreport"):
        return False
    chat = message.get("chat") or {}
    chat_id = telegram_chat_id_text(chat)
    if chat.get("type") not in ("group", "supergroup"):
        if chat_id:
            send_telegram(chat_id, "Reply to the wrong report in the Telegram group with <code>/deletereport</code>.")
        log_lead_intake_event(db, source, "telegram", "delete_report_private", update, 0, external_id)
        db.commit()
        return True
    target = message.get("reply_to_message") or {}
    target_id = target.get("message_id")
    target_from = target.get("from") or {}
    target_text = target.get("text") or target.get("caption") or ""
    bot_user_id = (get_telegram_bot_token() or ":").split(":", 1)[0]
    target_is_this_bot = str(target_from.get("id", "")) == bot_user_id
    target_is_report = (
        "Monthly Attendance Summary" in target_text
        or "Daily Staff Activity Report" in target_text
    )
    if not target_id or not target_is_this_bot or not target_is_report:
        if chat_id:
            send_telegram(chat_id, "Reply directly to the bot's HR report message with <code>/deletereport</code>.")
        log_lead_intake_event(db, source, "telegram", "delete_report_bad_target", update, 0, external_id)
        db.commit()
        return True
    deleted_report = delete_telegram_message(chat_id, target_id)
    delete_telegram_message(chat_id, message.get("message_id"))
    log_lead_intake_event(db, source, "telegram", "monthly_report_deleted" if deleted_report else "monthly_report_delete_failed", update, 0, external_id)
    db.commit()
    if not deleted_report and chat_id:
        send_telegram(chat_id, "I could not delete that report. Please make sure the bot is an admin with delete permission.")
    return True


def handle_telegram_hr_group_command(db, source, update, message, text, external_id):
    cmd, args = telegram_command_parts(text)
    if cmd not in ("hrgroup", "attendancegroup", "reportsgroup"):
        return False
    chat = message.get("chat") or {}
    chat_id = telegram_chat_id_text(chat)
    if not chat_id or chat.get("type") not in ("group", "supergroup"):
        if chat_id:
            send_telegram(chat_id, "Send <code>/hrgroup</code> inside the HR Team Telegram group.")
        log_lead_intake_event(db, source, "telegram", "hr_group_private", update, 0, external_id)
        db.commit()
        return True
    if chat_id == get_telegram_group_chat_id():
        send_telegram(chat_id, "This is General group. Attendance reports must use the separate HR Team group.")
        log_lead_intake_event(db, source, "telegram", "hr_group_general_rejected", update, 0, external_id)
        db.commit()
        return True
    if not telegram_chat_is_hr_team(chat):
        send_telegram(chat_id, "This command only connects the HR Team group.")
        log_lead_intake_event(db, source, "telegram", "hr_group_name_rejected", update, 0, external_id)
        db.commit()
        return True
    set_app_setting_on_db(db, "monthly_attendance_chat_id", chat_id)
    set_app_setting_on_db(db, "monthly_attendance_enabled", "1")
    set_app_setting_on_db(db, "daily_staff_report_chat_id", chat_id)
    set_app_setting_on_db(db, "daily_staff_report_enabled", "1")
    send_telegram(
        chat_id,
        "Connected. HR reports will be sent to this HR Team group only, not General group."
    )
    log_lead_intake_event(db, source, "telegram", "hr_group_connected", update, 0, external_id)
    db.commit()
    return True


def telegram_sales_command_allowed(db, message):
    chat = (message or {}).get("chat") or {}
    chat_id = telegram_chat_id_text(chat)
    configured_sales_chat_id = get_sales_telegram_chat_id(db)
    if configured_sales_chat_id:
        return chat_id == configured_sales_chat_id
    if chat.get("type") in ("group", "supergroup") and telegram_chat_is_sales_team(chat):
        return True
    if chat.get("type") == "private" and telegram_private_sender_is_superadmin(db, message):
        return True
    return False


def maybe_bind_sales_telegram_group(db, message):
    chat = (message or {}).get("chat") or {}
    chat_id = telegram_chat_id_text(chat)
    if not chat_id or chat.get("type") not in ("group", "supergroup"):
        return False
    if not telegram_chat_is_sales_team(chat):
        send_telegram(chat_id, "This command only connects the dedicated Sales Team group.")
        return True
    general_chat_id = get_telegram_group_chat_id()
    if chat_id == general_chat_id:
        send_telegram(chat_id, "This is the general Telegram group. Please use a separate Sales Team group for leads.")
        return True
    set_app_setting_on_db(db, "sales_telegram_chat_id", chat_id)
    existing = db.execute("SELECT value FROM app_settings WHERE key='lead_reminder_chat_id'").fetchone()
    if not existing or not (existing["value"] or "").strip():
        set_app_setting_on_db(db, "lead_reminder_chat_id", chat_id)
    send_telegram(
        chat_id,
        "Connected. New lead notifications, follow-up reminders, and lead edit commands will use this Sales Team group.\n\n"
        + telegram_sales_help_text()
    )
    return True


def maybe_auto_bind_sales_group(db, message):
    chat = (message or {}).get("chat") or {}
    chat_id = telegram_chat_id_text(chat)
    if not chat_id or chat.get("type") not in ("group", "supergroup"):
        return
    if get_sales_telegram_chat_id(db) or not telegram_chat_is_sales_team(chat):
        return
    if chat_id == get_telegram_group_chat_id():
        return
    set_app_setting_on_db(db, "sales_telegram_chat_id", chat_id)


def fetch_lead_for_telegram(db, lead_id):
    return db.execute("""
        SELECT l.*, e.name as assigned_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        WHERE l.id=?
    """, (lead_id,)).fetchone()


def update_lead_from_telegram_data(db, lead_id, data):
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return None, ["Lead not found."]
    updates = []
    params = []
    changes = []
    field_map = {
        "company_name": "company_name",
        "contact_person": "contact_person",
        "email": "email",
        "phone": "phone",
        "source": "source",
        "requirement": "requirement",
        "notes": "notes",
    }
    for key, column in field_map.items():
        if key not in data:
            continue
        value = (data.get(key) or "").strip()
        if column == "company_name" and not value:
            continue
        if value != (lead[column] or ""):
            updates.append(f"{column}=?")
            params.append(value)
            changes.append(column.replace("_", " "))

    if "estimated_value" in data:
        value = safe_float(data.get("estimated_value"), lead["estimated_value"] or 0)
        if value != (lead["estimated_value"] or 0):
            updates.append("estimated_value=?")
            params.append(value)
            changes.append("estimated value")

    if "assigned_to" in data:
        value = resolve_lead_assignee(db, data.get("assigned_to"), lead["assigned_to"] or 0)
        if value != (lead["assigned_to"] or 0):
            updates.append("assigned_to=?")
            params.append(value)
            changes.append("owner")

    stage = None
    if "stage" in data:
        stage = normalize_lead_stage(data.get("stage"), lead["stage"], allow_converted=False)
        if stage != lead["stage"]:
            updates.append("stage=?")
            params.append(stage)
            changes.append("stage")

    if updates:
        db.execute(f"""
            UPDATE leads SET {', '.join(updates)}, updated_at=datetime('now','localtime')
            WHERE id=?
        """, params + [lead_id])
        if stage and stage != lead["stage"]:
            db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                          VALUES (?,?,?,?,?)""",
                       (lead_id, lead["stage"], stage, "Updated from Telegram sales group", 0))

    due_at = data.get("due_at", "")
    if due_at:
        due_at = due_at.replace("T", " ")[:16]
        try:
            datetime.strptime(due_at, "%Y-%m-%d %H:%M")
        except ValueError:
            due_at = ""
    if not due_at:
        due_at = parse_lead_followup_at(data.get("followup_date", ""), data.get("followup_time", ""))
    followup_note = (data.get("followup_note") or "").strip()
    if due_at:
        db.execute("""INSERT INTO lead_followups (lead_id, due_at, note, created_by)
                      VALUES (?,?,?,?)""", (lead_id, due_at, followup_note or "Follow up from Telegram sales group", 0))
        refresh_lead_next_followup(db, lead_id)
        changes.append("follow-up")

    return fetch_lead_for_telegram(db, lead_id), changes


def telegram_leads_list_message(db, mode="open"):
    import html as _html
    mode = (mode or "open").strip().lower()
    now_str = get_tz_now().strftime("%Y-%m-%d %H:%M")
    params = []
    where = ["l.stage != 'converted'"]
    title = "Open leads"
    order = "l.updated_at DESC, l.id DESC"
    if mode in ("due", "overdue"):
        where.append("COALESCE(l.next_followup_at, '') != '' AND l.next_followup_at <= ?")
        params.append(now_str)
        title = "Due leads"
        order = "l.next_followup_at ASC"
    elif mode == "today":
        where.append("substr(COALESCE(l.next_followup_at, ''), 1, 10)=?")
        params.append(get_tz_now().date().isoformat())
        title = "Today's lead follow-ups"
        order = "l.next_followup_at ASC"
    rows = db.execute(f"""
        SELECT l.*, e.name as assigned_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT 10
    """, params).fetchall()
    if not rows:
        return f"<b>{_html.escape(title)}</b>\n\nNo leads found."
    lines = [f"<b>{_html.escape(title)}</b>"]
    for lead in rows:
        stage = LEAD_STAGE_LABELS.get(lead["stage"], lead["stage"])
        due = f" | {lead['next_followup_at']}" if lead["next_followup_at"] else ""
        lines.append(
            f"#{lead['id']} <b>{_html.escape(lead['company_name'])}</b> - "
            f"{_html.escape(stage)} - {_html.escape(lead['assigned_name'] or 'Unassigned')}{_html.escape(due)}"
        )
    lines.extend(["", "<code>/leadinfo 12</code> for full details."])
    return "\n".join(lines)


def handle_telegram_sales_command(db, source, update, message, text, external_id):
    cmd, args = telegram_command_parts(text)
    if not cmd:
        return False
    chat = message.get("chat") or {}
    chat_id = telegram_chat_id_text(chat)

    if cmd in ("salesgroup", "connectsales", "saleschat"):
        handled = maybe_bind_sales_telegram_group(db, message)
        log_lead_intake_event(db, source, "telegram", "sales_group_connected" if handled else "ignored", update, 0, external_id)
        db.commit()
        return True

    if cmd in ("start", "help", "leadhelp"):
        if chat_id:
            send_telegram(chat_id, telegram_sales_help_text())
        log_lead_intake_event(db, source, "telegram", "help", update, 0, external_id)
        db.commit()
        return True

    sales_commands = {"leadedit", "edit", "leadstage", "stage", "followup", "leadinfo", "leads"}
    if cmd not in sales_commands:
        return False

    maybe_auto_bind_sales_group(db, message)
    if not telegram_sales_command_allowed(db, message):
        if chat_id:
            send_telegram(chat_id, "Lead commands are enabled only in the configured Sales Team group or a linked superadmin private chat.")
        log_lead_intake_event(db, source, "telegram", "unauthorized_command", update, 0, external_id)
        db.commit()
        return True

    lead_id = 0
    if cmd == "leads":
        if chat_id:
            send_telegram(chat_id, telegram_leads_list_message(db, args))
        log_lead_intake_event(db, source, "telegram", "lead_list", update, 0, external_id)
        db.commit()
        return True

    lead_id, body = telegram_extract_lead_id(args)
    if not lead_id:
        if chat_id:
            send_telegram(chat_id, "Please include a lead id, for example <code>/leadinfo 12</code>.")
        log_lead_intake_event(db, source, "telegram", "bad_command", update, 0, external_id)
        db.commit()
        return True

    if cmd == "leadinfo":
        lead = fetch_lead_for_telegram(db, lead_id)
        if chat_id:
            if lead:
                send_telegram(chat_id, format_lead_for_telegram(lead, "Lead"), telegram_open_lead_markup(lead_id))
            else:
                send_telegram(chat_id, f"Lead #{lead_id} was not found.")
        log_lead_intake_event(db, source, "telegram", "lead_info", update, lead_id, external_id)
        db.commit()
        return True

    if cmd in ("leadstage", "stage"):
        stage = normalize_lead_stage(body, "", allow_converted=False)
        if not stage:
            stages = ", ".join(label for stage_value, label in LEAD_STAGES if stage_value != "converted")
            send_telegram(chat_id, f"Use one of these stages: {stages}")
            log_lead_intake_event(db, source, "telegram", "bad_stage", update, lead_id, external_id)
            db.commit()
            return True
        lead, changes = update_lead_from_telegram_data(db, lead_id, {"stage": stage})
        if chat_id:
            if lead:
                send_telegram(chat_id, format_lead_for_telegram(lead, "Lead updated"), telegram_open_lead_markup(lead_id))
            else:
                send_telegram(chat_id, f"Lead #{lead_id} was not found.")
        log_lead_intake_event(db, source, "telegram", "lead_stage_updated", update, lead_id, external_id)
        db.commit()
        return True

    if cmd == "followup":
        import re
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?(?:\s+(.+))?$", body or "")
        if not match:
            send_telegram(chat_id, "Use: <code>/followup 12 2026-05-05 10:00 Call client</code>")
            log_lead_intake_event(db, source, "telegram", "bad_followup", update, lead_id, external_id)
            db.commit()
            return True
        data = {
            "followup_date": match.group(1),
            "followup_time": match.group(2) or "09:00",
            "followup_note": match.group(3) or "Follow up from Telegram sales group",
        }
        lead, changes = update_lead_from_telegram_data(db, lead_id, data)
        if chat_id:
            if lead:
                send_telegram(chat_id, format_lead_for_telegram(lead, "Follow-up added"), telegram_open_lead_markup(lead_id))
            else:
                send_telegram(chat_id, f"Lead #{lead_id} was not found.")
        log_lead_intake_event(db, source, "telegram", "lead_followup_added", update, lead_id, external_id)
        db.commit()
        return True

    data = parse_lead_field_lines(body)
    if not data:
        send_telegram(chat_id, "Send editable fields after the lead id, for example:\n<code>/leadedit 12\nStage: discussion\nPhone: +91...</code>")
        log_lead_intake_event(db, source, "telegram", "bad_edit", update, lead_id, external_id)
        db.commit()
        return True
    lead, changes = update_lead_from_telegram_data(db, lead_id, data)
    if chat_id:
        if lead:
            send_telegram(chat_id, format_lead_for_telegram(lead, "Lead updated"), telegram_open_lead_markup(lead_id))
        else:
            send_telegram(chat_id, f"Lead #{lead_id} was not found.")
    log_lead_intake_event(db, source, "telegram", "lead_updated", update, lead_id, external_id)
    db.commit()
    return True


def lead_intake_duplicate(db, provider, external_id):
    if not external_id:
        return None
    return db.execute(
        "SELECT * FROM lead_intake_events WHERE provider=? AND external_id=? LIMIT 1",
        (provider, external_id)
    ).fetchone()


def log_lead_intake_event(db, source, provider, status, payload, lead_id=0, external_id=""):
    try:
        db.execute("""
            INSERT INTO lead_intake_events (source_id, provider, external_id, lead_id, status, payload_json, remote_addr)
            VALUES (?,?,?,?,?,?,?)
        """, (
            source["id"] if source else 0,
            provider,
            external_id or "",
            lead_id or 0,
            status,
            json.dumps(payload or {}, ensure_ascii=False)[:12000],
            request.headers.get("X-Forwarded-For", request.remote_addr or "")[:120],
        ))
    except sqlite3.IntegrityError:
        pass


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0):
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def safe_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("0", "false", "off", "no", "")


def resolve_lead_assignee(db, value, default=0):
    if value in (None, ""):
        return default or 0
    raw = str(value).strip()
    if raw.lower() in ("0", "none", "unassigned", "no owner", "no-owner"):
        return 0
    direct_id = safe_int(raw, 0)
    if direct_id:
        return direct_id
    try:
        row = db.execute("""
            SELECT id FROM employees
            WHERE role IN ('sales','manager','admin','superadmin')
              AND (lower(name)=lower(?) OR lower(username)=lower(?))
            ORDER BY CASE role WHEN 'sales' THEN 1 WHEN 'manager' THEN 2 WHEN 'admin' THEN 3 ELSE 4 END, id
            LIMIT 1
        """, (raw, raw)).fetchone()
        return row["id"] if row else (default or 0)
    except Exception:
        return default or 0


def create_lead_from_intake(db, source, payload, provider="website", raw_payload=None, external_id=""):
    data = normalize_lead_intake_data(payload)
    stage = normalize_lead_stage(data.get("stage"), source["default_stage"] or "enquiry", allow_converted=False)
    assigned_to = resolve_lead_assignee(db, data.get("assigned_to"), source["assigned_to"] or 0)
    company_name = (data.get("company_name") or data.get("contact_person") or data.get("phone") or data.get("email") or "").strip()
    if not company_name:
        company_name = f"Lead from {source['name']}"

    source_label = (data.get("source") or source["name"] or provider).strip()
    meta_lines = [f"Intake: {source['name']} ({provider})"]
    if data.get("source_url"):
        meta_lines.append(f"Page: {data['source_url']}")
    for utm_key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"):
        if data.get(utm_key):
            meta_lines.append(f"{utm_key}: {data[utm_key]}")
    known_keys = set(data.keys())
    alias_keys = {alias for aliases in LEAD_INTAKE_FIELD_ALIASES.values() for alias in aliases}
    extra_lines = []
    for key, value in (payload or {}).items():
        clean_key = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        if clean_key.startswith("_") or clean_key in known_keys or clean_key in alias_keys:
            continue
        if value not in (None, ""):
            extra_lines.append(f"{key}: {str(value).strip()}")
    notes = "\n".join([line for line in [data.get("notes", "").strip(), "\n".join(extra_lines), "\n".join(meta_lines)] if line])

    cursor = db.execute("""
        INSERT INTO leads (company_name, contact_person, email, phone, source, stage,
            estimated_value, requirement, notes, assigned_to, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        company_name,
        data.get("contact_person", ""),
        data.get("email", ""),
        data.get("phone", ""),
        source_label,
        stage,
        safe_float(data.get("estimated_value"), 0),
        data.get("requirement", ""),
        notes,
        assigned_to,
        0,
    ))
    lead_id = cursor.lastrowid
    db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                  VALUES (?,?,?,?,?)""", (lead_id, "", stage, f"Lead created from {source['name']}", 0))

    due_at = data.get("due_at", "")
    if due_at:
        due_at = due_at.replace("T", " ")[:16]
        try:
            datetime.strptime(due_at, "%Y-%m-%d %H:%M")
        except ValueError:
            due_at = ""
    if not due_at:
        due_at = parse_lead_followup_at(data.get("followup_date", ""), data.get("followup_time", ""))
    followup_note = data.get("followup_note", "").strip()
    if due_at and not followup_note:
        followup_note = f"Follow up lead from {source['name']}"
    if due_at and followup_note:
        db.execute("""INSERT INTO lead_followups (lead_id, due_at, note, created_by)
                      VALUES (?,?,?,?)""", (lead_id, due_at, followup_note, 0))
        refresh_lead_next_followup(db, lead_id)

    log_lead_intake_event(db, source, provider, "created", raw_payload or payload, lead_id, external_id)
    if assigned_to:
        assigned = db.execute("SELECT role FROM employees WHERE id=?", (assigned_to,)).fetchone()
        if assigned and assigned["role"] == "superadmin":
            create_notification(db, assigned_to, f"New lead: {company_name}", "info", f"/leads/{lead_id}")
    data["company_name"] = company_name
    return lead_id, data


def notify_lead_intake_created(source, lead_id, data):
    import html as _html
    chat_id = get_sales_telegram_chat_id() or get_lead_reminder_chat_id()
    if not chat_id:
        return
    msg = (
        f"<b>New Lead #{lead_id}</b>\n\n"
        f"Source: {_html.escape(source['name'])}\n"
        f"Company: {_html.escape(data.get('company_name') or '-')}\n"
        f"Contact: {_html.escape(data.get('contact_person') or '-')}\n"
        f"Phone: {_html.escape(data.get('phone') or '-')}\n"
        f"Email: {_html.escape(data.get('email') or '-')}\n\n"
        f"<code>/leadinfo {lead_id}</code>\n"
        f"<code>/leadstage {lead_id} discussion</code>\n"
        f"<code>/followup {lead_id} 2026-05-05 10:00 Call back</code>"
    )
    send_telegram(chat_id, msg, telegram_open_lead_markup(lead_id))


def _parse_day_list(value, default=None):
    """Parse a comma-separated day list like '0,2,4' into [0,2,4]. Empty → [default]."""
    if not value or not str(value).strip():
        return [default] if default is not None else []
    out = []
    for x in str(value).split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out or ([default] if default is not None else [])


def task_due_today(frequency, frequency_day, today_date=None, frequency_month=0, frequency_days=None):
    """Check if a recurring task is due on the given date.

    frequency: 'daily', 'weekly', or 'every-N' where N is months (1-12)
    frequency_days: comma-separated day list (preferred). For weekly: weekdays 0-6.
                    For monthly+: dates 1-31.
    frequency_day: legacy single-day fallback when frequency_days is empty.
    frequency_month: start month (1-12) for monthly+ frequencies.
    """
    if today_date is None:
        today_date = get_tz_now().date()
    if frequency == "daily":
        return True
    if frequency == "weekly":
        days = _parse_day_list(frequency_days, default=frequency_day if frequency_day is not None else 0)
        return today_date.weekday() in days
    # Handle 'every-N' (every-1 = monthly, every-3 = quarterly, every-12 = yearly)
    if frequency.startswith("every-"):
        try:
            interval = int(frequency.split("-")[1])
        except (IndexError, ValueError):
            return True
        if interval < 1 or interval > 12:
            return True
        target_days = _parse_day_list(frequency_days, default=frequency_day if frequency_day else 1)
        if not target_days:
            target_days = [1]
        # Check if today's date matches any target day (with end-of-month safety)
        import calendar as _cal
        last_day = _cal.monthrange(today_date.year, today_date.month)[1]
        matched = False
        for td in target_days:
            if td > last_day:
                # Cap day-31 in February etc. to last day of month
                if today_date.day == last_day:
                    matched = True
                    break
            elif today_date.day == td:
                matched = True
                break
        if not matched:
            return False
        start_month = frequency_month or 1
        month_offset = (today_date.month - start_month) % 12
        return month_offset % interval == 0
    return True  # default: treat as daily


# ── Auth Decorators ─────────────────────────────────────

ADMIN_ROLES = ("admin", "superadmin")
MANAGER_ROLES = ("manager", "admin", "superadmin")
PROJECT_MANAGER_ROLES = ("admin", "superadmin", "subadmin")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in ADMIN_ROLES:
            flash("Access denied. Admin only.", "error")
            return redirect(url_for("today_view"))
        return f(*args, **kwargs)
    return decorated


def manager_required(f):
    """Allow manager, admin, superadmin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in MANAGER_ROLES:
            flash("Access denied. Manager+ only.", "error")
            return redirect(url_for("today_view"))
        return f(*args, **kwargs)
    return decorated


def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "superadmin":
            flash("Access denied. Super Admin only.", "error")
            return redirect(url_for("today_view"))
        return f(*args, **kwargs)
    return decorated


def project_manager_required(f):
    """Admin, superadmin, or subadmin can access."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") not in PROJECT_MANAGER_ROLES:
            flash("Access denied.", "error")
            return redirect(url_for("today_view"))
        return f(*args, **kwargs)
    return decorated


# ── Auth Routes ─────────────────────────────────────────


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Rate limit — block after 5 failed attempts per IP within 5 min
        if _rate_limit_check():
            logger.warning(f"Login rate limit triggered for IP {_get_client_ip()}")
            flash("Too many failed login attempts. Please wait 5 minutes and try again.", "error")
            return render_template("login.html"), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        db = get_db()
        user = db.execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
        if user and verify_password(password, user["password"]):
            # Re-hash with bcrypt if still on SHA-256 (migration)
            if not user["password"].startswith("$2b$"):
                db.execute("UPDATE employees SET password=? WHERE id=?", (hash_password(password), user["id"]))
                db.commit()
            session.permanent = True
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["role"] = user["role"]
            _clear_failed_logins()
            logger.info(f"Login success: {username} from {_get_client_ip()}")
            today = get_tz_now().date().isoformat()
            existing = db.execute(
                "SELECT id FROM login_log WHERE employee_id=? AND login_date=?",
                (user["id"], today),
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO login_log (employee_id, login_date) VALUES (?,?)",
                    (user["id"], today),
                )
                db.commit()
            if user["must_change_password"]:
                session["must_change_password"] = True
                return redirect(url_for("force_change_password"))
            return redirect(url_for("today_view"))
        _record_failed_login(username)
        logger.warning(f"Login failure: user={username!r} from {_get_client_ip()}")
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── CSRF Protection ────────────────────────────────────


@app.before_request
def ensure_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


@app.before_request
def check_csrf_token():
    if request.method == "POST" and not request.path.startswith("/api/"):
        token = session.get("csrf_token", "")
        form_token = request.form.get("csrf_token", "")
        if not token or token != form_token:
            flash("Session expired. Please try again.", "error")
            return redirect(request.referrer or "/")


# ── Security headers on every response ──────────────────
@app.after_request
def add_security_headers(response):
    # Clickjacking protection — don't allow framing
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # MIME type sniffing protection
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Referrer policy — don't leak full URLs to other sites
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Permissions policy — disable unused browser APIs
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    # Minimal CSP — allow self + Google Fonts + Chart.js CDN (used on reports)
    if not request.path.startswith("/api/") and request.endpoint != "static":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'self';"
        )
    # HSTS only when actually served over HTTPS (via ProxyFix X-Forwarded-Proto)
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.before_request
def auto_secure_session():
    """Mark session cookies Secure when the request is HTTPS.
    This lets us deploy both HTTP (dev / LAN) and HTTPS (production) without reconfig."""
    if request.is_secure:
        app.config["SESSION_COOKIE_SECURE"] = True


def wants_json_response():
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


@app.errorhandler(404)
def handle_not_found(error):
    if wants_json_response():
        return jsonify({"error": "not_found"}), 404
    return make_response("Page not found", 404)


@app.errorhandler(413)
def handle_payload_too_large(error):
    if wants_json_response():
        return jsonify({"error": "payload_too_large", "max_bytes": app.config["MAX_CONTENT_LENGTH"]}), 413
    flash("The uploaded file is too large.", "error")
    return redirect(request.referrer or "/")


@app.errorhandler(500)
def handle_server_error(error):
    logger.exception("Unhandled request error: %s %s", request.method, request.path)
    if wants_json_response():
        return jsonify({"error": "server_error"}), 500
    return make_response("Something went wrong. The error has been logged.", 500)


# ── Login rate limiter (DB-backed — shared across gunicorn workers) ──
# 5 failures in 5 minutes per IP → temporarily locked out
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 300  # seconds

def _get_client_ip():
    # ProxyFix sets request.remote_addr to the real client IP from X-Forwarded-For
    return request.remote_addr or "unknown"

def _rate_limit_check():
    """Return True if the current IP is locked out."""
    import time as _t
    ip = _get_client_ip()
    cutoff = _t.time() - _LOGIN_WINDOW
    try:
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM login_attempts WHERE ip=? AND attempted_at >= ?",
            (ip, cutoff)
        ).fetchone()
        return (row["cnt"] if row else 0) >= _LOGIN_MAX_ATTEMPTS
    except Exception:
        return False  # on error, don't lock out (fail-open for availability)

def _record_failed_login(username=""):
    import time as _t
    try:
        db = get_db()
        db.execute(
            "INSERT INTO login_attempts (ip, attempted_at, username) VALUES (?,?,?)",
            (_get_client_ip(), _t.time(), (username or "")[:60])
        )
        # Opportunistically purge old rows — keeps the table small
        cutoff = _t.time() - (_LOGIN_WINDOW * 4)
        db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        db.commit()
    except Exception:
        pass

def _clear_failed_logins():
    try:
        db = get_db()
        db.execute("DELETE FROM login_attempts WHERE ip=?", (_get_client_ip(),))
        db.commit()
    except Exception:
        pass


# ── Force Password Change ──────────────────────────────


@app.before_request
def check_force_password():
    if session.get("must_change_password") and request.endpoint not in ("force_change_password", "logout", "static"):
        return redirect(url_for("force_change_password"))


@app.route("/force-change-password", methods=["GET", "POST"])
@login_required
def force_change_password():
    if request.method == "POST":
        new_pw = request.form.get("new_password", "").strip()
        if len(new_pw) < 4:
            flash("Password must be at least 4 characters.", "error")
            return render_template("force_password.html")
        db = get_db()
        db.execute("UPDATE employees SET password=?, must_change_password=0 WHERE id=?",
                   (hash_password(new_pw), session["user_id"]))
        db.commit()
        session.pop("must_change_password", None)
        flash("Password changed successfully!", "success")
        return redirect(url_for("today_view"))
    return render_template("force_password.html")


# ── Context Processors ─────────────────────────────────


@app.context_processor
def inject_branding():
    unread_count = 0
    if "user_id" in session:
        try:
            _db = open_db()
            row = _db.execute("SELECT COUNT(*) as cnt FROM notifications WHERE employee_id=? AND read=0", (session["user_id"],)).fetchone()
            unread_count = row[0] if row else 0
            _db.close()
        except Exception:
            pass
    active_projects = []
    if "user_id" in session:
        try:
            _db2 = open_db()
            _db2.row_factory = sqlite3.Row
            rows = _db2.execute("""
                SELECT p.id, p.name, c.name as client_name
                FROM projects p LEFT JOIN clients c ON p.client_id = c.id
                WHERE p.status='active'
                ORDER BY c.name, p.name
            """).fetchall()
            active_projects = [dict(r) for r in rows]
            _db2.close()
        except Exception:
            pass
    return {
        "app_name": get_app_setting("app_name", "Task Manager"),
        "primary_color": get_app_setting("primary_color", "#2563eb"),
        "app_logo_url": get_app_setting("app_logo_url", ""),
        "app_version": APP_VERSION,
        "unread_notifications": unread_count,
        "dark_mode": get_app_setting("dark_mode", "auto"),
        "active_projects": active_projects,
    }


# ── Today Dashboard (landing page for all users) ────────


@app.route("/")
@login_required
def index():
    return redirect(url_for("today_view"))


@app.route("/today")
@login_required
def today_view():
    db = get_db()
    emp_id = session["user_id"]
    employee = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    now = get_tz_now()
    today = now.date().isoformat()
    current_time = now.strftime("%H:%M")

    all_recurring = db.execute(
        "SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1 ORDER BY title",
        (emp_id,),
    ).fetchall()

    today_date = now.date()
    recurring = [t for t in all_recurring if task_due_today(t["frequency"] or "daily", t["frequency_day"] or 0, today_date, t["frequency_month"] or 0, t["frequency_days"] if "frequency_days" in t.keys() else "")]

    today_completions = {}
    for t in recurring:
        comp = db.execute(
            "SELECT * FROM recurring_completions WHERE task_id=? AND completion_date=?",
            (t["id"], today),
        ).fetchone()
        if comp:
            today_completions[t["id"]] = comp

    pending_oneoff = db.execute(
        "SELECT * FROM oneoff_tasks WHERE employee_id=? AND completed=0 ORDER BY created_at DESC",
        (emp_id,),
    ).fetchall()

    completed_today = db.execute(
        "SELECT * FROM oneoff_tasks WHERE employee_id=? AND completed=1 AND completion_date=?",
        (emp_id, today),
    ).fetchall()

    recurring_done = len(today_completions)
    recurring_total = len(recurring)
    oneoff_done_today = len(completed_today)
    total_done = recurring_done + oneoff_done_today

    time_logged = sum(c["time_minutes"] for c in today_completions.values())
    time_logged += sum(t["time_minutes"] for t in completed_today if t["time_minutes"])

    last_attendance = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id DESC LIMIT 1",
        (emp_id, today),
    ).fetchone()
    is_checked_in = last_attendance and last_attendance["action"] == "checkin"

    today_attendance = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id",
        (emp_id, today),
    ).fetchall()

    # Load comments for tasks
    task_comments = {}
    for t in recurring:
        comments = db.execute("""SELECT tc.comment as text, tc.created_at, e.name as author
            FROM task_comments tc JOIN employees e ON tc.employee_id = e.id
            WHERE tc.task_id=? AND tc.task_type='recurring' ORDER BY tc.created_at DESC LIMIT 5""",
            (t["id"],)).fetchall()
        if comments:
            task_comments[("recurring", t["id"])] = [dict(c) for c in comments]
    for t in list(pending_oneoff) + list(completed_today):
        comments = db.execute("""SELECT tc.comment as text, tc.created_at, e.name as author
            FROM task_comments tc JOIN employees e ON tc.employee_id = e.id
            WHERE tc.task_id=? AND tc.task_type='oneoff' ORDER BY tc.created_at DESC LIMIT 5""",
            (t["id"],)).fetchall()
        if comments:
            task_comments[("oneoff", t["id"])] = [dict(c) for c in comments]

    # Check celebrations for this employee
    celebrations = []
    today_mmdd = now.date().strftime("%m-%d")
    if employee["birthday"]:
        try:
            if employee["birthday"][5:] == today_mmdd:
                age = now.date().year - int(employee["birthday"][:4])
                celebrations.append({"type": "birthday", "years": age})
        except: pass
    if employee["joining_date"]:
        try:
            if employee["joining_date"][5:] == today_mmdd:
                years = now.date().year - int(employee["joining_date"][:4])
                if years > 0:
                    celebrations.append({"type": "work_anniversary", "years": years})
        except: pass
    if employee["wedding_anniversary"]:
        try:
            if employee["wedding_anniversary"][5:] == today_mmdd:
                years = now.date().year - int(employee["wedding_anniversary"][:4])
                if years > 0:
                    celebrations.append({"type": "wedding_anniversary", "years": years})
        except: pass

    return render_template(
        "today.html",
        employee=employee,
        recurring=recurring,
        today_completions=today_completions,
        pending_oneoff=pending_oneoff,
        completed_today=completed_today,
        today=today,
        current_time=current_time,
        recurring_done=recurring_done,
        recurring_total=recurring_total,
        total_done=total_done,
        time_logged=round(time_logged, 1),
        is_checked_in=is_checked_in,
        today_attendance=today_attendance,
        celebrations=celebrations,
        task_comments=task_comments,
    )


# ── Employee: Full Task Manager ─────────────────────────


@app.route("/dashboard")
@login_required
def my_dashboard():
    db = get_db()
    emp_id = session["user_id"]
    employee = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    today = get_tz_now().date().isoformat()

    recurring = db.execute(
        "SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1 ORDER BY title",
        (emp_id,),
    ).fetchall()

    today_completions = {}
    for t in recurring:
        comp = db.execute(
            "SELECT * FROM recurring_completions WHERE task_id=? AND completion_date=?",
            (t["id"], today),
        ).fetchone()
        if comp:
            today_completions[t["id"]] = comp

    oneoff = db.execute(
        "SELECT * FROM oneoff_tasks WHERE employee_id=? ORDER BY completed, created_at DESC",
        (emp_id,),
    ).fetchall()

    backlog = db.execute(
        "SELECT * FROM backlog_tasks WHERE employee_id=? ORDER BY priority DESC, created_at DESC",
        (emp_id,),
    ).fetchall()

    last_attendance = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id DESC LIMIT 1",
        (emp_id, today),
    ).fetchone()
    is_checked_in = last_attendance and last_attendance["action"] == "checkin"

    today_attendance = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id",
        (emp_id, today),
    ).fetchall()

    return render_template(
        "dashboard.html",
        employee=employee,
        recurring=recurring,
        today_completions=today_completions,
        oneoff=oneoff,
        backlog=backlog,
        today=today,
        is_checked_in=is_checked_in,
        today_attendance=today_attendance,
    )


# ── Leave Requests ─────────────────────────────────────


@app.route("/admin/leaves")
@login_required
def admin_leaves_redirect():
    return redirect(url_for("leaves_list"))


@app.route("/leaves")
@login_required
def leaves_list():
    if not (current_has_perm("apply_leave") or current_has_perm("view_team_leave") or current_has_perm("manage_leave")):
        flash("You don't have permission to access leave requests.", "error")
        return redirect(url_for("today_view"))

    db = get_db()
    user_id = session["user_id"]
    can_apply = current_has_perm("apply_leave")
    can_view_team = current_has_perm("view_team_leave") or current_has_perm("manage_leave")
    can_manage = current_has_perm("manage_leave")
    today = get_tz_now().date()

    status_filter = request.args.get("status", "all").strip()
    if status_filter not in LEAVE_STATUS_VALUES:
        status_filter = "all"
    emp_filter = request.args.get("employee", 0, type=int)
    from_date = request.args.get("from", today.replace(day=1).isoformat()).strip()
    to_date = request.args.get("to", (today + timedelta(days=90)).isoformat()).strip()
    if not parse_iso_date(from_date) or not parse_iso_date(to_date) or parse_iso_date(from_date) > parse_iso_date(to_date):
        from_date = today.replace(day=1).isoformat()
        to_date = (today + timedelta(days=90)).isoformat()

    leave_types = db.execute("""
        SELECT * FROM leave_types
        WHERE active=1
        ORDER BY paid DESC, name
    """).fetchall()
    all_leave_types = db.execute("SELECT * FROM leave_types ORDER BY active DESC, paid DESC, name").fetchall()
    team_employees = db.execute("""
        SELECT id, name, role, department
        FROM employees
        WHERE role IN ('employee','sales','accountant','subadmin','manager')
        ORDER BY name
    """).fetchall()
    current_employee = db.execute("SELECT id, name, role, department FROM employees WHERE id=?", (user_id,)).fetchone()

    where = ["lr.start_date <= ?", "lr.end_date >= ?"]
    params = [to_date, from_date]
    if status_filter != "all":
        where.append("lr.status=?")
        params.append(status_filter)
    if can_view_team:
        if emp_filter:
            where.append("lr.employee_id=?")
            params.append(emp_filter)
    else:
        where.append("lr.employee_id=?")
        params.append(user_id)
        emp_filter = user_id

    where_sql = " AND ".join(where)
    leave_requests = db.execute(f"""
        SELECT lr.*, e.name as employee_name, e.department as employee_department,
               COALESCE(lt.name, lr.leave_type_name, 'Leave') as type_name,
               COALESCE(lt.paid, 1) as paid,
               COALESCE(rb.name, '') as reviewer_name
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
        LEFT JOIN employees rb ON rb.id = lr.reviewed_by
        WHERE {where_sql}
        ORDER BY CASE lr.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'rejected' THEN 2 ELSE 3 END,
                 lr.start_date DESC, lr.id DESC
        LIMIT 300
    """, params).fetchall()

    visible_ids = [e["id"] for e in team_employees] if can_view_team else [user_id]
    stats = {"pending": 0, "approved_upcoming": 0, "on_leave_today": 0, "own_pending": 0}
    if visible_ids:
        placeholders = ",".join("?" for _ in visible_ids)
        stat_rows = db.execute(f"""
            SELECT status, COUNT(*) as cnt
            FROM leave_requests
            WHERE employee_id IN ({placeholders})
              AND status IN ('pending','approved')
              AND end_date >= ?
            GROUP BY status
        """, [*visible_ids, today.isoformat()]).fetchall()
        for row in stat_rows:
            if row["status"] == "pending":
                stats["pending"] = row["cnt"]
            elif row["status"] == "approved":
                stats["approved_upcoming"] = row["cnt"]
        stats["on_leave_today"] = db.execute(f"""
            SELECT COUNT(*) as cnt
            FROM leave_requests
            WHERE employee_id IN ({placeholders})
              AND status='approved'
              AND start_date <= ?
              AND end_date >= ?
        """, [*visible_ids, today.isoformat(), today.isoformat()]).fetchone()["cnt"]
    stats["own_pending"] = db.execute("""
        SELECT COUNT(*) as cnt FROM leave_requests
        WHERE employee_id=? AND status='pending'
    """, (user_id,)).fetchone()["cnt"]

    return render_template(
        "leaves.html",
        leave_requests=leave_requests,
        leave_types=leave_types,
        all_leave_types=all_leave_types,
        team_employees=team_employees,
        current_employee=current_employee,
        status_filter=status_filter,
        emp_filter=emp_filter,
        from_date=from_date,
        to_date=to_date,
        today=today.isoformat(),
        can_apply=can_apply,
        can_view_team=can_view_team,
        can_manage=can_manage,
        status_labels=LEAVE_STATUS_LABELS,
        status_badges=LEAVE_STATUS_BADGES,
        day_parts=LEAVE_DAY_PARTS,
        stats=stats,
        focus_id=request.args.get("focus", 0, type=int),
    )


@app.route("/leaves/apply", methods=["POST"])
@login_required
@require_perm("apply_leave")
def leave_apply():
    db = get_db()
    actor_id = session["user_id"]
    employee_id = request.form.get("employee_id", actor_id, type=int)
    if employee_id != actor_id and not current_has_perm("manage_leave"):
        employee_id = actor_id

    employee = db.execute("SELECT id, name FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("leaves_list"))

    leave_type_id = request.form.get("leave_type_id", 0, type=int)
    leave_type = db.execute("SELECT * FROM leave_types WHERE id=? AND active=1", (leave_type_id,)).fetchone()
    if not leave_type:
        flash("Select a valid leave type.", "error")
        return redirect(url_for("leaves_list"))

    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    start_part = request.form.get("start_day_part", "full").strip()
    end_part = request.form.get("end_day_part", "full").strip()
    reason = request.form.get("reason", "").strip()
    handover_notes = request.form.get("handover_notes", "").strip()
    start_day = parse_iso_date(start_date)
    end_day = parse_iso_date(end_date)

    if not start_day or not end_day or start_day > end_day:
        flash("Select a valid leave date range.", "error")
        return redirect(url_for("leaves_list"))
    if start_part not in LEAVE_DAY_PARTS:
        start_part = "full"
    if end_part not in LEAVE_DAY_PARTS:
        end_part = "full"
    if not reason:
        flash("Reason is required.", "error")
        return redirect(url_for("leaves_list"))

    total_days = calculate_leave_days(db, start_date, end_date, start_part, end_part)
    if total_days <= 0:
        flash("The selected range has no working leave days.", "error")
        return redirect(url_for("leaves_list"))

    conflict = leave_request_overlaps(db, employee_id, start_date, end_date)
    if conflict:
        flash(f"This overlaps an existing {conflict['status']} leave request.", "error")
        return redirect(url_for("leaves_list"))

    cursor = db.execute("""
        INSERT INTO leave_requests
            (employee_id, leave_type_id, leave_type_name, start_date, end_date,
             start_day_part, end_day_part, total_days, reason, handover_notes, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        employee_id, leave_type_id, leave_type["name"], start_date, end_date,
        start_part, end_part, total_days, reason, handover_notes, actor_id,
    ))
    leave_id = cursor.lastrowid
    add_leave_history(db, leave_id, actor_id, "", "pending", "Submitted")
    log_activity(
        db, employee_id, "requested", "leave",
        f"{leave_type['name']} leave: {start_date} to {end_date}",
        reason, notes=handover_notes, actor_id=actor_id, actor_role=session.get("role", ""),
    )
    notify_leave_reviewers(db, leave_id, employee["name"], leave_type["name"], start_date, end_date)
    db.commit()
    flash(f"Leave request submitted for {total_days:g} day(s).", "success")
    return redirect(url_for("leaves_list", status="pending", focus=leave_id))


@app.route("/leaves/<int:leave_id>/approve", methods=["POST"])
@login_required
@require_perm("manage_leave")
def leave_approve(leave_id):
    db = get_db()
    leave = db.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        flash("Leave request not found.", "error")
        return redirect(url_for("leaves_list"))
    if leave["status"] != "pending":
        flash("Only pending leave requests can be approved.", "error")
        return redirect(url_for("leaves_list", focus=leave_id))
    note = request.form.get("review_note", "").strip()
    actor_id = session["user_id"]
    db.execute("""
        UPDATE leave_requests
        SET status='approved', reviewed_by=?, reviewed_at=datetime('now','localtime'),
            review_note=?, updated_at=datetime('now','localtime')
        WHERE id=?
    """, (actor_id, note, leave_id))
    add_leave_history(db, leave_id, actor_id, leave["status"], "approved", note)
    log_activity(
        db, leave["employee_id"], "approved", "leave",
        f"Leave #{leave_id}: {leave['start_date']} to {leave['end_date']}",
        notes=note, actor_id=actor_id, actor_role=session.get("role", ""),
    )
    notify_leave_employee(db, leave, "Approved", note)
    db.commit()
    flash("Leave approved.", "success")
    return redirect(url_for("leaves_list", status="pending"))


@app.route("/leaves/<int:leave_id>/reject", methods=["POST"])
@login_required
@require_perm("manage_leave")
def leave_reject(leave_id):
    db = get_db()
    leave = db.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        flash("Leave request not found.", "error")
        return redirect(url_for("leaves_list"))
    if leave["status"] != "pending":
        flash("Only pending leave requests can be rejected.", "error")
        return redirect(url_for("leaves_list", focus=leave_id))
    note = request.form.get("review_note", "").strip()
    actor_id = session["user_id"]
    db.execute("""
        UPDATE leave_requests
        SET status='rejected', reviewed_by=?, reviewed_at=datetime('now','localtime'),
            review_note=?, updated_at=datetime('now','localtime')
        WHERE id=?
    """, (actor_id, note, leave_id))
    add_leave_history(db, leave_id, actor_id, leave["status"], "rejected", note)
    log_activity(
        db, leave["employee_id"], "rejected", "leave",
        f"Leave #{leave_id}: {leave['start_date']} to {leave['end_date']}",
        notes=note, actor_id=actor_id, actor_role=session.get("role", ""),
    )
    notify_leave_employee(db, leave, "Rejected", note)
    db.commit()
    flash("Leave rejected.", "success")
    return redirect(url_for("leaves_list", status="pending"))


@app.route("/leaves/<int:leave_id>/cancel", methods=["POST"])
@login_required
def leave_cancel(leave_id):
    db = get_db()
    leave = db.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        flash("Leave request not found.", "error")
        return redirect(url_for("leaves_list"))
    actor_id = session["user_id"]
    can_cancel_own = leave["employee_id"] == actor_id and leave["status"] == "pending"
    can_cancel_team = current_has_perm("manage_leave") and leave["status"] in ("pending", "approved")
    if not (can_cancel_own or can_cancel_team):
        flash("This leave request cannot be cancelled.", "error")
        return redirect(url_for("leaves_list", focus=leave_id))
    note = request.form.get("review_note", "").strip()
    old_status = leave["status"]
    db.execute("""
        UPDATE leave_requests
        SET status='cancelled', cancelled_at=datetime('now','localtime'),
            reviewed_by=CASE WHEN ? THEN ? ELSE reviewed_by END,
            reviewed_at=CASE WHEN ? THEN datetime('now','localtime') ELSE reviewed_at END,
            review_note=CASE WHEN ? THEN ? ELSE review_note END,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (1 if can_cancel_team else 0, actor_id, 1 if can_cancel_team else 0,
          1 if can_cancel_team else 0, note, leave_id))
    add_leave_history(db, leave_id, actor_id, old_status, "cancelled", note)
    log_activity(
        db, leave["employee_id"], "cancelled", "leave",
        f"Leave #{leave_id}: {leave['start_date']} to {leave['end_date']}",
        notes=note, actor_id=actor_id, actor_role=session.get("role", ""),
    )
    if can_cancel_team and actor_id != leave["employee_id"]:
        notify_leave_employee(db, leave, "Cancelled", note)
    db.commit()
    flash("Leave cancelled.", "success")
    return redirect(url_for("leaves_list"))


@app.route("/leaves/types/add", methods=["POST"])
@login_required
@require_perm("manage_leave")
def leave_type_add():
    import re
    db = get_db()
    name = request.form.get("name", "").strip()
    paid = 1 if request.form.get("paid", "1") == "1" else 0
    if not name:
        flash("Leave type name is required.", "error")
        return redirect(url_for("leaves_list"))
    code = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or secrets.token_hex(4)
    existing = db.execute("SELECT id FROM leave_types WHERE code=?", (code,)).fetchone()
    if existing:
        code = f"{code}_{secrets.token_hex(3)}"
    db.execute("INSERT INTO leave_types (name, code, paid, active) VALUES (?,?,?,1)", (name, code, paid))
    db.commit()
    flash("Leave type added.", "success")
    return redirect(url_for("leaves_list"))


@app.route("/leaves/types/<int:type_id>/toggle", methods=["POST"])
@login_required
@require_perm("manage_leave")
def leave_type_toggle(type_id):
    db = get_db()
    leave_type = db.execute("SELECT * FROM leave_types WHERE id=?", (type_id,)).fetchone()
    if leave_type:
        db.execute("UPDATE leave_types SET active=? WHERE id=?", (0 if leave_type["active"] else 1, type_id))
        db.commit()
        flash("Leave type updated.", "success")
    return redirect(url_for("leaves_list"))


# ── Employee Task Actions ───────────────────────────────


def _project_exists(db, project_id):
    if not project_id:
        return False
    return db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None


def _safe_project_id(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _project_required_response(db, project_id, html=True, message="project_id required before completing this task"):
    if _project_exists(db, project_id):
        return None
    if html:
        flash("Please select a project before completing this task.", "error")
        return redirect(request.referrer or url_for("my_dashboard"))
    return jsonify({"error": message}), 400


@app.route("/recurring/add", methods=["POST"])
@login_required
def add_recurring():
    emp_id = session["user_id"]
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    frequency = request.form.get("frequency", "daily").strip()
    if frequency == "weekly":
        # Accept multi-select: frequency_days_weekly=0&frequency_days_weekly=2 → "0,2"
        days_list = request.form.getlist("frequency_days_weekly")
        if not days_list:
            days_list = [request.form.get("frequency_day_weekly", "0")]
        frequency_days = ",".join(d for d in days_list if d.strip().isdigit())
        frequency_day = int(days_list[0]) if days_list and days_list[0].strip().isdigit() else 0
    else:
        # Monthly+: comma-separated input or single value
        days_input = request.form.get("frequency_days_monthly", "").strip()
        if days_input:
            frequency_days = ",".join(x.strip() for x in days_input.split(",") if x.strip().isdigit() and 1 <= int(x.strip()) <= 31)
            first_day = next((int(x.strip()) for x in days_input.split(",") if x.strip().isdigit()), 1)
            frequency_day = first_day
        else:
            frequency_day = request.form.get("frequency_day_monthly", request.form.get("frequency_day", 1), type=int)
            frequency_days = str(frequency_day)
    scheduled_time = request.form.get("scheduled_time", "").strip()
    frequency_month = request.form.get("frequency_month", 0, type=int)
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    if title:
        db = get_db()
        if not _project_exists(db, project_id):
            flash("Please select a project for this task.", "error")
            return redirect(request.referrer or url_for("my_dashboard"))
        db.execute(
            "INSERT INTO recurring_tasks (employee_id, title, description, frequency, frequency_day, frequency_days, frequency_month, drive_link, project_id, scheduled_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (emp_id, title, description, frequency, frequency_day, frequency_days, frequency_month, drive_link, project_id, scheduled_time),
        )
        log_activity(db, emp_id, "created", "recurring", title, description)
        db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/recurring/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_recurring(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM recurring_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("my_dashboard"))

    completion_date = request.form.get("completion_date", get_tz_now().date().isoformat())
    time_minutes = request.form.get("time_minutes", 0)
    notes = request.form.get("notes", "").strip()
    project_id = request.form.get("project_id", task["project_id"] or 0, type=int)
    project_error = _project_required_response(db, project_id)
    if project_error:
        return project_error
    if project_id != (task["project_id"] or 0):
        db.execute("UPDATE recurring_tasks SET project_id=? WHERE id=?", (project_id, task_id))

    db.execute(
        "DELETE FROM recurring_completions WHERE task_id=? AND completion_date=?",
        (task_id, completion_date),
    )
    db.execute(
        "INSERT INTO recurring_completions (task_id, completion_date, time_minutes, notes) VALUES (?,?,?,?)",
        (task_id, completion_date, float(time_minutes), notes),
    )
    log_activity(db, session["user_id"], "completed", "recurring", task["title"], task["description"], completion_date, float(time_minutes), notes)
    # Notify admins about task completion
    admins = db.execute("SELECT id FROM employees WHERE role IN ('admin','superadmin')").fetchall()
    for a in admins:
        if a["id"] != session["user_id"]:
            create_notification(db, a["id"], f"{session.get('user_name','Someone')} completed: {task['title']}", "task", "/admin/dashboard")
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/recurring/<int:task_id>/uncomplete", methods=["POST"])
@login_required
def uncomplete_recurring(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM recurring_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        return redirect(url_for("my_dashboard"))
    completion_date = request.form.get("completion_date", get_tz_now().date().isoformat())
    db.execute(
        "DELETE FROM recurring_completions WHERE task_id=? AND completion_date=?",
        (task_id, completion_date),
    )
    log_activity(db, session["user_id"], "uncompleted", "recurring", task["title"], task["description"], completion_date)
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/recurring/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_recurring(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM recurring_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        return redirect(url_for("my_dashboard"))
    log_activity(db, session["user_id"], "deleted", "recurring", task["title"], task["description"])
    db.execute("DELETE FROM recurring_completions WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM recurring_tasks WHERE id=?", (task_id,))
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/oneoff/add", methods=["POST"])
@login_required
def add_oneoff():
    emp_id = session["user_id"]
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    if title:
        db = get_db()
        if not _project_exists(db, project_id):
            flash("Please select a project for this task.", "error")
            return redirect(request.referrer or url_for("my_dashboard"))
        db.execute(
            "INSERT INTO oneoff_tasks (employee_id, title, description, drive_link, project_id) VALUES (?,?,?,?,?)",
            (emp_id, title, description, drive_link, project_id),
        )
        log_activity(db, emp_id, "created", "one-time", title, description)
        db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/oneoff/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_oneoff(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM oneoff_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        return redirect(url_for("my_dashboard"))
    completion_date = request.form.get("completion_date", get_tz_now().date().isoformat())
    time_minutes = request.form.get("time_minutes", 0)
    notes = request.form.get("notes", "").strip()
    project_id = request.form.get("project_id", task["project_id"] or 0, type=int)
    project_error = _project_required_response(db, project_id)
    if project_error:
        return project_error
    db.execute(
        "UPDATE oneoff_tasks SET completed=1, completion_date=?, time_minutes=?, notes=?, project_id=? WHERE id=?",
        (completion_date, float(time_minutes), notes, project_id, task_id),
    )
    log_activity(db, session["user_id"], "completed", "one-time", task["title"], task["description"], completion_date, float(time_minutes), notes)
    admins = db.execute("SELECT id FROM employees WHERE role IN ('admin','superadmin')").fetchall()
    for a in admins:
        if a["id"] != session["user_id"]:
            create_notification(db, a["id"], f"{session.get('user_name','Someone')} completed: {task['title']}", "task", "/admin/dashboard")
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/oneoff/<int:task_id>/uncomplete", methods=["POST"])
@login_required
def uncomplete_oneoff(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM oneoff_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        return redirect(url_for("my_dashboard"))
    db.execute(
        "UPDATE oneoff_tasks SET completed=0, completion_date=NULL, time_minutes=NULL, notes='' WHERE id=?",
        (task_id,),
    )
    log_activity(db, session["user_id"], "uncompleted", "one-time", task["title"], task["description"])
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/oneoff/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_oneoff(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM oneoff_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        return redirect(url_for("my_dashboard"))
    log_activity(db, session["user_id"], "deleted", "one-time", task["title"], task["description"])
    db.execute("DELETE FROM oneoff_tasks WHERE id=?", (task_id,))
    db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


# ── Admin: Home ─────────────────────────────────────────


@app.route("/admin")
@admin_required
def admin_home():
    db = get_db()
    employees = db.execute("SELECT * FROM employees ORDER BY role DESC, name").fetchall()
    shifts = db.execute("SELECT * FROM shifts ORDER BY start_time").fetchall()
    return render_template("admin_home.html", employees=employees, shifts=shifts)


@app.route("/admin/employee/add", methods=["POST"])
@login_required
@require_perm("create_team_member")
def admin_add_employee():
    name = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "employee")
    if role not in ROLE_ORDER:
        role = "employee"
    if role == "superadmin" and session.get("role") != "superadmin":
        role = "employee"
    telegram_chat_id = request.form.get("telegram_chat_id", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    department = request.form.get("department", "").strip()
    birthday = request.form.get("birthday", "").strip()
    joining_date = request.form.get("joining_date", "").strip()
    wedding_anniversary = request.form.get("wedding_anniversary", "").strip()
    shift_id = request.form.get("shift_id", 0, type=int)
    try: hourly_rate = float(request.form.get("hourly_rate", "0") or 0)
    except: hourly_rate = 0.0
    if name and username and password:
        db = get_db()
        existing = db.execute("SELECT id FROM employees WHERE username=?", (username,)).fetchone()
        if existing:
            flash(f"Username '{username}' already exists.", "error")
        else:
            cursor = db.execute(
                "INSERT INTO employees (name, username, password, role, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary, hourly_rate, shift_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, username, hash_password(password), role, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary, hourly_rate, shift_id),
            )
            new_emp_id = cursor.lastrowid
            db.execute("UPDATE employees SET must_change_password=1 WHERE id=?", (new_emp_id,))
            db.commit()
            log_activity(db, new_emp_id, "added member", "admin", name)
            create_notification(db, new_emp_id, "Welcome! Your account has been created.", "info", "/profile")
            db.commit()
            flash(f"Team member '{name}' added.", "success")
    return redirect(url_for("admin_home"))


@app.route("/admin/employee/<int:emp_id>/edit", methods=["POST"])
@login_required
@require_perm("edit_team_member")
def admin_edit_employee(emp_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    telegram_chat_id = request.form.get("telegram_chat_id", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    department = request.form.get("department", "").strip()
    birthday = request.form.get("birthday", "").strip()
    joining_date = request.form.get("joining_date", "").strip()
    wedding_anniversary = request.form.get("wedding_anniversary", "").strip()
    new_password = request.form.get("new_password", "").strip()
    new_role = request.form.get("role", "").strip()
    shift_id = request.form.get("shift_id", 0, type=int)
    try: hourly_rate = float(request.form.get("hourly_rate", "0") or 0)
    except: hourly_rate = 0.0
    if name:
        db.execute("UPDATE employees SET name=?, telegram_chat_id=?, email=?, phone=?, department=?, birthday=?, joining_date=?, wedding_anniversary=?, hourly_rate=?, shift_id=? WHERE id=?",
                   (name, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary, hourly_rate, shift_id, emp_id))
        if new_password:
            db.execute("UPDATE employees SET password=? WHERE id=?", (hash_password(new_password), emp_id))
        if new_role and session.get("role") == "superadmin":
            if new_role in ROLE_ORDER:
                db.execute("UPDATE employees SET role=? WHERE id=?", (new_role, emp_id))
        db.commit()
        log_activity(db, emp_id, "edited member", "admin", name)
        db.commit()
        flash("Team member updated.", "success")
    return redirect(url_for("admin_home"))


@app.route("/admin/employee/<int:emp_id>/delete", methods=["POST"])
@admin_required
def admin_delete_employee(emp_id):
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp and emp["role"] not in ("admin", "superadmin"):
        if emp["role"] == "subadmin" and session.get("role") != "superadmin":
            flash("Only Super Admin can delete sub-admins.", "error")
            return redirect(url_for("admin_home"))
        log_activity(db, session["user_id"], "deleted member", "admin", emp["name"])
        db.commit()
        db.execute("DELETE FROM recurring_completions WHERE task_id IN (SELECT id FROM recurring_tasks WHERE employee_id=?)", (emp_id,))
        db.execute("DELETE FROM recurring_tasks WHERE employee_id=?", (emp_id,))
        db.execute("DELETE FROM oneoff_tasks WHERE employee_id=?", (emp_id,))
        db.execute("DELETE FROM login_log WHERE employee_id=?", (emp_id,))
        db.execute("DELETE FROM attendance WHERE employee_id=?", (emp_id,))
        db.execute("UPDATE leave_request_history SET actor_id=0 WHERE actor_id=?", (emp_id,))
        db.execute("UPDATE leave_requests SET created_by=0 WHERE created_by=?", (emp_id,))
        db.execute("UPDATE leave_requests SET reviewed_by=0 WHERE reviewed_by=?", (emp_id,))
        db.execute("DELETE FROM leave_request_history WHERE leave_request_id IN (SELECT id FROM leave_requests WHERE employee_id=?)", (emp_id,))
        db.execute("DELETE FROM leave_requests WHERE employee_id=?", (emp_id,))
        db.execute("DELETE FROM activity_log WHERE employee_id=?", (emp_id,))
        db.execute("DELETE FROM employees WHERE id=?", (emp_id,))
        db.commit()
        flash(f"Team member '{emp['name']}' deleted.", "success")
    return redirect(url_for("admin_home"))


# ── Admin: Import Employees from Excel ──────────────────


@app.route("/admin/import-employees", methods=["POST"])
@login_required
@require_perm("import_team")
def admin_import_employees():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("admin_home"))

    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active

        headers = []
        for cell in next(ws.iter_rows(min_row=1, max_row=1)):
            headers.append(str(cell.value or "").strip().lower().replace(" ", "_"))

        db = get_db()
        added = 0
        skipped = 0
        errors = []

        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {}
            for i, cell in enumerate(row):
                if i < len(headers):
                    data[headers[i]] = str(cell.value or "").strip()

            name = data.get("name", "")
            username = data.get("username", "").lower()
            password = data.get("password", "")
            role = data.get("role", "employee")
            telegram_chat_id = data.get("telegram_chat_id", "") or data.get("telegram_id", "") or data.get("telegram", "")
            email = data.get("email", "")
            phone = data.get("phone", "")
            department = data.get("department", "")
            birthday = data.get("birthday", "")
            joining_date = data.get("joining_date", "")
            wedding_anniversary = data.get("wedding_anniversary", "")

            if not name or not username:
                if name or username:
                    errors.append(f"Row {row_idx}: missing name or username")
                continue

            if not password:
                password = username + "123"

            if role not in ROLE_ORDER:
                role = "employee"
            if role == "superadmin" and session.get("role") != "superadmin":
                role = "employee"

            existing = db.execute("SELECT id FROM employees WHERE username=?", (username,)).fetchone()
            if existing:
                skipped += 1
                continue

            db.execute(
                "INSERT INTO employees (name, username, password, role, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, username, hash_password(password), role, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary),
            )
            added += 1

        db.commit()
        msg = f"Import complete: {added} added, {skipped} skipped (existing usernames)."
        if errors:
            msg += f" {len(errors)} errors."
        flash(msg, "success" if added > 0 else "error")

    except ImportError:
        flash("openpyxl not installed on server. Contact admin.", "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")

    return redirect(url_for("admin_home"))


@app.route("/admin/employee-template")
@admin_required
def employee_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Employees"
        headers = ["name", "username", "password", "role", "telegram_chat_id", "email", "phone", "department", "birthday", "joining_date", "wedding_anniversary"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        sample = ["John Smith", "john", "john123", "employee", "", "john@example.com", "+91-9876543210", "Development", "1990-05-15", "2020-01-10", "2018-06-20"]
        for col, v in enumerate(sample, 1):
            ws.cell(row=2, column=col, value=v)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="employee_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("admin_home"))



# ── Import: Recurring Tasks from Excel ─────────────────


@app.route("/admin/import-tasks", methods=["POST"])
@admin_required
def admin_import_tasks():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("admin_home"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0
        errors = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {headers[i]: str(cell.value or "").strip() for i, cell in enumerate(row) if i < len(headers)}
            employee_username = data.get("employee_username", "") or data.get("username", "")
            title = data.get("title", "") or data.get("task_name", "")
            description = data.get("description", "")
            frequency = data.get("frequency", "daily") or "daily"
            frequency_day = int(data.get("frequency_day", "0") or "0")
            frequency_month = int(data.get("frequency_month", "0") or "0")
            if not employee_username or not title:
                if employee_username or title:
                    errors.append(f"Row {row_idx}: missing username or title")
                continue
            emp = db.execute("SELECT id FROM employees WHERE username=?", (employee_username.lower(),)).fetchone()
            if not emp:
                errors.append(f"Row {row_idx}: employee '{employee_username}' not found")
                continue
            db.execute(
                "INSERT INTO recurring_tasks (employee_id, title, description, frequency, frequency_day, frequency_month) VALUES (?,?,?,?,?,?)",
                (emp["id"], title, description, frequency, frequency_day, frequency_month),
            )
            log_activity(db, emp["id"], "created (import)", "recurring", title, description)
            added += 1
        db.commit()
        msg = f"Tasks import: {added} added."
        if errors:
            msg += f" {len(errors)} errors: " + "; ".join(errors[:5])
        flash(msg, "success" if added > 0 else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("admin_home"))


@app.route("/admin/task-template")
@admin_required
def task_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Tasks"
        headers = ["employee_username", "title", "description", "frequency", "frequency_day", "frequency_month"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        # Instructions row
        instructions = ["john (employee username)", "Daily Standup", "Morning standup meeting", "daily", "0", "0"]
        for col, v in enumerate(instructions, 1):
            ws.cell(row=2, column=col, value=v)
        instructions2 = ["john", "Monthly Report", "Submit monthly report", "every-1", "15", "1"]
        for col, v in enumerate(instructions2, 1):
            ws.cell(row=3, column=col, value=v)
        # Add instructions sheet
        ws2 = wb.create_sheet("Instructions")
        instructions_text = [
            ["Column", "Description", "Example"],
            ["employee_username", "Username of the employee (must exist)", "john"],
            ["title", "Task name (required)", "Daily Standup"],
            ["description", "Task description (optional)", "Morning meeting"],
            ["frequency", "daily, weekly, every-1 (monthly), every-2 (bi-monthly), every-3 (quarterly), every-6 (every 6 months), every-12 (yearly)", "every-3"],
            ["frequency_day", "For weekly: 0=Mon..6=Sun. For monthly+: day of month (1-31)", "15"],
            ["frequency_month", "Start month for monthly+ (1=Jan..12=Dec). Task repeats every N months from this month.", "1"],
        ]
        for r, row_data in enumerate(instructions_text, 1):
            for c, val in enumerate(row_data, 1):
                ws2.cell(row=r, column=c, value=val)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="task_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("admin_home"))


# ── Import: Clients from Excel ─────────────────────────


@app.route("/admin/import-clients", methods=["POST"])
@login_required
@require_perm("import_clients")
def admin_import_clients():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("clients_list"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0
        skipped = 0
        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {headers[i]: str(cell.value or "").strip() for i, cell in enumerate(row) if i < len(headers)}
            name = data.get("name", "") or data.get("client_name", "")
            if not name:
                continue
            existing = db.execute("SELECT id FROM clients WHERE name=?", (name,)).fetchone()
            if existing:
                skipped += 1
                continue
            db.execute(
                "INSERT INTO clients (name, contact_person, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
                (name, data.get("contact_person", ""), data.get("email", ""),
                 data.get("phone", ""), data.get("address", ""), data.get("notes", "")),
            )
            added += 1
        db.commit()
        flash(f"Clients import: {added} added, {skipped} skipped (existing).", "success" if added > 0 else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("clients_list"))


@app.route("/admin/client-template")
@superadmin_required
def client_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Clients"
        headers = ["name", "contact_person", "email", "phone", "address", "notes"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        sample = ["Acme Corp", "John Smith", "john@acme.com", "+1-555-0100", "123 Main St, NY", "Enterprise client"]
        for col, v in enumerate(sample, 1):
            ws.cell(row=2, column=col, value=v)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="client_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("clients_list"))


# ── Import: Projects from Excel ────────────────────────


@app.route("/admin/import-projects", methods=["POST"])
@login_required
@require_perm("import_projects")
def admin_import_projects():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("projects_list"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0
        errors = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {headers[i]: str(cell.value or "").strip() for i, cell in enumerate(row) if i < len(headers)}
            client_name = data.get("client_name", "") or data.get("client", "")
            name = data.get("project_name", "") or data.get("name", "")
            if not name:
                if client_name:
                    errors.append(f"Row {row_idx}: missing project name")
                continue
            client_id = 0
            if client_name:
                client = db.execute("SELECT id FROM clients WHERE name=?", (client_name,)).fetchone()
                if not client:
                    errors.append(f"Row {row_idx}: client '{client_name}' not found")
                    continue
                client_id = client["id"]
            db.execute(
                "INSERT INTO projects (client_id, name, description, services) VALUES (?,?,?,?)",
                (client_id, name, data.get("description", ""), data.get("services", "")),
            )
            added += 1
        db.commit()
        msg = f"Projects import: {added} added."
        if errors:
            msg += f" {len(errors)} errors: " + "; ".join(errors[:5])
        flash(msg, "success" if added > 0 else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("projects_list"))


@app.route("/admin/project-template")
@project_manager_required
def project_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Projects"
        headers = ["client_name", "project_name", "description", "services"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        sample = ["Acme Industries Pvt Ltd", "Acme \u2014 Digital Marketing", "Monthly retainer for digital services", "SEO, Social Media, Web Development"]
        for col, v in enumerate(sample, 1):
            ws.cell(row=2, column=col, value=v)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="project_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("projects_list"))


# ── Import: Attendance from Excel ──────────────────────


@app.route("/admin/import-attendance", methods=["POST"])
@admin_required
def admin_import_attendance():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("attendance_report"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0
        errors = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {headers[i]: str(cell.value or "").strip() for i, cell in enumerate(row) if i < len(headers)}
            employee_username = data.get("employee_username", "") or data.get("username", "")
            action = data.get("action", "").lower()
            action_date = data.get("date", "") or data.get("action_date", "")
            action_time = data.get("time", "") or data.get("action_time", "")
            if not employee_username or not action or not action_date or not action_time:
                if any([employee_username, action, action_date]):
                    errors.append(f"Row {row_idx}: missing required fields")
                continue
            if action not in ("checkin", "checkout"):
                errors.append(f"Row {row_idx}: action must be 'checkin' or 'checkout'")
                continue
            emp = db.execute("SELECT id FROM employees WHERE username=?", (employee_username.lower(),)).fetchone()
            if not emp:
                errors.append(f"Row {row_idx}: employee '{employee_username}' not found")
                continue
            db.execute(
                "INSERT INTO attendance (employee_id, action, action_date, action_time) VALUES (?,?,?,?)",
                (emp["id"], action, action_date, action_time),
            )
            added += 1
        db.commit()
        msg = f"Attendance import: {added} records added."
        if errors:
            msg += f" {len(errors)} errors: " + "; ".join(errors[:5])
        flash(msg, "success" if added > 0 else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("attendance_report"))


@app.route("/admin/attendance-template")
@admin_required
def attendance_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Attendance"
        headers = ["employee_username", "action", "date", "time"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        samples = [
            ["john", "checkin", "2026-04-01", "09:00:00"],
            ["john", "checkout", "2026-04-01", "18:00:00"],
        ]
        for r, sample in enumerate(samples, 2):
            for col, v in enumerate(sample, 1):
                ws.cell(row=r, column=col, value=v)
        ws2 = wb.create_sheet("Instructions")
        for r, row_data in enumerate([
            ["Column", "Description", "Example"],
            ["employee_username", "Username of employee (must exist)", "john"],
            ["action", "checkin or checkout", "checkin"],
            ["date", "Date in YYYY-MM-DD format", "2026-04-01"],
            ["time", "Time in HH:MM:SS format", "09:00:00"],
        ], 1):
            for c, val in enumerate(row_data, 1):
                ws2.cell(row=r, column=c, value=val)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="attendance_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("attendance_report"))


# ── Import: Invoices from Excel ────────────────────────


@app.route("/admin/import-invoices", methods=["POST"])
@login_required
@require_perm("import_invoices")
def admin_import_invoices():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("invoices_list"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0
        errors = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
            data = {headers[i]: str(cell.value or "").strip() for i, cell in enumerate(row) if i < len(headers)}
            client_name = data.get("client_name", "") or data.get("client", "")
            invoice_number = data.get("invoice_number", "")
            invoice_date = data.get("invoice_date", "") or data.get("date", "")
            total = float(data.get("total", "0") or "0")
            currency = data.get("currency", "INR") or "INR"
            status = data.get("status", "draft") or "draft"
            if not client_name or not invoice_number:
                if client_name or invoice_number:
                    errors.append(f"Row {row_idx}: missing client or invoice number")
                continue
            client = db.execute("SELECT id FROM clients WHERE name=?", (client_name,)).fetchone()
            if not client:
                errors.append(f"Row {row_idx}: client '{client_name}' not found")
                continue
            due_date = data.get("due_date", "")
            remarks = data.get("remarks", "")
            db.execute("""
                INSERT INTO invoices (client_id, invoice_number, invoice_date, due_date, subtotal, total, remarks, status, currency)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (client["id"], invoice_number, invoice_date, due_date, total, total, remarks, status, currency))
            added += 1
        db.commit()
        msg = f"Invoices import: {added} added."
        if errors:
            msg += f" {len(errors)} errors: " + "; ".join(errors[:5])
        flash(msg, "success" if added > 0 else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("invoices_list"))


@app.route("/admin/invoice-template")
@superadmin_required
def invoice_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Invoices"
        headers = ["client_name", "invoice_number", "invoice_date", "due_date", "total", "currency", "status", "remarks"]
        for col, h in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=h)
        sample = ["Acme Corp", "INV-202604-0001", "2026-04-01", "2026-04-30", "5000", "INR", "draft", "Payment due in 30 days"]
        for col, v in enumerate(sample, 1):
            ws.cell(row=2, column=col, value=v)
        ws2 = wb.create_sheet("Instructions")
        for r, row_data in enumerate([
            ["Column", "Description", "Example"],
            ["client_name", "Client name (must exist in Clients)", "Acme Corp"],
            ["invoice_number", "Unique invoice number", "INV-202604-0001"],
            ["invoice_date", "Date in YYYY-MM-DD", "2026-04-01"],
            ["due_date", "Due date in YYYY-MM-DD (optional)", "2026-04-30"],
            ["total", "Invoice total amount", "5000"],
            ["currency", "INR or USD", "INR"],
            ["status", "draft, sent, or paid", "draft"],
            ["remarks", "Notes (optional)", "Net 30"],
        ], 1):
            for c, val in enumerate(row_data, 1):
                ws2.cell(row=r, column=c, value=val)
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="invoice_import_template.xlsx",
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("invoices_list"))


# ── Admin: View Employee Dashboard ──────────────────────


@app.route("/admin/employee/<int:emp_id>")
@admin_required
def admin_view_employee(emp_id):
    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not employee:
        return redirect(url_for("admin_home"))
    today = get_tz_now().date().isoformat()

    recurring = db.execute(
        "SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1 ORDER BY title",
        (emp_id,),
    ).fetchall()

    today_completions = {}
    for t in recurring:
        comp = db.execute(
            "SELECT * FROM recurring_completions WHERE task_id=? AND completion_date=?",
            (t["id"], today),
        ).fetchone()
        if comp:
            today_completions[t["id"]] = comp

    oneoff = db.execute(
        "SELECT * FROM oneoff_tasks WHERE employee_id=? ORDER BY completed, created_at DESC",
        (emp_id,),
    ).fetchall()

    backlog = db.execute(
        "SELECT * FROM backlog_tasks WHERE employee_id=? ORDER BY priority DESC, created_at DESC",
        (emp_id,),
    ).fetchall()

    return render_template(
        "admin_employee.html",
        employee=employee,
        recurring=recurring,
        today_completions=today_completions,
        oneoff=oneoff,
        backlog=backlog,
        today=today,
    )


# ── Admin: Add tasks for employees ──────────────────────


@app.route("/admin/employee/<int:emp_id>/recurring/add", methods=["POST"])
@admin_required
def admin_add_recurring(emp_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    frequency = request.form.get("frequency", "daily").strip()
    if frequency == "weekly":
        days_list = request.form.getlist("frequency_days_weekly")
        if not days_list:
            days_list = [request.form.get("frequency_day_weekly", "0")]
        frequency_days = ",".join(d for d in days_list if d.strip().isdigit())
        frequency_day = int(days_list[0]) if days_list and days_list[0].strip().isdigit() else 0
    else:
        days_input = request.form.get("frequency_days_monthly", "").strip()
        if days_input:
            frequency_days = ",".join(x.strip() for x in days_input.split(",") if x.strip().isdigit() and 1 <= int(x.strip()) <= 31)
            first_day = next((int(x.strip()) for x in days_input.split(",") if x.strip().isdigit()), 1)
            frequency_day = first_day
        else:
            frequency_day = request.form.get("frequency_day_monthly", request.form.get("frequency_day", 1), type=int)
            frequency_days = str(frequency_day)
    scheduled_time = request.form.get("scheduled_time", "").strip()
    frequency_month = request.form.get("frequency_month", 0, type=int)
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    if title:
        db = get_db()
        if not _project_exists(db, project_id):
            flash("Please select a project for this task.", "error")
            return redirect(url_for("admin_view_employee", emp_id=emp_id))
        db.execute(
            "INSERT INTO recurring_tasks (employee_id, title, description, frequency, frequency_day, frequency_days, frequency_month, drive_link, project_id, scheduled_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (emp_id, title, description, frequency, frequency_day, frequency_days, frequency_month, drive_link, project_id, scheduled_time),
        )
        log_activity(db, emp_id, "created (by admin)", "recurring", title, description)
        db.commit()
    return redirect(url_for("admin_view_employee", emp_id=emp_id))


@app.route("/admin/employee/<int:emp_id>/oneoff/add", methods=["POST"])
@admin_required
def admin_add_oneoff(emp_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    if title:
        db = get_db()
        if not _project_exists(db, project_id):
            flash("Please select a project for this task.", "error")
            return redirect(url_for("admin_view_employee", emp_id=emp_id))
        db.execute(
            "INSERT INTO oneoff_tasks (employee_id, title, description, drive_link, project_id) VALUES (?,?,?,?,?)",
            (emp_id, title, description, drive_link, project_id),
        )
        log_activity(db, emp_id, "created (by admin)", "one-time", title, description)
        db.commit()
    return redirect(url_for("admin_view_employee", emp_id=emp_id))


# ── Admin: Calendar ─────────────────────────────────────


@app.route("/admin/calendar")
@admin_required
def calendar_view():
    db = get_db()
    employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()

    today_dt = get_tz_now().date()
    year = request.args.get("year", today_dt.year, type=int)
    month = request.args.get("month", today_dt.month, type=int)
    emp_filter = request.args.get("employee", 0, type=int)

    import calendar
    cal = calendar.monthcalendar(year, month)
    month_name = calendar.month_name[month]

    start = f"{year}-{month:02d}-01"
    end = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"

    params = (start, end, emp_filter) if emp_filter else (start, end)
    emp_clause = "AND rt.employee_id = ?" if emp_filter else ""

    recurring_data = db.execute(f"""
        SELECT rc.completion_date, rc.time_minutes, rt.title, e.name as employee_name, rc.notes
        FROM recurring_completions rc
        JOIN recurring_tasks rt ON rc.task_id = rt.id
        JOIN employees e ON rt.employee_id = e.id
        WHERE rc.completion_date >= ? AND rc.completion_date < ? {emp_clause}
        ORDER BY rc.completion_date
    """, params).fetchall()

    emp_clause2 = "AND ot.employee_id = ?" if emp_filter else ""
    oneoff_data = db.execute(f"""
        SELECT ot.completion_date, ot.time_minutes, ot.title, e.name as employee_name, ot.notes
        FROM oneoff_tasks ot
        JOIN employees e ON ot.employee_id = e.id
        WHERE ot.completed = 1 AND ot.completion_date >= ? AND ot.completion_date < ? {emp_clause2}
        ORDER BY ot.completion_date
    """, params).fetchall()

    day_tasks = {}
    for row in list(recurring_data) + list(oneoff_data):
        d = row["completion_date"]
        day_tasks.setdefault(d, []).append(dict(row))

    day_minutes = {}
    for d, tasks in day_tasks.items():
        day_minutes[d] = sum(t["time_minutes"] for t in tasks)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template(
        "calendar.html", employees=employees, cal=cal, year=year, month=month,
        month_name=month_name, day_tasks=day_tasks, day_minutes=day_minutes,
        emp_filter=emp_filter, prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month, today=today_dt.isoformat(),
    )


# ── Admin: KPI ──────────────────────────────────────────


@app.route("/admin/kpi")
@admin_required
def kpi_view():
    """Performance dashboard for owners / managers.

    Computes team and per-member metrics over the selected period,
    comparing against the previous period of the same length for trend arrows.
    The extra "quality" metrics are read-only derivations from existing logs.
    """
    db = get_db()
    employees = db.execute(
        "SELECT id, name, department, role FROM employees "
        "WHERE role IN ('employee','sales','accountant','subadmin','manager') "
        "AND lower(COALESCE(name, '')) NOT IN ('test', 'test employee') "
        "ORDER BY name"
    ).fetchall()

    emp_filter = request.args.get("employee", 0, type=int)
    days = request.args.get("days", 30, type=int)
    days = max(1, min(days, 365))

    end_date = get_tz_now().date()
    start_date = end_date - timedelta(days=days - 1)
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    def _safe_minutes(value):
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    def _fetch_work(p_start, p_end, emp_id=0):
        """Return list of work events: {date, minutes, emp_id, project_id, title, kind}."""
        emp_clause_r = "AND rt.employee_id = ?" if emp_id else ""
        emp_clause_o = "AND employee_id = ?" if emp_id else ""
        params_r = [p_start.isoformat(), p_end.isoformat()]
        params_o = [p_start.isoformat(), p_end.isoformat()]
        if emp_id:
            params_r.append(emp_id)
            params_o.append(emp_id)

        rec = db.execute(f"""
            SELECT rc.completion_date as date, rc.time_minutes as minutes,
                   rt.employee_id as emp_id, rt.project_id as project_id,
                   rt.title as title, 'recurring' as kind,
                   rt.id as task_id
            FROM recurring_completions rc
            JOIN recurring_tasks rt ON rc.task_id = rt.id
            WHERE rc.completion_date >= ? AND rc.completion_date <= ? {emp_clause_r}
        """, params_r).fetchall()

        one = db.execute(f"""
            SELECT completion_date as date, time_minutes as minutes,
                   employee_id as emp_id, project_id as project_id,
                   title as title, 'oneoff' as kind, id as task_id
            FROM oneoff_tasks
            WHERE completed = 1 AND completion_date >= ? AND completion_date <= ? {emp_clause_o}
        """, params_o).fetchall()

        events = [dict(r) for r in rec] + [dict(r) for r in one]
        for event in events:
            event["minutes"] = _safe_minutes(event.get("minutes"))
            event["project_id"] = event.get("project_id") or 0
            event["title"] = (event.get("title") or "").strip() or "Untitled task"
        return events

    def _fetch_attendance(p_start, p_end, emp_id=0):
        emp_clause = "AND employee_id = ?" if emp_id else ""
        params = [p_start.isoformat(), p_end.isoformat()]
        if emp_id:
            params.append(emp_id)
        rows = db.execute(f"""
            SELECT employee_id, action_date, action, action_time
            FROM attendance
            WHERE action_date >= ? AND action_date <= ? {emp_clause}
            ORDER BY employee_id, action_date, action_time
        """, params).fetchall()
        return [dict(r) for r in rows]

    def _parse_clock_minutes(value):
        try:
            parts = str(value or "").split(":")
            if len(parts) < 2:
                return None
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2]) if len(parts) > 2 else 0
            return hours * 60 + minutes + (seconds / 60.0)
        except (TypeError, ValueError):
            return None

    def _normalize_title(title):
        return " ".join((title or "").strip().lower().split())

    work = _fetch_work(start_date, end_date, emp_filter)
    prev_work = _fetch_work(prev_start, prev_end, emp_filter)
    attendance_rows = _fetch_attendance(start_date, end_date, emp_filter)

    # ── Team-level KPIs ───────────────────────────────
    def _agg(events):
        total_min = sum(_safe_minutes(e.get("minutes")) for e in events)
        total_tasks = len(events)
        active_emp_ids = set(e["emp_id"] for e in events)
        active_days = len(set(e["date"] for e in events))
        return {
            "hours": total_min / 60.0,
            "tasks": total_tasks,
            "active_members": len(active_emp_ids),
            "active_days": active_days,
        }

    now = _agg(work)
    prev = _agg(prev_work)

    def _delta(curr, previous):
        """Returns (pct_change, direction). None if no previous."""
        if previous == 0:
            return (None, "flat") if curr == 0 else (None, "up")
        pct = (curr - previous) / previous * 100
        direction = "up" if pct > 1 else ("down" if pct < -1 else "flat")
        return (round(pct, 1), direction)

    # Total staff for utilization
    total_staff = len(employees) if not emp_filter else 1
    utilization = round(now["active_members"] / total_staff * 100) if total_staff else 0

    kpis = {
        "hours":          {"value": round(now["hours"], 1), "delta": _delta(now["hours"], prev["hours"])},
        "tasks":          {"value": now["tasks"],           "delta": _delta(now["tasks"], prev["tasks"])},
        "active_members": {"value": now["active_members"],  "total": total_staff, "pct": utilization,
                           "delta": _delta(now["active_members"], prev["active_members"])},
        "avg_tasks_day":  {"value": round(now["tasks"] / max(now["active_days"], 1), 1),
                           "delta": _delta(now["tasks"] / max(now["active_days"], 1),
                                           prev["tasks"] / max(prev["active_days"], 1))},
    }

    # ── Per-member breakdown ─────────────────────────
    member_rows = []
    if emp_filter:
        member_list = [e for e in employees if e["id"] == emp_filter]
    else:
        member_list = list(employees)
    member_ids = [m["id"] for m in member_list]
    emp_lookup = {e["id"]: e for e in employees}

    # Build date list for sparklines
    date_list = []
    d = start_date
    while d <= end_date:
        date_list.append(d.isoformat())
        d += timedelta(days=1)

    # Group work by employee and day
    by_emp = {}
    by_emp_day = {}
    for e in work:
        by_emp.setdefault(e["emp_id"], []).append(e)
        day_bucket = by_emp_day.setdefault(e["emp_id"], {}).setdefault(
            e["date"], {"minutes": 0.0, "tasks": 0}
        )
        day_bucket["minutes"] += _safe_minutes(e.get("minutes"))
        day_bucket["tasks"] += 1

    # Attendance spans are the observed check-in/check-out window, not payroll truth.
    attendance_by_emp_day = {}
    for row in attendance_rows:
        attendance_by_emp_day.setdefault(row["employee_id"], {}).setdefault(
            row["action_date"], []
        ).append(row)

    def _attendance_summary(emp_id):
        days_map = attendance_by_emp_day.get(emp_id, {})
        attendance_days = set()
        missing_checkout_days = []
        orphan_checkout_days = []
        day_spans = {}
        total_span = 0.0
        for day_key, records in days_map.items():
            checkins = [
                _parse_clock_minutes(r.get("action_time"))
                for r in records if r.get("action") == "checkin"
            ]
            checkouts = [
                _parse_clock_minutes(r.get("action_time"))
                for r in records if r.get("action") == "checkout"
            ]
            checkins = [v for v in checkins if v is not None]
            checkouts = [v for v in checkouts if v is not None]
            if checkins:
                attendance_days.add(day_key)
            if checkins and checkouts:
                first_in = min(checkins)
                last_out = max(checkouts)
                if last_out < first_in:
                    last_out += 24 * 60
                span = max(0.0, last_out - first_in)
                day_spans[day_key] = span
                total_span += span
            elif checkins:
                missing_checkout_days.append(day_key)
            elif checkouts:
                orphan_checkout_days.append(day_key)
        return {
            "attendance_days": attendance_days,
            "attendance_minutes": total_span,
            "day_spans": day_spans,
            "missing_checkout_days": missing_checkout_days,
            "orphan_checkout_days": orphan_checkout_days,
        }

    attendance_summary_by_emp = {
        emp_id: _attendance_summary(emp_id)
        for emp_id in member_ids
    }
    approved_leave_by_emp = approved_leave_map(
        db, member_ids, start_date.isoformat(), end_date.isoformat()
    )

    # Build median duration baselines for repeated task titles.
    durations_by_key = {}
    for ev in work:
        minutes = _safe_minutes(ev.get("minutes"))
        key_title = _normalize_title(ev.get("title"))
        if minutes > 0 and key_title:
            durations_by_key.setdefault((ev.get("kind"), key_title), []).append(minutes)
    median_by_key = {
        key: median(values)
        for key, values in durations_by_key.items()
        if len(values) >= 3
    }

    project_name_cache = {0: "No Project"}

    def _project_label(project_id):
        pid = project_id or 0
        if pid in project_name_cache:
            return project_name_cache[pid]
        project = db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
        project_name_cache[pid] = project["name"] if project else f"Project #{pid}"
        return project_name_cache[pid]

    time_outliers = []
    outliers_by_emp = {}
    for ev in work:
        minutes = _safe_minutes(ev.get("minutes"))
        key = (ev.get("kind"), _normalize_title(ev.get("title")))
        typical = median_by_key.get(key)
        if not typical:
            continue
        multiple = minutes / typical if typical else 0
        if (minutes >= 90 and multiple >= 2.5) or (minutes >= 240 and typical <= 90):
            employee = emp_lookup.get(ev["emp_id"])
            item = {
                "employee_id": ev["emp_id"],
                "employee": employee["name"] if employee else f"Employee #{ev['emp_id']}",
                "date": ev["date"],
                "title": ev["title"],
                "kind": ev["kind"],
                "project": _project_label(ev.get("project_id")),
                "minutes": round(minutes),
                "hours": round(minutes / 60.0, 1),
                "typical_minutes": round(typical),
                "multiple": round(multiple, 1),
            }
            time_outliers.append(item)
            outliers_by_emp.setdefault(ev["emp_id"], []).append(item)
    time_outliers.sort(key=lambda o: (o["multiple"], o["minutes"]), reverse=True)
    time_outliers = time_outliers[:12]

    # Compute active recurring task counts per employee for completion rate.
    # "Expected" = number of recurring tasks that were due in the period.
    recurring_by_emp = {emp_id: [] for emp_id in member_ids}
    if member_ids:
        placeholders = ",".join("?" for _ in member_ids)
        recurring_rows = db.execute(f"""
            SELECT id, employee_id, frequency, frequency_day, frequency_month,
                   COALESCE(frequency_days, '') as frequency_days
            FROM recurring_tasks
            WHERE active=1 AND employee_id IN ({placeholders})
        """, member_ids).fetchall()
        for row in recurring_rows:
            recurring_by_emp.setdefault(row["employee_id"], []).append(row)
    else:
        recurring_rows = []

    task_ids = [row["id"] for row in recurring_rows]
    completed_recurring = set()
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        completed_rows = db.execute(f"""
            SELECT task_id, completion_date
            FROM recurring_completions
            WHERE completion_date >= ? AND completion_date <= ?
              AND task_id IN ({placeholders})
        """, [start_date.isoformat(), end_date.isoformat(), *task_ids]).fetchall()
        completed_recurring = {
            (row["task_id"], row["completion_date"])
            for row in completed_rows
        }

    for emp in member_list:
        emp_id = emp["id"]
        events = by_emp.get(emp_id, [])
        emp_min = sum(_safe_minutes(ev.get("minutes")) for ev in events)
        emp_tasks = len(events)
        emp_active_days = len(set(ev["date"] for ev in events))
        attendance_summary = attendance_summary_by_emp.get(emp_id, {
            "attendance_days": set(),
            "attendance_minutes": 0.0,
            "day_spans": {},
            "missing_checkout_days": [],
            "orphan_checkout_days": [],
        })

        # Daily sparkline data (hours per day)
        daily_hours = {dt: 0.0 for dt in date_list}
        daily_tasks = {dt: 0 for dt in date_list}
        for ev in events:
            if ev["date"] in daily_hours:
                daily_hours[ev["date"]] += _safe_minutes(ev.get("minutes")) / 60.0
                daily_tasks[ev["date"]] += 1
        spark = [round(daily_hours[dt], 2) for dt in date_list]

        # Completion rate: of recurring tasks due in this period, how many got done
        recurring_due = 0
        recurring_done = 0
        for rt in recurring_by_emp.get(emp_id, []):
            d = start_date
            while d <= end_date:
                if task_due_today(rt["frequency"] or "daily", rt["frequency_day"] or 0,
                                  d, rt["frequency_month"] or 0, rt["frequency_days"] or ""):
                    recurring_due += 1
                    if (rt["id"], d.isoformat()) in completed_recurring:
                        recurring_done += 1
                d += timedelta(days=1)

        completion_rate = round(recurring_done / recurring_due * 100) if recurring_due else None

        # Previous period tasks for delta
        prev_emp_events = [e for e in prev_work if e["emp_id"] == emp_id]
        prev_tasks = len(prev_emp_events)

        attendance_minutes = attendance_summary["attendance_minutes"]
        attendance_days = attendance_summary["attendance_days"]
        approved_leave_days = set(approved_leave_by_emp.get(emp_id, {}).keys())
        work_dates = set(daily_hours.keys())
        work_dates = {dt for dt in daily_hours if daily_tasks.get(dt, 0) > 0}
        work_without_attendance_days = sorted(work_dates - attendance_days)
        no_project_minutes = sum(
            _safe_minutes(ev.get("minutes"))
            for ev in events if not ev.get("project_id")
        )
        tasks_per_hour = round(emp_tasks / (emp_min / 60.0), 2) if emp_min else 0.0
        avg_minutes_per_task = round(emp_min / emp_tasks, 1) if emp_tasks else 0.0
        no_project_pct = round(no_project_minutes / emp_min * 100) if emp_min else 0
        logged_gap_hours = round((emp_min - attendance_minutes) / 60.0, 1)
        high_hour_low_output_days = 0
        for day_agg in by_emp_day.get(emp_id, {}).values():
            if day_agg["minutes"] >= 240 and day_agg["tasks"] <= 2:
                high_hour_low_output_days += 1
        very_long_logs = sum(
            1 for ev in events
            if _safe_minutes(ev.get("minutes")) >= 360
        )

        review_flags = []
        review_weight = 0

        def _add_flag(level, text, weight=1):
            nonlocal review_weight
            review_flags.append({"level": level, "text": text})
            review_weight += weight

        if emp_min > 0 and attendance_minutes == 0:
            _add_flag("rose", f"Logged {emp_min / 60.0:.1f}h with no completed check-in/out span", 2)
        elif attendance_minutes > 0 and emp_min > attendance_minutes + 90:
            _add_flag("amber", f"Logged {logged_gap_hours:.1f}h above attendance span", 2)
        elif attendance_minutes >= 240 and emp_min == 0:
            _add_flag("amber", f"Checked in {attendance_minutes / 60.0:.1f}h with no completed task logs", 1)
        elif attendance_minutes > emp_min + 180:
            _add_flag("amber", f"Attendance span exceeds task logs by {abs(logged_gap_hours):.1f}h", 1)
        if work_without_attendance_days:
            _add_flag("amber", f"Work logged on {len(work_without_attendance_days)} day(s) without check-in", 2)
        if attendance_summary["missing_checkout_days"]:
            _add_flag("amber", f"{len(attendance_summary['missing_checkout_days'])} missing checkout day(s)", 1)
        if completion_rate is not None and completion_rate < 60 and recurring_due >= 5:
            _add_flag("rose", f"Planned recurring completion is {completion_rate}%", 3)
        if no_project_minutes >= 180 and no_project_pct >= 30:
            _add_flag("amber", f"{no_project_minutes / 60.0:.1f}h unassigned to projects", 1)
        emp_outlier_count = len(outliers_by_emp.get(emp_id, []))
        if emp_outlier_count:
            _add_flag("amber", f"{emp_outlier_count} time outlier log(s)", 2)
        if very_long_logs and not emp_outlier_count:
            _add_flag("amber", f"{very_long_logs} single log(s) over 6h", 1)
        if emp_min >= 240 and tasks_per_hour < 0.75:
            _add_flag("amber", f"Low throughput: {tasks_per_hour:.2f} tasks/h", 1)
        if high_hour_low_output_days:
            _add_flag("amber", f"{high_hour_low_output_days} high-hour / low-output day(s)", 1)

        member_rows.append({
            "id": emp_id,
            "name": emp["name"],
            "department": emp["department"] or "—",
            "hours": round(emp_min / 60.0, 1),
            "minutes": emp_min,
            "tasks": emp_tasks,
            "tasks_delta": _delta(emp_tasks, prev_tasks),
            "active_days": emp_active_days,
            "attendance_hours": round(attendance_minutes / 60.0, 1),
            "attendance_minutes": attendance_minutes,
            "attendance_days": len(attendance_days),
            "leave_days": len(approved_leave_days),
            "logged_attendance_gap_hours": logged_gap_hours,
            "missing_checkout_days": len(attendance_summary["missing_checkout_days"]),
            "work_without_attendance_days": len(work_without_attendance_days),
            "tasks_per_hour": tasks_per_hour,
            "avg_minutes_per_task": avg_minutes_per_task,
            "no_project_hours": round(no_project_minutes / 60.0, 1),
            "no_project_pct": no_project_pct,
            "high_hour_low_output_days": high_hour_low_output_days,
            "time_outlier_count": emp_outlier_count,
            "review_flags": review_flags,
            "review_flag_count": len(review_flags),
            "review_weight": review_weight,
            "completion_rate": completion_rate,
            "recurring_due": recurring_due,
            "recurring_done": recurring_done,
            "sparkline": spark,
            "spark_max": max(spark) if spark else 0,
        })

    throughput_rates = [
        m["tasks_per_hour"] for m in member_rows
        if m["tasks_per_hour"] > 0 and m["hours"] >= 1
    ]
    median_tasks_per_hour = median(throughput_rates) if throughput_rates else 0
    target_active_days = max(1, min(days, 22))
    for m in member_rows:
        if m["recurring_due"]:
            plan_component = m["completion_rate"] or 0
        elif m["tasks"] > 0:
            plan_component = 70
        else:
            plan_component = 0
        if median_tasks_per_hour:
            throughput_component = min(100, round(m["tasks_per_hour"] / median_tasks_per_hour * 100))
        else:
            throughput_component = 0
        activity_component = min(100, round(m["active_days"] / target_active_days * 100))
        trust_penalty = min(45, m["review_weight"] * 7)
        score = round(max(0, min(100, (0.55 * plan_component) + (0.30 * throughput_component) + (0.15 * activity_component) - trust_penalty)))
        m["effectiveness_score"] = score
        m["effectiveness_level"] = "good" if score >= 80 else ("ok" if score >= 60 else "risk")

    # Sort by hours descending
    member_rows.sort(key=lambda m: m["hours"], reverse=True)

    review_queue = [
        m for m in member_rows
        if m["review_flag_count"] > 0
    ]
    review_queue.sort(
        key=lambda m: (m["review_weight"], m["logged_attendance_gap_hours"], m["minutes"]),
        reverse=True
    )
    review_queue = review_queue[:10]

    # ── Project breakdown (top 8) ───────────────────
    project_totals = {}
    for ev in work:
        pid = ev["project_id"] or 0
        if pid not in project_totals:
            project_totals[pid] = {"minutes": 0, "tasks": 0}
        project_totals[pid]["minutes"] += ev["minutes"] or 0
        project_totals[pid]["tasks"] += 1

    project_list = []
    for pid, agg in project_totals.items():
        if pid == 0:
            pname = "No Project"
            client_name = ""
        else:
            p = db.execute(
                "SELECT p.name, COALESCE(c.name,'') as client_name FROM projects p "
                "LEFT JOIN clients c ON p.client_id = c.id WHERE p.id=?",
                (pid,)
            ).fetchone()
            pname = p["name"] if p else f"Project #{pid}"
            client_name = p["client_name"] if p else ""
        project_list.append({
            "id": pid, "name": pname, "client": client_name,
            "hours": round(agg["minutes"] / 60.0, 1),
            "tasks": agg["tasks"],
        })
    project_list.sort(key=lambda p: p["hours"], reverse=True)
    project_list = project_list[:8]
    total_project_hours = sum(p["hours"] for p in project_list) or 1

    # ── Daily activity time-series ──────────────────
    daily_series = {d: {"hours": 0.0, "tasks": 0} for d in date_list}
    for ev in work:
        dt = ev["date"]
        if dt in daily_series:
            daily_series[dt]["hours"] += _safe_minutes(ev.get("minutes")) / 60.0
            daily_series[dt]["tasks"] += 1
    daily_attendance_series = {d: 0.0 for d in date_list}
    for summary in attendance_summary_by_emp.values():
        for dt, minutes in summary["day_spans"].items():
            if dt in daily_attendance_series:
                daily_attendance_series[dt] += minutes / 60.0
    daily_hours_list  = [round(daily_series[d]["hours"], 2) for d in date_list]
    daily_tasks_list  = [daily_series[d]["tasks"] for d in date_list]
    daily_attendance_list = [round(daily_attendance_series[d], 2) for d in date_list]

    total_logged_minutes = sum(m["minutes"] for m in member_rows)
    total_attendance_minutes = sum(m["attendance_minutes"] for m in member_rows)
    total_recurring_due = sum(m["recurring_due"] for m in member_rows)
    total_recurring_done = sum(m["recurring_done"] for m in member_rows)
    total_no_project_minutes = sum(m["no_project_hours"] * 60 for m in member_rows)
    team_plan_rate = round(total_recurring_done / total_recurring_due * 100) if total_recurring_due else None
    reality = {
        "attendance_hours": round(total_attendance_minutes / 60.0, 1),
        "attendance_days": sum(m["attendance_days"] for m in member_rows),
        "missing_checkout_days": sum(m["missing_checkout_days"] for m in member_rows),
        "logged_gap_hours": round((total_logged_minutes - total_attendance_minutes) / 60.0, 1),
        "logged_gap_class": "rose" if total_attendance_minutes == 0 and total_logged_minutes > 0 else (
            "amber" if total_logged_minutes > total_attendance_minutes + 120 else "sage"
        ),
        "plan_rate": team_plan_rate,
        "planned_done": total_recurring_done,
        "planned_due": total_recurring_due,
        "tasks_per_hour": round(now["tasks"] / now["hours"], 2) if now["hours"] else 0,
        "median_tasks_per_hour": round(median_tasks_per_hour, 2) if median_tasks_per_hour else 0,
        "no_project_hours": round(total_no_project_minutes / 60.0, 1),
        "no_project_pct": round(total_no_project_minutes / total_logged_minutes * 100) if total_logged_minutes else 0,
        "review_flags": sum(m["review_flag_count"] for m in member_rows),
        "flagged_members": len(review_queue),
        "time_outliers": len(time_outliers),
    }

    # ── Attention items ─────────────────────────────
    inactive = []
    low_completion = []
    now_date = get_tz_now().date()
    for m in member_rows:
        # Inactive if no work in last 3 days AND no holiday/weekend excuse
        last_3_days = [(now_date - timedelta(days=i)).isoformat() for i in range(3)]
        recent_activity = any(ev["date"] in last_3_days and ev["emp_id"] == m["id"] for ev in work)
        recent_leave = any(day in approved_leave_by_emp.get(m["id"], {}) for day in last_3_days)
        if not recent_activity and not recent_leave and m["active_days"] > 0:
            inactive.append(m)
        # Low completion rate
        if m["completion_rate"] is not None and m["completion_rate"] < 60 and m["recurring_due"] >= 5:
            low_completion.append(m)

    # Overdue one-off tasks
    overdue_count = db.execute(
        "SELECT COUNT(*) as cnt FROM oneoff_tasks "
        "WHERE completed = 0 AND created_at < date('now', '-7 days')"
    ).fetchone()["cnt"]

    return render_template(
        "kpi.html",
        employees=employees,
        emp_filter=emp_filter,
        days=days,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        prev_start_date=prev_start.isoformat(),
        prev_end_date=prev_end.isoformat(),
        kpis=kpis,
        member_rows=member_rows,
        project_list=project_list,
        total_project_hours=total_project_hours,
        date_list=date_list,
        daily_hours_list=daily_hours_list,
        daily_tasks_list=daily_tasks_list,
        daily_attendance_list=daily_attendance_list,
        reality=reality,
        review_queue=review_queue,
        time_outliers=time_outliers,
        inactive_members=inactive,
        low_completion_members=low_completion,
        overdue_count=overdue_count,
        today=now_date.isoformat(),
    )


# ── Admin: Daily View ───────────────────────────────────


@app.route("/admin/daily/<string:day_date>")
@admin_required
def daily_view(day_date):
    db = get_db()
    employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()
    emp_filter = request.args.get("employee", 0, type=int)

    params = (day_date, emp_filter) if emp_filter else (day_date,)
    emp_clause = "AND rt.employee_id = ?" if emp_filter else ""
    emp_clause2 = "AND ot.employee_id=?" if emp_filter else ""
    emp_clause3 = "AND ll.employee_id=?" if emp_filter else ""

    recurring = db.execute(f"""
        SELECT rc.*, rt.title, rt.description as task_desc, e.name as employee_name
        FROM recurring_completions rc
        JOIN recurring_tasks rt ON rc.task_id = rt.id
        JOIN employees e ON rt.employee_id = e.id
        WHERE rc.completion_date = ? {emp_clause}
        ORDER BY e.name, rt.title
    """, params).fetchall()

    oneoff = db.execute(f"""
        SELECT ot.*, e.name as employee_name
        FROM oneoff_tasks ot
        JOIN employees e ON ot.employee_id = e.id
        WHERE ot.completed=1 AND ot.completion_date=? {emp_clause2}
        ORDER BY e.name, ot.title
    """, params).fetchall()

    logins = db.execute(f"""
        SELECT ll.*, e.name as employee_name
        FROM login_log ll
        JOIN employees e ON ll.employee_id = e.id
        WHERE ll.login_date=? {emp_clause3}
        ORDER BY e.name
    """, params).fetchall()

    total_minutes = sum(r["time_minutes"] for r in recurring) + sum(
        o["time_minutes"] for o in oneoff if o["time_minutes"]
    )

    return render_template(
        "daily.html", employees=employees, emp_filter=emp_filter,
        day_date=day_date, recurring=recurring, oneoff=oneoff,
        logins=logins, total_minutes=total_minutes,
    )


# ── Admin: Activity Log ────────────────────────────────


@app.route("/admin/log")
@admin_required
def log_view():
    db = get_db()
    current_role = session.get("role", "employee")

    # Hierarchy filtering: admins see employee+subadmin logs, superadmins see all
    if current_role == "superadmin":
        employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()
    else:
        employees = db.execute("SELECT * FROM employees WHERE role IN ('employee','sales','accountant','subadmin','manager') ORDER BY name").fetchall()

    emp_filter = request.args.get("employee", 0, type=int)
    action_filter = request.args.get("action", "")

    where_clauses = []
    params = []

    # Admins can only see logs for employees and subadmins
    if current_role != "superadmin":
        visible_ids = [e["id"] for e in employees]
        if visible_ids:
            placeholders = ",".join("?" * len(visible_ids))
            where_clauses.append(f"al.employee_id IN ({placeholders})")
            params.extend(visible_ids)
        else:
            where_clauses.append("1=0")

    if emp_filter:
        where_clauses.append("al.employee_id = ?")
        params.append(emp_filter)
    if action_filter:
        where_clauses.append("al.action = ?")
        params.append(action_filter)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    entries = db.execute(f"""
        SELECT al.*, e.name as employee_name,
               COALESCE(a.name, e.name) as actor_name
        FROM activity_log al
        JOIN employees e ON al.employee_id = e.id
        LEFT JOIN employees a ON al.actor_id = a.id
        {where_sql}
        ORDER BY al.created_at DESC
        LIMIT 500
    """, params).fetchall()

    return render_template("log.html", employees=employees, emp_filter=emp_filter,
                           action_filter=action_filter, entries=entries, current_role=current_role)


# ── Password Changes ────────────────────────────────────


@app.route("/admin/change-password", methods=["POST"])
@admin_required
def admin_change_password():
    new_password = request.form.get("new_password", "").strip()
    if new_password and len(new_password) >= 4:
        db = get_db()
        db.execute("UPDATE employees SET password=? WHERE id=?", (hash_password(new_password), session["user_id"]))
        db.commit()
        flash("Password updated.", "success")
    else:
        flash("Password must be at least 4 characters.", "error")
    return redirect(url_for("admin_home"))


@app.route("/change-password", methods=["POST"])
@login_required
def change_password():
    new_password = request.form.get("new_password", "").strip()
    if new_password and len(new_password) >= 4:
        db = get_db()
        db.execute("UPDATE employees SET password=? WHERE id=?", (hash_password(new_password), session["user_id"]))
        db.commit()
        flash("Password updated.", "success")
    else:
        flash("Password must be at least 4 characters.", "error")
    return redirect(url_for("my_dashboard"))


# ── Telegram Notifications ──────────────────────────────


def send_telegram(chat_id, message, reply_markup=None):
    if not is_telegram_enabled() or not chat_id:
        return False
    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{get_telegram_bot_token()}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    try:
        urllib.request.urlopen(url, data, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error for {chat_id}: {e}")
        return False


def delete_telegram_message(chat_id, message_id):
    if not is_telegram_enabled() or not chat_id or not message_id:
        return False
    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{get_telegram_bot_token()}/deleteMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "message_id": message_id}).encode()
    try:
        urllib.request.urlopen(url, data, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram delete error for {chat_id}/{message_id}: {e}")
        return False


def send_email(to_email, subject, body_html):
    """Send email via SMTP. Fails silently if not configured."""
    if get_app_setting("smtp_enabled", "0") != "1":
        return
    host = get_app_setting("smtp_host", "")
    if not host or not to_email:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        port = int(get_app_setting("smtp_port", "587") or "587")
        user = get_app_setting("smtp_user", "")
        password = get_app_setting("smtp_password", "")
        from_email = get_app_setting("smtp_from", user)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(host, port) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_email, to_email, msg.as_string())
    except Exception as e:
        print(f"Email send error: {e}")


def create_notification(db_or_path, employee_id, message, type="info", link=""):
    """Create an in-app notification."""
    if isinstance(db_or_path, str):
        db = sqlite3.connect(db_or_path)
        db.execute("INSERT INTO notifications (employee_id, message, type, link) VALUES (?,?,?,?)",
                   (employee_id, message, type, link))
        db.commit()
        db.close()
    else:
        db_or_path.execute("INSERT INTO notifications (employee_id, message, type, link) VALUES (?,?,?,?)",
                          (employee_id, message, type, link))


def send_telegram_file(chat_id, file_bytes, filename, caption=""):
    if not is_telegram_enabled() or not chat_id:
        return
    import urllib.request
    boundary = "----FormBoundary" + secrets.token_hex(8)
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/pdf\r\n\r\n"
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{get_telegram_bot_token()}/sendDocument"
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"Telegram file send error: {e}")


def send_telegram_photo(chat_id, image_bytes, filename="report.png", caption=""):
    if not is_telegram_enabled() or not chat_id or not image_bytes:
        return False
    import urllib.request
    boundary = "----FormBoundary" + secrets.token_hex(8)
    body = b""
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: image/png\r\n\r\n"
    body += image_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{get_telegram_bot_token()}/sendPhoto"
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"Telegram photo send error: {e}")
        return False


def is_off_day(db, today):
    """Check if today is a holiday or weekly off day. Returns reason str or None."""
    info = is_non_working_date(db, today)
    if info["is_off"]:
        if db.execute("SELECT 1 FROM holidays WHERE holiday_date=?", (today,)).fetchone():
            return f"holiday: {info['reason']}"
        return f"weekly off: {info['reason']}"
    return None


def send_absentees_report():
    """Send daily absentees list to Telegram group, grouped by shift.
    Skipped on holidays and configured weekly off days."""
    if not is_telegram_enabled():
        return
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()
    # Skip on off days
    off_reason = is_off_day(db, today)
    if off_reason:
        db.close()
        print(f"[absentee report] skipped — {off_reason}")
        return
    employees = db.execute("""
        SELECT id, name, shift_id
        FROM employees
        WHERE role IN ('employee','sales','accountant','subadmin','manager')
          AND lower(COALESCE(name, '')) != 'test employee'
          AND lower(COALESCE(username, '')) NOT LIKE 'testuser_%'
        ORDER BY name
    """).fetchall()
    shifts = db.execute("SELECT * FROM shifts ORDER BY start_time").fetchall()
    shift_map = {s["id"]: s["name"] for s in shifts}
    present_ids = set()
    for row in db.execute("SELECT DISTINCT employee_id FROM attendance WHERE action_date=? AND action='checkin'", (today,)).fetchall():
        present_ids.add(row["employee_id"])
    leave_today = approved_leave_map(db, [e["id"] for e in employees], today, today)
    on_leave = [e for e in employees if e["id"] not in present_ids and leave_today.get(e["id"], {}).get(today)]
    absent = [e for e in employees if e["id"] not in present_ids and not leave_today.get(e["id"], {}).get(today)]
    present = [e for e in employees if e["id"] in present_ids]
    msg = f"<b>Attendance Report — {today}</b>\n\n"
    msg += f"Present: {len(present)} | Leave: {len(on_leave)} | Absent: {len(absent)} | Total: {len(employees)}\n\n"
    if on_leave:
        msg += "<b>Approved Leave Today:</b>\n"
        for i, e in enumerate(on_leave, 1):
            leave_row = leave_today.get(e["id"], {}).get(today)
            msg += f"  {i}. {e['name']} ({leave_row['type_name']})\n"
        msg += "\n"
    if absent:
        msg += "<b>Absent Today:</b>\n"
        # Group by shift
        from collections import defaultdict
        shift_groups = defaultdict(list)
        for e in absent:
            sid = e["shift_id"] if "shift_id" in e.keys() else 0
            shift_name = shift_map.get(sid, "General")
            shift_groups[shift_name].append(e["name"])
        for sname, names in sorted(shift_groups.items()):
            if len(shift_map) > 1:
                msg += f"\n<b>[{sname} Shift]</b>\n"
            for i, name in enumerate(names, 1):
                msg += f"  {i}. {name}\n"
    else:
        msg += "All team members checked in!"
    send_telegram_group(msg, category="attendance_event")
    db.close()


def send_month_end_attendance_summary():
    """Send completed-month attendance summary to a dedicated attendance chat."""
    if not is_telegram_enabled():
        return
    if get_app_setting("monthly_attendance_enabled", "0") != "1":
        print("[monthly attendance] skipped — disabled")
        return
    chat_id = get_app_setting("monthly_attendance_chat_id", "").strip()
    if not chat_id:
        print("[monthly attendance] skipped — monthly_attendance_chat_id not configured")
        return
    db = open_db()
    db.row_factory = sqlite3.Row
    run_date = get_tz_now().date()
    report_month_end = run_date.replace(day=1) - timedelta(days=1)
    year, month = report_month_end.year, report_month_end.month
    import calendar as _cal
    days_in_month = _cal.monthrange(year, month)[1]
    month_name = _cal.month_name[month]
    weekly_off = get_app_setting("weekly_off_days", "6")
    try:
        off_days = {int(x.strip()) for x in weekly_off.split(",") if x.strip()}
    except ValueError:
        off_days = {6}
    holiday_dates = {
        row["holiday_date"] for row in db.execute(
            "SELECT holiday_date FROM holidays WHERE holiday_date LIKE ?",
            (f"{year}-{month:02d}-%",)
        ).fetchall()
    }
    working_dates = []
    for day in range(1, days_in_month + 1):
        day_date = date(year, month, day)
        day_str = day_date.isoformat()
        if day_date.weekday() in off_days or day_str in holiday_dates:
            continue
        working_dates.append(day_str)

    employees = db.execute("""
        SELECT id, name, COALESCE(joining_date, '') as joining_date
        FROM employees
        WHERE role IN ('employee','sales','accountant','subadmin','manager')
          AND lower(COALESCE(name, '')) != 'test employee'
          AND lower(COALESCE(username, '')) NOT LIKE 'testuser_%'
        ORDER BY name
    """).fetchall()
    leave_by_emp = approved_leave_map(
        db, [emp["id"] for emp in employees],
        f"{year}-{month:02d}-01",
        f"{year}-{month:02d}-{days_in_month:02d}",
    )
    msg = f"<b>Monthly Attendance Summary — {month_name} {year}</b>\n"
    msg += f"Working days counted: {len(working_dates)}\n\n"
    for emp in employees:
        expected_dates = list(working_dates)
        if emp["joining_date"]:
            expected_dates = [d for d in expected_dates if d >= emp["joining_date"]]
        expected_count = len(expected_dates)
        present_dates = set()
        if expected_count:
            placeholders = ",".join("?" for _ in expected_dates)
            present_rows = db.execute(
                f"""SELECT COUNT(DISTINCT action_date) as cnt
                    FROM attendance
                    WHERE employee_id=? AND action='checkin' AND action_date IN ({placeholders})""",
                [emp["id"], *expected_dates]
            ).fetchone()
            days_present = present_rows["cnt"]
            present_date_rows = db.execute(
                f"""SELECT DISTINCT action_date
                    FROM attendance
                    WHERE employee_id=? AND action='checkin' AND action_date IN ({placeholders})""",
                [emp["id"], *expected_dates]
            ).fetchall()
            present_dates = {row["action_date"] for row in present_date_rows}
        else:
            days_present = 0
        leave_dates = {
            d for d in expected_dates
            if leave_by_emp.get(emp["id"], {}).get(d) and d not in present_dates
        }
        days_leave = len(leave_dates)
        days_absent = max(expected_count - days_present - days_leave, 0)
        pct = round(days_present / expected_count * 100) if expected_count else 0
        msg += f"<b>{emp['name']}</b>: {days_present}/{expected_count} working days ({pct}%)"
        if days_leave > 0:
            msg += f" — {days_leave} leave"
        if days_absent > 0:
            msg += f" — {days_absent} absent"
        msg += "\n"
    send_telegram(chat_id, msg)
    db.close()


def _format_duration_minutes(value):
    try:
        minutes = int(round(float(value or 0)))
    except (TypeError, ValueError):
        minutes = 0
    minutes = max(minutes, 0)
    hours = minutes // 60
    mins = minutes % 60
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _short_text(value, limit=60):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _attendance_span_summary(rows):
    first_checkin = ""
    last_checkout = ""
    open_since = ""
    span_minutes = 0
    current_checkin = None
    has_checkin = False
    for row in rows:
        action = row["action"]
        action_time = row["action_time"] or ""
        action_at = _attendance_datetime(row["action_date"], action_time)
        if action == "checkin":
            has_checkin = True
            if not first_checkin:
                first_checkin = action_time[:5]
            current_checkin = action_at
            open_since = action_time[:5]
        elif action == "checkout":
            if action_time:
                last_checkout = action_time[:5]
            if current_checkin and action_at and action_at >= current_checkin:
                span_minutes += int((action_at - current_checkin).total_seconds() // 60)
            current_checkin = None
            open_since = ""
    if has_checkin:
        if last_checkout:
            label = f"{first_checkin or '-'}-{last_checkout} ({_format_duration_minutes(span_minutes)})"
        elif open_since:
            label = f"checked in {open_since}; no checkout"
        else:
            label = "checked in"
    else:
        label = "no check-in"
    return {
        "has_checkin": has_checkin,
        "span_minutes": span_minutes,
        "open_since": open_since,
        "label": label,
    }


def _telegram_message_chunks(message, limit=3600):
    chunks = []
    current = ""
    for line in (message or "").splitlines():
        next_line = line if not current else current + "\n" + line
        if len(next_line) <= limit:
            current = next_line
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks or [message or ""]


def build_daily_staff_activity_report(db, report_date=None):
    """Build yesterday-style staff activity report for HR Telegram."""
    import html as _html
    target_day = parse_iso_date(report_date) if report_date else (get_tz_now().date() - timedelta(days=1))
    if not target_day:
        target_day = get_tz_now().date() - timedelta(days=1)
    day_str = target_day.isoformat()
    off_info = is_non_working_date(db, day_str)

    employees = db.execute("""
        SELECT e.id, e.name, e.role, COALESCE(e.department, '') as department,
               COALESCE(e.joining_date, '') as joining_date,
               COALESCE(s.name, '') as shift_name
        FROM employees e
        LEFT JOIN shifts s ON s.id = e.shift_id
        WHERE e.role IN ('employee','sales','accountant','subadmin','manager')
          AND lower(COALESCE(e.name, '')) != 'test employee'
          AND lower(COALESCE(e.username, '')) NOT LIKE 'testuser_%'
        ORDER BY e.name
    """).fetchall()
    employee_ids = [emp["id"] for emp in employees]
    if not employee_ids:
        return {
            "report_date": day_str,
            "message": f"<b>Daily Staff Activity Report - {_html.escape(day_str)}</b>\n\nNo staff accounts found.",
            "has_activity": False,
            "skip_reason": "no staff accounts",
            "summaries": [],
            "totals": {"present": 0, "leave": 0, "no_checkin": 0, "tasks": 0, "task_minutes": 0},
            "needs_review": [],
        }

    placeholders = ",".join("?" for _ in employee_ids)
    attendance_by_emp = {emp_id: [] for emp_id in employee_ids}
    for row in db.execute(f"""
        SELECT *
        FROM attendance
        WHERE action_date=? AND employee_id IN ({placeholders})
        ORDER BY employee_id, action_date, action_time, id
    """, [day_str, *employee_ids]).fetchall():
        attendance_by_emp.setdefault(row["employee_id"], []).append(row)

    recurring_by_emp = {emp_id: [] for emp_id in employee_ids}
    completed_recurring_ids = set()
    for row in db.execute(f"""
        SELECT rc.task_id, rc.time_minutes, rc.notes,
               rt.employee_id, rt.title, rt.project_id,
               COALESCE(p.name, 'No Project') as project_name
        FROM recurring_completions rc
        JOIN recurring_tasks rt ON rt.id = rc.task_id
        LEFT JOIN projects p ON p.id = rt.project_id
        WHERE rc.completion_date=? AND rt.employee_id IN ({placeholders})
        ORDER BY rt.employee_id, rc.id
    """, [day_str, *employee_ids]).fetchall():
        recurring_by_emp.setdefault(row["employee_id"], []).append(row)
        completed_recurring_ids.add(row["task_id"])

    oneoff_by_emp = {emp_id: [] for emp_id in employee_ids}
    for row in db.execute(f"""
        SELECT ot.id, ot.employee_id, ot.title, ot.time_minutes, ot.notes,
               COALESCE(p.name, 'No Project') as project_name
        FROM oneoff_tasks ot
        LEFT JOIN projects p ON p.id = ot.project_id
        WHERE ot.completed=1 AND ot.completion_date=? AND ot.employee_id IN ({placeholders})
        ORDER BY ot.employee_id, ot.id
    """, [day_str, *employee_ids]).fetchall():
        oneoff_by_emp.setdefault(row["employee_id"], []).append(row)

    recurring_tasks_by_emp = {emp_id: [] for emp_id in employee_ids}
    for task in db.execute(f"""
        SELECT rt.*, COALESCE(p.name, 'No Project') as project_name
        FROM recurring_tasks rt
        LEFT JOIN projects p ON p.id = rt.project_id
        WHERE rt.active=1 AND rt.employee_id IN ({placeholders})
        ORDER BY rt.employee_id, rt.title
    """, employee_ids).fetchall():
        recurring_tasks_by_emp.setdefault(task["employee_id"], []).append(task)

    leave_by_emp = approved_leave_map(db, employee_ids, day_str, day_str)

    summaries = []
    needs_review = []
    total_tasks = 0
    total_task_minutes = 0
    present_count = 0
    leave_count = 0
    no_checkin_count = 0
    has_any_activity = False

    for emp in employees:
        emp_id = emp["id"]
        attendance = _attendance_span_summary(attendance_by_emp.get(emp_id, []))
        leave_row = leave_by_emp.get(emp_id, {}).get(day_str)
        if attendance["has_checkin"]:
            present_count += 1
            has_any_activity = True
        elif leave_row:
            leave_count += 1
            has_any_activity = True
        else:
            no_checkin_count += 1

        due_tasks = []
        for task in recurring_tasks_by_emp.get(emp_id, []):
            if task_due_today(
                task["frequency"] or "daily",
                task["frequency_day"] or 0,
                target_day,
                task["frequency_month"] or 0,
                task["frequency_days"] if "frequency_days" in task.keys() else "",
            ):
                due_tasks.append(task)
        due_count = len(due_tasks)
        due_done = sum(1 for task in due_tasks if task["id"] in completed_recurring_ids)
        due_ids = {task["id"] for task in due_tasks}
        extra_recurring = sum(1 for row in recurring_by_emp.get(emp_id, []) if row["task_id"] not in due_ids)
        missed_due = [task for task in due_tasks if task["id"] not in completed_recurring_ids]

        work_items = []
        for row in recurring_by_emp.get(emp_id, []):
            work_items.append({
                "title": row["title"],
                "project_name": row["project_name"],
                "minutes": float(row["time_minutes"] or 0),
                "kind": "recurring",
            })
        for row in oneoff_by_emp.get(emp_id, []):
            work_items.append({
                "title": row["title"],
                "project_name": row["project_name"],
                "minutes": float(row["time_minutes"] or 0),
                "kind": "one-time",
            })
        task_count = len(work_items)
        task_minutes = sum(item["minutes"] for item in work_items)
        total_tasks += task_count
        total_task_minutes += task_minutes
        if task_count:
            has_any_activity = True

        project_totals = {}
        for item in work_items:
            bucket = project_totals.setdefault(item["project_name"] or "No Project", {"count": 0, "minutes": 0})
            bucket["count"] += 1
            bucket["minutes"] += item["minutes"]
        project_parts = []
        for project_name, data in sorted(project_totals.items(), key=lambda kv: (-kv[1]["minutes"], kv[0]))[:3]:
            project_parts.append(
                f"{_short_text(project_name, 26)}: {data['count']} task(s), {_format_duration_minutes(data['minutes'])}"
            )
        work_summary = "; ".join(project_parts) if project_parts else "-"
        sample_titles = "; ".join(_short_text(item["title"], 42) for item in sorted(work_items, key=lambda x: -x["minutes"])[:3])

        issues = []
        if leave_row and not attendance["has_checkin"] and task_count == 0:
            status_label = f"approved leave ({leave_row['type_name']})"
        elif not attendance["has_checkin"] and task_count:
            status_label = "work logged without check-in"
            issues.append("work logged without check-in")
        elif not attendance["has_checkin"]:
            status_label = "no check-in"
            issues.append("no check-in")
        else:
            status_label = "present"

        if attendance["open_since"]:
            issues.append(f"checkout missing since {attendance['open_since']}")
        if due_count and due_done < due_count:
            issues.append(f"{due_count - due_done} planned recurring task(s) missed")
        if attendance["span_minutes"] >= 360 and task_minutes > 0:
            work_ratio = task_minutes / attendance["span_minutes"]
            if work_ratio < 0.35:
                issues.append("task time low vs attendance span")
            elif work_ratio > 1.15:
                issues.append("task time exceeds attendance span")
        if task_count and task_minutes:
            avg_minutes = task_minutes / task_count
            if avg_minutes >= 180:
                issues.append("high average time per task")
            elif avg_minutes < 5 and task_count >= 5:
                issues.append("many tasks with very low logged time")
        if not issues:
            if due_count and due_done == due_count:
                issues.append("planned work completed")
            elif task_count:
                issues.append("activity looks reasonable")
            elif leave_row:
                issues.append("approved leave")
            else:
                issues.append("no task activity")

        actionable_issues = [
            item for item in issues
            if item not in ("planned work completed", "activity looks reasonable", "approved leave")
        ]
        if actionable_issues:
            needs_review.append(f"{emp['name']}: " + "; ".join(actionable_issues[:2]))
        if any("no check-in" in item or "checkout missing" in item or "without check-in" in item for item in actionable_issues):
            severity = "danger"
        elif actionable_issues:
            severity = "warning"
        else:
            severity = "success"

        planned_label = f"{due_done}/{due_count}"
        if due_count:
            planned_label += f" ({round((due_done / due_count) * 100)}%)"
            planned_image_label = f"{due_done}/{due_count} done"
        else:
            planned_label = "none due"
            planned_image_label = "none due"
        if extra_recurring:
            planned_image_label += f"\n+{extra_recurring} extra recurring"

        summaries.append({
            "name": emp["name"],
            "department": emp["department"] or emp["role"].title(),
            "attendance": attendance["label"],
            "status": status_label,
            "task_count": task_count,
            "task_minutes": task_minutes,
            "planned": planned_label,
            "planned_image": planned_image_label,
            "work_summary": work_summary,
            "sample_titles": sample_titles,
            "main_work": work_summary if not sample_titles else f"{work_summary}. Examples: {sample_titles}",
            "review": "; ".join(issues[:3]),
            "missed_titles": ", ".join(_short_text(task["title"], 28) for task in missed_due[:3]),
            "severity": severity,
        })

    if off_info["is_off"] and not has_any_activity:
        return {
            "report_date": day_str,
            "message": "",
            "has_activity": False,
            "skip_reason": f"non-working day with no activity: {off_info['reason']}",
            "summaries": summaries,
            "totals": {
                "present": present_count,
                "leave": leave_count,
                "no_checkin": no_checkin_count,
                "tasks": total_tasks,
                "task_minutes": total_task_minutes,
            },
            "needs_review": needs_review,
        }

    lines = [
        f"<b>Daily Staff Activity Report - {_html.escape(day_str)}</b>",
        f"Present: {present_count} | Leave: {leave_count} | No check-in: {no_checkin_count}",
        f"Tasks completed: {total_tasks} | Task time: {_format_duration_minutes(total_task_minutes)}",
    ]
    if off_info["is_off"]:
        lines.append(f"Calendar: non-working day ({_html.escape(off_info['reason'])})")
    if needs_review:
        lines.extend(["", "<b>Needs Review</b>"])
        for item in needs_review[:8]:
            lines.append(f"- {_html.escape(item)}")
    lines.append("")
    lines.append("<b>Staff Details</b>")
    for summary in summaries:
        lines.append("")
        lines.append(f"<b>{_html.escape(summary['name'])}</b> - {_html.escape(summary['department'])}")
        lines.append(
            f"Attendance: {_html.escape(summary['attendance'])} | Status: {_html.escape(summary['status'])}"
        )
        lines.append(
            f"Tasks: {summary['task_count']} | Task time: {_format_duration_minutes(summary['task_minutes'])} | Planned: {_html.escape(summary['planned'])}"
        )
        lines.append(f"Work: {_html.escape(summary['work_summary'])}")
        if summary["sample_titles"]:
            lines.append(f"Examples: {_html.escape(summary['sample_titles'])}")
        if summary["missed_titles"]:
            lines.append(f"Missed planned: {_html.escape(summary['missed_titles'])}")
        lines.append(f"Review: {_html.escape(summary['review'])}")
    return {
        "report_date": day_str,
        "message": "\n".join(lines),
        "has_activity": has_any_activity,
        "skip_reason": "",
        "summaries": summaries,
        "totals": {
            "present": present_count,
            "leave": leave_count,
            "no_checkin": no_checkin_count,
            "tasks": total_tasks,
            "task_minutes": total_task_minutes,
        },
        "needs_review": needs_review,
        "off_info": off_info,
    }


def _report_font(size, bold=False):
    try:
        from PIL import ImageFont as _ImageFont
        font_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        candidates = [
            f"/usr/share/fonts/truetype/dejavu/{font_name}",
            f"/Library/Fonts/{font_name}",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return _ImageFont.truetype(path, size)
    except Exception:
        pass
    try:
        from PIL import ImageFont as _ImageFont
        return _ImageFont.load_default()
    except Exception:
        return None


def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), str(text or ""), font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _line_height(draw, font, line_gap=8):
    return _text_size(draw, "Ag", font)[1] + line_gap


def _wrap_text_lines(draw, text, font, max_width):
    output = []
    for paragraph in str(text or "").split("\n"):
        words = paragraph.split()
        if not words:
            output.append("")
            continue
        current = ""
        for word in words:
            test = word if not current else f"{current} {word}"
            if _text_size(draw, test, font)[0] <= max_width or not current:
                current = test
            else:
                output.append(current)
                current = word
        if current:
            output.append(current)
    return output or [""]


def _fit_wrapped_lines(draw, text, font, max_width, max_lines=None):
    lines = _wrap_text_lines(draw, text, font, max_width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        while last and _text_size(draw, last + "...", font)[0] > max_width:
            last = last[:-1].rstrip()
        lines[-1] = (last + "...") if last else "..."
    return lines


def _wrapped_text_height(draw, text, font, max_width, max_lines=None, line_gap=8):
    lines = _fit_wrapped_lines(draw, text, font, max_width, max_lines)
    return max(1, len(lines)) * _line_height(draw, font, line_gap)


def _draw_wrapped_text(draw, text, xy, font, fill, max_width, max_lines=None, line_gap=8):
    x, y = xy
    lines = _fit_wrapped_lines(draw, text, font, max_width, max_lines)
    step = _line_height(draw, font, line_gap)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += step
    return y


def _attendance_image_text(attendance_label):
    text = str(attendance_label or "-")
    if " (" in text and text.endswith(")"):
        span, duration = text.rsplit(" (", 1)
        return f"{span}\n~{duration[:-1]}"
    return text


def render_daily_staff_activity_image(report):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        print(f"Daily staff report image unavailable: {e}")
        return None

    summaries = report.get("summaries") or []
    totals = report.get("totals") or {}
    report_date = report.get("report_date") or ""

    title_font = _report_font(58, True)
    subtitle_font = _report_font(30, False)
    header_font = _report_font(30, True)
    body_font = _report_font(29, False)
    body_bold = _report_font(29, True)
    name_font = _report_font(29, True)
    small_font = _report_font(24, False)

    margin = 28
    table_x = 28
    title_h = 145
    header_h = 88
    pad_x = 30
    pad_y = 30
    columns = [
        ("Staff", 270),
        ("Attendance", 350),
        ("Task Logs", 290),
        ("Planned Recurring", 330),
        ("Main Work Logged", 690),
        ("Review", 620),
    ]
    table_w = sum(width for _, width in columns)
    image_w = table_w + table_x * 2
    measure = Image.new("RGB", (image_w, 100), "white")
    draw = ImageDraw.Draw(measure)

    row_heights = []
    for item in summaries:
        texts = [
            item.get("name", ""),
            _attendance_image_text(item.get("attendance", "")),
            f"{item.get('task_count', 0)} task(s)\n{_format_duration_minutes(item.get('task_minutes', 0))}",
            item.get("planned_image") or item.get("planned", ""),
            item.get("main_work") or item.get("work_summary", "-"),
            item.get("review", ""),
        ]
        fonts = [name_font, body_font, body_bold, body_bold, body_font, body_font]
        max_lines = [3, 3, 3, 4, 7, 6]
        heights = []
        for idx, text in enumerate(texts):
            cell_w = columns[idx][1] - (pad_x * 2)
            heights.append(_wrapped_text_height(draw, text, fonts[idx], cell_w, max_lines[idx]) + (pad_y * 2))
        row_heights.append(max(170, *heights))

    image_h = title_h + header_h + sum(row_heights) + margin
    image = Image.new("RGB", (image_w, image_h), "#f8fafc")
    draw = ImageDraw.Draw(image)

    navy = "#111827"
    text = "#1f2937"
    muted = "#6b7280"
    border = "#e5e7eb"
    blue = "#2f55a4"
    success_bg = "#e8f6ee"
    warning_bg = "#fff3d6"
    danger_bg = "#fdecea"

    draw.rounded_rectangle((margin, 8, image_w - margin, title_h - 18), radius=18, fill="#ffffff", outline=border)
    draw.text((margin + 40, 34), "Staff Activity Summary", font=title_font, fill="#111827")
    subtitle = (
        f"Yesterday: {report_date}  |  Source: live Task Manager records  |  "
        f"Present: {totals.get('present', 0)}  Tasks: {totals.get('tasks', 0)}  "
        f"Logged: {_format_duration_minutes(totals.get('task_minutes', 0))}"
    )
    draw.text((margin + 40, 102), subtitle, font=subtitle_font, fill=muted)

    y = title_h
    x = table_x
    draw.rectangle((table_x, y, table_x + table_w, y + header_h), fill=navy)
    for label, width in columns:
        draw.text((x + pad_x, y + 27), label, font=header_font, fill="#ffffff")
        x += width
    y += header_h

    review_bg = {"success": success_bg, "warning": warning_bg, "danger": danger_bg}
    for row_idx, item in enumerate(summaries):
        row_h = row_heights[row_idx]
        base_bg = "#ffffff" if row_idx % 2 == 0 else "#f9fafb"
        x = table_x
        cells = [
            item.get("name", ""),
            _attendance_image_text(item.get("attendance", "")),
            f"{item.get('task_count', 0)} task(s)\n{_format_duration_minutes(item.get('task_minutes', 0))}",
            item.get("planned_image") or item.get("planned", ""),
            item.get("main_work") or item.get("work_summary", "-"),
            item.get("review", ""),
        ]
        fonts = [name_font, body_font, body_bold, body_bold, body_font, body_font]
        fills = [blue, text, navy, navy, text, text]
        max_lines = [3, 3, 3, 4, 7, 6]
        for col_idx, (_, width) in enumerate(columns):
            fill_bg = review_bg.get(item.get("severity"), warning_bg) if col_idx == 5 else base_bg
            draw.rectangle((x, y, x + width, y + row_h), fill=fill_bg, outline=border)
            _draw_wrapped_text(
                draw,
                cells[col_idx],
                (x + pad_x, y + pad_y),
                fonts[col_idx],
                fills[col_idx],
                width - (pad_x * 2),
                max_lines[col_idx],
            )
            x += width
        y += row_h

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def send_daily_staff_activity_report(report_date=None, force=False):
    """Send the daily HR staff activity report to the dedicated HR chat only."""
    if not is_telegram_enabled():
        return False
    if get_app_setting("daily_staff_report_enabled", "0") != "1":
        print("[daily staff report] skipped - disabled")
        return False
    chat_id = (
        get_app_setting("daily_staff_report_chat_id", "").strip()
        or get_app_setting("monthly_attendance_chat_id", "").strip()
    )
    if not chat_id:
        print("[daily staff report] skipped - no HR chat configured")
        return False

    db = open_db()
    db.row_factory = sqlite3.Row
    try:
        report = build_daily_staff_activity_report(db, report_date)
        report_day = report["report_date"]
        if report.get("skip_reason"):
            print(f"[daily staff report] skipped - {report['skip_reason']}")
            return False
        if not force:
            sent = db.execute(
                "SELECT 1 FROM daily_staff_reports WHERE report_date=? AND status='sent'",
                (report_day,),
            ).fetchone()
            if sent:
                print(f"[daily staff report] skipped - already sent for {report_day}")
                return False

        image_bytes = render_daily_staff_activity_image(report)
        ok = False
        if image_bytes:
            ok = send_telegram_photo(
                chat_id,
                image_bytes,
                f"daily-staff-report-{report_day}.png",
                f"Daily Staff Activity Report - {report_day}",
            )
        if not ok:
            for chunk in _telegram_message_chunks(report["message"]):
                if not send_telegram(chat_id, chunk):
                    ok = False
                    break
                ok = True
        db.execute("""
            INSERT OR REPLACE INTO daily_staff_reports (report_date, chat_id, status, error, sent_at)
            VALUES (?,?,?,?,datetime('now','localtime'))
        """, (report_day, chat_id, "sent" if ok else "failed", "" if ok else "Telegram image/text send failed"))
        db.commit()
        return ok
    except Exception as e:
        report_day = report_date or (get_tz_now().date() - timedelta(days=1)).isoformat()
        db.execute("""
            INSERT OR REPLACE INTO daily_staff_reports (report_date, chat_id, status, error, sent_at)
            VALUES (?,?,?,?,datetime('now','localtime'))
        """, (report_day, chat_id, "failed", str(e)))
        db.commit()
        print(f"daily staff report error: {e}")
        return False
    finally:
        db.close()


def send_daily_reminders():
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()
    off_reason = is_off_day(db, today)
    if off_reason:
        print(f"[daily reminders] skipped — {off_reason}")
        db.close()
        return
    employees = db.execute("SELECT * FROM employees WHERE telegram_chat_id != '' AND role IN ('employee','sales','subadmin')").fetchall()

    for emp in employees:
        recurring = db.execute(
            "SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1 ORDER BY title",
            (emp["id"],),
        ).fetchall()
        pending_oneoff = db.execute(
            "SELECT * FROM oneoff_tasks WHERE employee_id=? AND completed=0 ORDER BY created_at",
            (emp["id"],),
        ).fetchall()
        if not recurring and not pending_oneoff:
            continue
        msg = f"<b>Good morning, {emp['name']}!</b>\n\n"
        if recurring:
            msg += "<b>Daily Recurring Tasks:</b>\n"
            for i, t in enumerate(recurring, 1):
                msg += f"  {i}. {t['title']}\n"
            msg += "\n"
        if pending_oneoff:
            msg += "<b>Pending One-Time Tasks:</b>\n"
            for i, t in enumerate(pending_oneoff, 1):
                msg += f"  {i}. {t['title']}\n"
            msg += "\n"
        msg += "Please log in and complete your tasks today!"
        send_telegram(emp["telegram_chat_id"], msg)
    db.close()


def send_evening_reminders():
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()
    off_reason = is_off_day(db, today)
    if off_reason:
        print(f"[evening reminders] skipped — {off_reason}")
        db.close()
        return
    employees = db.execute("SELECT * FROM employees WHERE telegram_chat_id != '' AND role IN ('employee','sales','subadmin')").fetchall()

    for emp in employees:
        recurring = db.execute(
            "SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1",
            (emp["id"],),
        ).fetchall()
        incomplete = []
        for t in recurring:
            comp = db.execute(
                "SELECT id FROM recurring_completions WHERE task_id=? AND completion_date=?",
                (t["id"], today),
            ).fetchone()
            if not comp:
                incomplete.append(t["title"])
        pending_oneoff = db.execute(
            "SELECT title FROM oneoff_tasks WHERE employee_id=? AND completed=0",
            (emp["id"],),
        ).fetchall()
        if not incomplete and not pending_oneoff:
            continue
        msg = f"<b>Reminder, {emp['name']}!</b>\n\n"
        if incomplete:
            msg += "<b>Incomplete recurring tasks today:</b>\n"
            for i, title in enumerate(incomplete, 1):
                msg += f"  {i}. {title}\n"
            msg += "\n"
        if pending_oneoff:
            msg += "<b>Pending one-time tasks:</b>\n"
            for i, t in enumerate(pending_oneoff, 1):
                msg += f"  {i}. {t['title']}\n"
        msg += "\nPlease complete and log your tasks!"
        send_telegram(emp["telegram_chat_id"], msg)
    db.close()


def _attendance_datetime(action_date, action_time):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{action_date} {action_time}", fmt).replace(tzinfo=get_tz())
        except (TypeError, ValueError):
            continue
    return None


def send_checkout_reminders():
    """Remind employees and General group when checkout is pending after 10 hours."""
    db = open_db()
    db.row_factory = sqlite3.Row
    now = get_tz_now()
    cutoff_date = (now.date() - timedelta(days=2)).isoformat()
    try:
        open_checkins = db.execute("""
            SELECT a.id as attendance_id, a.employee_id, a.action_date, a.action_time,
                   e.name, e.telegram_chat_id
            FROM attendance a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.action = 'checkin'
              AND a.action_date >= ?
              AND e.role IN ('employee','sales','accountant','subadmin','manager')
              AND lower(COALESCE(e.name, '')) != 'test employee'
              AND lower(COALESCE(e.username, '')) NOT LIKE 'testuser_%'
            ORDER BY a.action_date, a.action_time
        """, (cutoff_date,)).fetchall()

        for row in open_checkins:
            last = db.execute("""
                SELECT id, action
                FROM attendance
                WHERE employee_id=?
                ORDER BY action_date DESC, action_time DESC, id DESC
                LIMIT 1
            """, (row["employee_id"],)).fetchone()
            if not last or last["id"] != row["attendance_id"] or last["action"] != "checkin":
                continue

            checkin_at = _attendance_datetime(row["action_date"], row["action_time"])
            if not checkin_at:
                continue
            elapsed = now - checkin_at
            if elapsed < timedelta(hours=10):
                continue

            already_sent = db.execute(
                "SELECT 1 FROM attendance_checkout_reminders WHERE attendance_id=?",
                (row["attendance_id"],),
            ).fetchone()
            if already_sent:
                continue

            hours = round(elapsed.total_seconds() / 3600, 1)
            msg = (
                f"<b>Checkout reminder</b>\n\n"
                f"Hi {row['name']}, you have been checked in for about <b>{hours} hours</b> "
                f"since {row['action_time']} on {row['action_date']}.\n\n"
                f"If your workday is done, please open Task Manager and check out."
            )
            private_sent = send_telegram(row["telegram_chat_id"], msg) if row["telegram_chat_id"] else False
            group_msg = (
                f"<b>Checkout pending</b>\n\n"
                f"{row['name']} has been checked in for about <b>{hours} hours</b> "
                f"since {row['action_time']} on {row['action_date']}."
            )
            group_sent = send_telegram_group(group_msg, category="attendance_event")
            if private_sent or group_sent:
                db.execute("""
                    INSERT OR IGNORE INTO attendance_checkout_reminders
                    (attendance_id, employee_id, action_date)
                    VALUES (?,?,?)
                """, (row["attendance_id"], row["employee_id"], row["action_date"]))
                db.commit()
    except Exception as e:
        print(f"checkout reminder error: {e}")
    finally:
        db.close()


def send_late_checkin_reminders():
    """Remind one hour after shift start if an employee has not checked in."""
    db = open_db()
    db.row_factory = sqlite3.Row
    now = get_tz_now()
    today = now.date().isoformat()
    off_reason = is_off_day(db, today)
    if off_reason:
        db.close()
        return
    try:
        default_shift = db.execute(
            "SELECT id, name, start_time FROM shifts WHERE name='General' ORDER BY id LIMIT 1"
        ).fetchone()
        if not default_shift:
            default_shift = db.execute("SELECT id, name, start_time FROM shifts ORDER BY start_time LIMIT 1").fetchone()
        default_shift_id = default_shift["id"] if default_shift else 0
        default_shift_name = default_shift["name"] if default_shift else "General"
        default_start_time = default_shift["start_time"] if default_shift else "09:00"

        employees = db.execute("""
            SELECT e.id, e.name, e.telegram_chat_id, COALESCE(e.shift_id, 0) as shift_id,
                   COALESCE(s.name, ?) as shift_name,
                   COALESCE(s.start_time, ?) as start_time
            FROM employees e
            LEFT JOIN shifts s ON s.id = e.shift_id
            WHERE e.role IN ('employee','sales','accountant','subadmin','manager')
              AND lower(COALESCE(e.name, '')) != 'test employee'
              AND lower(COALESCE(e.username, '')) NOT LIKE 'testuser_%'
            ORDER BY COALESCE(s.start_time, ?), e.name
        """, (default_shift_name, default_start_time, default_start_time)).fetchall()

        for emp in employees:
            shift_start_at = _attendance_datetime(today, emp["start_time"])
            if not shift_start_at:
                continue
            remind_at = shift_start_at + timedelta(hours=1)
            if now < remind_at:
                continue
            checked_in = db.execute("""
                SELECT 1 FROM attendance
                WHERE employee_id=? AND action_date=? AND action='checkin'
                LIMIT 1
            """, (emp["id"], today)).fetchone()
            if checked_in:
                continue
            if employee_on_approved_leave(db, emp["id"], today):
                continue
            already_sent = db.execute("""
                SELECT 1 FROM attendance_checkin_reminders
                WHERE employee_id=? AND action_date=?
            """, (emp["id"], today)).fetchone()
            if already_sent:
                continue

            msg = (
                f"<b>Check-in reminder</b>\n\n"
                f"Hi {emp['name']}, your shift started at <b>{emp['start_time']}</b> "
                f"and no check-in is recorded yet for today.\n\n"
                f"Please check in on Task Manager if you are working today."
            )
            private_sent = send_telegram(emp["telegram_chat_id"], msg) if emp["telegram_chat_id"] else False
            group_msg = (
                f"<b>Check-in pending</b>\n\n"
                f"{emp['name']} has not checked in yet. "
                f"Shift: {emp['shift_name']} ({emp['start_time']})."
            )
            group_sent = send_telegram_group(group_msg, category="attendance_event")
            if private_sent or group_sent:
                db.execute("""
                    INSERT OR IGNORE INTO attendance_checkin_reminders
                    (employee_id, action_date, shift_id, shift_start_time)
                    VALUES (?,?,?,?)
                """, (emp["id"], today, emp["shift_id"] or default_shift_id, emp["start_time"]))
                db.commit()
    except Exception as e:
        print(f"late check-in reminder error: {e}")
    finally:
        db.close()


def send_midnight_continuity_reminders():
    """Ask overnight workers to check in again after the attendance day resets."""
    if not is_telegram_enabled():
        return
    now = get_tz_now()
    if now.hour != 0 or now.minute > 30:
        return
    today = now.date().isoformat()
    previous_day = (now.date() - timedelta(days=1)).isoformat()
    db = open_db()
    db.row_factory = sqlite3.Row
    try:
        candidates = db.execute("""
            SELECT a.id as attendance_id, a.employee_id, a.action_date, a.action_time,
                   e.name, e.telegram_chat_id
            FROM attendance a
            JOIN employees e ON e.id = a.employee_id
            WHERE a.action='checkin'
              AND a.action_date=?
              AND e.role IN ('employee','sales','accountant','subadmin','manager')
              AND lower(COALESCE(e.name, '')) != 'test employee'
              AND lower(COALESCE(e.username, '')) NOT LIKE 'testuser_%'
            ORDER BY a.action_time DESC, a.id DESC
        """, (previous_day,)).fetchall()

        for row in candidates:
            last_before_reset = db.execute("""
                SELECT id, action
                FROM attendance
                WHERE employee_id=? AND action_date <= ?
                ORDER BY action_date DESC, action_time DESC, id DESC
                LIMIT 1
            """, (row["employee_id"], previous_day)).fetchone()
            if not last_before_reset or last_before_reset["id"] != row["attendance_id"] or last_before_reset["action"] != "checkin":
                continue

            checked_in_today = db.execute("""
                SELECT 1 FROM attendance
                WHERE employee_id=? AND action_date=? AND action='checkin'
                LIMIT 1
            """, (row["employee_id"], today)).fetchone()
            if checked_in_today:
                continue

            already_sent = db.execute("""
                SELECT 1 FROM attendance_midnight_reminders
                WHERE employee_id=? AND previous_action_date=? AND new_action_date=?
            """, (row["employee_id"], previous_day, today)).fetchone()
            if already_sent:
                continue

            msg = (
                f"<b>Midnight check-in reminder</b>\n\n"
                f"Hi {row['name']}, your previous attendance day started at "
                f"<b>{row['action_time']}</b> on {previous_day} and appears to have crossed midnight.\n\n"
                f"Task Manager starts a new attendance day at 12:00 AM. "
                f"If you are still working, please check in again now for {today}."
            )
            private_sent = send_telegram(row["telegram_chat_id"], msg) if row["telegram_chat_id"] else False
            group_msg = (
                f"<b>Midnight attendance continuity</b>\n\n"
                f"{row['name']} appears to be continuing work after midnight. "
                f"They have been asked to check in again for {today}."
            )
            group_sent = send_telegram_group(group_msg, category="attendance_event")
            if private_sent or group_sent:
                db.execute("""
                    INSERT OR IGNORE INTO attendance_midnight_reminders
                    (employee_id, previous_action_date, new_action_date)
                    VALUES (?,?,?)
                """, (row["employee_id"], previous_day, today))
                db.commit()
    except Exception as e:
        print(f"midnight continuity reminder error: {e}")
    finally:
        db.close()


def check_invoice_generation():
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()

    templates = db.execute("""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name
        FROM invoices i
        LEFT JOIN clients c ON i.client_id = c.id
        WHERE i.auto_generate = 1 AND i.next_generate_date <= ? AND i.billing_frequency > 0
    """, (today,)).fetchall()

    for tmpl in templates:
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (tmpl["id"],)).fetchall()
        count = db.execute("SELECT COUNT(*) as cnt FROM invoices").fetchone()["cnt"]
        inv_number = f"INV-{get_tz_now().date().strftime('%Y%m')}-{count + 1:04d}"
        inv_date = get_tz_now().date().isoformat()

        freq = tmpl["billing_frequency"]
        next_date = get_tz_now().date()
        next_month = next_date.month + freq
        next_year = next_date.year + (next_month - 1) // 12
        next_month = ((next_month - 1) % 12) + 1
        try:
            next_date = date(next_year, next_month, next_date.day)
        except ValueError:
            import calendar as cal_mod
            last_day = cal_mod.monthrange(next_year, next_month)[1]
            next_date = date(next_year, next_month, last_day)

        cursor = db.execute("""
            INSERT INTO invoices (client_id, invoice_number, invoice_date, due_date, subtotal,
                discount_percent, discount_amount, total, remarks, status, billing_frequency,
                auto_generate, next_generate_date, template_invoice_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (tmpl["client_id"], inv_number, inv_date, inv_date, tmpl["subtotal"],
              tmpl["discount_percent"], tmpl["discount_amount"], tmpl["total"],
              tmpl["remarks"], "sent", tmpl["billing_frequency"], 0,
              None, tmpl["id"]))
        new_inv_id = cursor.lastrowid

        for item in items:
            db.execute("""
                INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, total)
                VALUES (?,?,?,?,?)
            """, (new_inv_id, item["description"], item["quantity"], item["unit_price"], item["total"]))

        db.execute("UPDATE invoices SET next_generate_date=? WHERE id=?",
                    (next_date.isoformat(), tmpl["id"]))
        db.commit()

        acct_chat_id = get_accountant_chat_id()
        if acct_chat_id:
            currency = tmpl["currency"] or "INR"
            currency_sym = get_currency_symbol(currency)
            msg = (f"<b>📄 Invoice Auto-Generated</b>\n\n"
                   f"Client: {tmpl['client_name']}\n"
                   f"Invoice #: {inv_number}\n"
                   f"Amount: {currency_sym}{tmpl['total']:.2f} ({currency})\n"
                   f"Date: {inv_date}\n\n"
                   f"Please process this invoice.")
            send_telegram(acct_chat_id, msg)

        # Auto-email invoice to client
        client = db.execute("SELECT * FROM clients WHERE id=?", (tmpl["client_id"],)).fetchone()
        if client and client["email"]:
            send_email(client["email"],
                f"Invoice {inv_number} from {get_app_setting('company_name', 'Task Manager')}",
                f"<p>Dear {client['contact_person'] or client['name']},</p>"
                f"<p>Invoice <b>{inv_number}</b> has been generated for <b>{currency_sym}{tmpl['total']:.2f}</b>.</p>"
                f"<p>Please process at your earliest convenience.</p>"
                f"<p>Best regards,<br>{get_app_setting('company_name', 'Task Manager')}</p>")

    db.close()


def send_unpaid_reminders():
    """Send weekly reminders about unpaid invoices to accountant (run on Mondays)."""
    acct_chat_id = get_accountant_chat_id()
    if not acct_chat_id:
        return
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()

    unpaid = db.execute("""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name
        FROM invoices i
        LEFT JOIN clients c ON i.client_id = c.id
        WHERE i.status IN ('sent', 'draft') AND i.status != 'paid'
        ORDER BY i.invoice_date
    """).fetchall()

    if unpaid:
        msg = "<b>⚠️ Unpaid Invoice Reminder</b>\n\n"
        total_inr = 0
        total_usd = 0
        for inv in unpaid:
            currency = inv["currency"] or "INR"
            currency_sym = get_currency_symbol(currency)
            msg += f"• {inv['client_name']} - {inv['invoice_number']}: {currency_sym}{inv['total']:.2f} ({currency})\n"
            if currency == "USD":
                total_usd += inv["total"]
            else:
                total_inr += inv["total"]
        msg += f"\n<b>Total Pending:</b>"
        if total_inr > 0:
            msg += f" ₹{total_inr:,.2f}"
        if total_usd > 0:
            msg += f" ${total_usd:,.2f}"
        msg += "\n\nPlease follow up on these invoices."

        # Update last_reminder_date
        for inv in unpaid:
            db.execute("UPDATE invoices SET last_reminder_date=? WHERE id=?", (today, inv["id"]))
        db.commit()

        send_telegram(acct_chat_id, msg)
    db.close()


def send_due_lead_followup_reminders():
    """Send due lead follow-up reminders once per follow-up."""
    if not is_telegram_enabled():
        return
    db = open_db()
    db.row_factory = sqlite3.Row
    now_str = get_tz_now().strftime("%Y-%m-%d %H:%M")
    reminders = db.execute("""
        SELECT lf.*, l.company_name, l.contact_person, l.phone, l.email, l.stage,
               l.assigned_to, e.name as assigned_name
        FROM lead_followups lf
        JOIN leads l ON lf.lead_id = l.id
        LEFT JOIN employees e ON l.assigned_to = e.id
        WHERE lf.status='pending'
          AND lf.due_at <= ?
          AND COALESCE(lf.telegram_sent_at, '') = ''
          AND l.stage != 'converted'
        ORDER BY lf.due_at ASC
        LIMIT 25
    """, (now_str,)).fetchall()
    if not reminders:
        db.close()
        return

    import html as _html
    chat_id = get_lead_reminder_chat_id(db)
    if not chat_id:
        db.close()
        return
    public_base = (get_app_setting("public_base_url", "http://localhost:5050") or "").rstrip("/")

    for rem in reminders:
        stage_label = LEAD_STAGE_LABELS.get(rem["stage"], rem["stage"])
        contact_bits = []
        if rem["contact_person"]:
            contact_bits.append(rem["contact_person"])
        if rem["phone"]:
            contact_bits.append(rem["phone"])
        if rem["email"]:
            contact_bits.append(rem["email"])
        contact = " | ".join(contact_bits) or "No contact saved"
        lead_url = f"{public_base}/leads/{rem['lead_id']}" if public_base else f"/leads/{rem['lead_id']}"
        msg = (
            f"<b>Lead Follow-up Due</b>\n\n"
            f"<b>{_html.escape(rem['company_name'])}</b>\n"
            f"Stage: {_html.escape(stage_label)}\n"
            f"Contact: {_html.escape(contact)}\n"
            f"Due: {_html.escape(rem['due_at'])}\n"
            f"Owner: {_html.escape(rem['assigned_name'] or 'Unassigned')}\n\n"
            f"{_html.escape(rem['note'])}\n\n"
            f"{_html.escape(lead_url)}"
        )
        if send_telegram(chat_id, msg):
            sent_at = get_tz_now().strftime("%Y-%m-%d %H:%M")
            db.execute("UPDATE lead_followups SET telegram_sent_at=? WHERE id=?", (sent_at, rem["id"]))
            if rem["assigned_to"]:
                create_notification(db, rem["assigned_to"],
                    f"Lead follow-up due: {rem['company_name']}", "warning", f"/leads/{rem['lead_id']}")
            db.commit()
    db.close()


_tg_update_offset = 0

def poll_telegram_messages():
    global _tg_update_offset
    if not is_telegram_enabled():
        return
    if telegram_lead_intake_enabled():
        return
    import urllib.request, json as _json
    try:
        bot_token = get_telegram_bot_token()
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={_tg_update_offset}&timeout=0&limit=10"
        resp = urllib.request.urlopen(url, timeout=15)
        data = _json.loads(resp.read())
        for update in data.get("result", []):
            _tg_update_offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            chat_type = chat.get("type", "")
            if chat_id and chat_type == "private":
                first_name = msg.get("from", {}).get("first_name", "there")
                reply = (
                    f"\U0001f44b Hi {first_name}!\n\n"
                    f"Your Telegram Chat ID is:\n"
                    f"<code>{chat_id}</code>\n\n"
                    f"Send this number to your admin to receive task reminders."
                )
                send_telegram(chat_id, reply)
    except Exception as e:
        print(f"Telegram poll error: {e}")


def check_celebrations():
    """Check for birthdays, joining anniversaries, and wedding anniversaries.
    Sends wishes to employees on the day, and notifies admins 7 days before."""
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date()
    today_str = today.isoformat()
    today_mmdd = today.strftime("%m-%d")

    # Calculate date 7 days from now
    advance_date = today + timedelta(days=7)
    advance_mmdd = advance_date.strftime("%m-%d")

    employees = db.execute("SELECT * FROM employees").fetchall()
    # Get admin chat IDs for advance notifications
    admins = db.execute("SELECT * FROM employees WHERE role IN ('admin','superadmin') AND telegram_chat_id != ''").fetchall()

    for emp in employees:
        celebrations_today = []
        celebrations_upcoming = []

        # Check birthday
        if emp["birthday"]:
            try:
                bday = emp["birthday"]  # stored as YYYY-MM-DD
                bday_mmdd = bday[5:]  # MM-DD
                if bday_mmdd == today_mmdd:
                    birth_year = int(bday[:4])
                    age = today.year - birth_year
                    celebrations_today.append(("birthday", age))
                if bday_mmdd == advance_mmdd:
                    birth_year = int(bday[:4])
                    age = advance_date.year - birth_year
                    celebrations_upcoming.append(("birthday", age))
            except Exception:
                pass

        # Check joining date
        if emp["joining_date"]:
            try:
                jdate = emp["joining_date"]
                jdate_mmdd = jdate[5:]
                if jdate_mmdd == today_mmdd:
                    join_year = int(jdate[:4])
                    years = today.year - join_year
                    if years > 0:
                        celebrations_today.append(("work_anniversary", years))
                if jdate_mmdd == advance_mmdd:
                    join_year = int(jdate[:4])
                    years = advance_date.year - join_year
                    if years > 0:
                        celebrations_upcoming.append(("work_anniversary", years))
            except Exception:
                pass

        # Check wedding anniversary
        if emp["wedding_anniversary"]:
            try:
                wdate = emp["wedding_anniversary"]
                wdate_mmdd = wdate[5:]
                if wdate_mmdd == today_mmdd:
                    wed_year = int(wdate[:4])
                    years = today.year - wed_year
                    if years > 0:
                        celebrations_today.append(("wedding_anniversary", years))
                if wdate_mmdd == advance_mmdd:
                    wed_year = int(wdate[:4])
                    years = advance_date.year - wed_year
                    if years > 0:
                        celebrations_upcoming.append(("wedding_anniversary", years))
            except Exception:
                pass

        # Send wishes to employee on their day
        if celebrations_today and emp["telegram_chat_id"]:
            for ctype, years in celebrations_today:
                if ctype == "birthday":
                    msg = (f"\U0001f382 <b>Happy Birthday, {emp['name']}!</b>\n\n"
                           f"Wishing you a wonderful {years}th birthday! \U0001f389\n"
                           f"Have a great day!")
                elif ctype == "work_anniversary":
                    msg = (f"\U0001f3c6 <b>Happy Work Anniversary, {emp['name']}!</b>\n\n"
                           f"Congratulations on completing {years} year{'s' if years > 1 else ''} with us! \U0001f389\n"
                           f"Thank you for your dedication!")
                elif ctype == "wedding_anniversary":
                    msg = (f"\U0001f48d <b>Happy Wedding Anniversary, {emp['name']}!</b>\n\n"
                           f"Wishing you a wonderful {years}th wedding anniversary! \U0001f389\n"
                           f"Have a beautiful day!")
                send_telegram(emp["telegram_chat_id"], msg)

        # Also send to group chat
        if celebrations_today and get_telegram_group_chat_id():
            for ctype, years in celebrations_today:
                if ctype == "birthday":
                    msg = f"\U0001f382 Today is <b>{emp['name']}</b>'s birthday! They turn {years}. Wish them a happy birthday! \U0001f389"
                elif ctype == "work_anniversary":
                    msg = f"\U0001f3c6 <b>{emp['name']}</b> completes {years} year{'s' if years > 1 else ''} with us today! Congratulations! \U0001f389"
                elif ctype == "wedding_anniversary":
                    msg = f"\U0001f48d It's <b>{emp['name']}</b>'s {years}th wedding anniversary today! Congratulations! \U0001f389"
                send_telegram_group(msg, category="celebration")

        # Notify admins 7 days in advance
        if celebrations_upcoming:
            for admin in admins:
                for ctype, years in celebrations_upcoming:
                    event_date = advance_date.strftime("%d %b %Y")
                    if ctype == "birthday":
                        msg = (f"\U0001f4c5 <b>Upcoming Birthday</b>\n\n"
                               f"{emp['name']} turns {years} on {event_date} (in 7 days).\n"
                               f"Plan ahead for their birthday! \U0001f382")
                    elif ctype == "work_anniversary":
                        msg = (f"\U0001f4c5 <b>Upcoming Work Anniversary</b>\n\n"
                               f"{emp['name']} completes {years} year{'s' if years > 1 else ''} on {event_date} (in 7 days).\n"
                               f"Plan ahead to celebrate! \U0001f3c6")
                    elif ctype == "wedding_anniversary":
                        msg = (f"\U0001f4c5 <b>Upcoming Wedding Anniversary</b>\n\n"
                               f"{emp['name']}'s {years}th wedding anniversary is on {event_date} (in 7 days).\n"
                               f"Plan ahead to wish them! \U0001f48d")
                    send_telegram(admin["telegram_chat_id"], msg)

    db.close()


def send_overdue_review_reminders():
    """Send daily reminders for overdue reviews until completed."""
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date().isoformat()

    overdue = db.execute("""
        SELECT r.*, p.name as project_name, rc.participants
        FROM reviews r
        JOIN projects p ON r.project_id = p.id
        JOIN review_configs rc ON r.review_config_id = rc.id
        WHERE r.status = 'pending' AND r.scheduled_date < ?
    """, (today,)).fetchall()

    for rev in overdue:
        # Update status to overdue
        db.execute("UPDATE reviews SET status='overdue' WHERE id=? AND status='pending'", (rev["id"],))

        # Only send reminder if we haven't sent one today
        if rev["last_reminder_date"] == today:
            continue

        db.execute("UPDATE reviews SET last_reminder_date=? WHERE id=?", (today, rev["id"]))

        # Notify participants
        participant_ids = [p.strip() for p in (rev["participants"] or "").split(",") if p.strip()]
        for pid in participant_ids:
            try:
                create_notification(db, int(pid),
                    f"OVERDUE: Review for {rev['project_name']} was due {rev['scheduled_date']}. Please complete ASAP.",
                    "warning", "/reviews")
            except:
                pass

        # Send Telegram if enabled
        if is_telegram_enabled():
            msg = f"<b>\u26a0\ufe0f Overdue Review</b>\n"
            msg += f"Project: {rev['project_name']}\n"
            msg += f"Due: {rev['scheduled_date']}\n"
            msg += f"Please complete the review and submit MOM."
            send_telegram_group(msg)

    db.commit()
    db.close()


def auto_generate_reviews():
    """Auto-generate upcoming reviews based on configured frequency."""
    db = open_db()
    db.row_factory = sqlite3.Row
    today = get_tz_now().date()
    today_str = today.isoformat()

    configs = db.execute("SELECT * FROM review_configs WHERE active=1").fetchall()

    for cfg in configs:
        # Check if there's already a pending/overdue review for this config
        existing = db.execute("""
            SELECT id FROM reviews
            WHERE review_config_id=? AND status IN ('pending','overdue')
        """, (cfg["id"],)).fetchone()
        if existing:
            continue

        # Check the last completed review date
        last_review = db.execute("""
            SELECT scheduled_date FROM reviews
            WHERE review_config_id=? AND status='completed'
            ORDER BY scheduled_date DESC LIMIT 1
        """, (cfg["id"],)).fetchone()

        freq = cfg["frequency"]
        if freq == "weekly":
            delta_days = 7
        elif freq == "biweekly":
            delta_days = 14
        elif freq == "monthly":
            delta_days = 30
        elif freq == "quarterly":
            delta_days = 90
        else:
            delta_days = 30

        if last_review:
            last_date = datetime.strptime(last_review["scheduled_date"], "%Y-%m-%d").date()
            next_date = last_date + timedelta(days=delta_days)
        else:
            # No reviews yet — schedule one for today + 3 days lead time
            next_date = today + timedelta(days=3)

        # Only create if the next review date is within the next 7 days
        if next_date <= today + timedelta(days=7):
            next_str = next_date.isoformat()
            db.execute("""INSERT INTO reviews (review_config_id, project_id, scheduled_date, scheduled_time, status)
                          VALUES (?,?,?,?,?)""",
                       (cfg["id"], cfg["project_id"], next_str, "10:00", "pending"))

            # Notify participants
            participant_ids = [p.strip() for p in (cfg["participants"] or "").split(",") if p.strip()]
            project = db.execute("SELECT name FROM projects WHERE id=?", (cfg["project_id"],)).fetchone()
            pname = project["name"] if project else f"Project #{cfg['project_id']}"
            for pid in participant_ids:
                try:
                    create_notification(db, int(pid),
                        f"Upcoming {freq} review for {pname} scheduled on {next_str}",
                        "info", "/reviews")
                except:
                    pass

    db.commit()
    db.close()


def scheduler_loop():
    global _tg_update_offset
    if is_telegram_enabled() and not telegram_lead_intake_enabled():
        import urllib.request, json as _json
        try:
            bot_token = get_telegram_bot_token()
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset=-1&limit=1"
            resp = urllib.request.urlopen(url, timeout=10)
            data = _json.loads(resp.read())
            results = data.get("result", [])
            if results:
                _tg_update_offset = results[-1]["update_id"] + 1
        except Exception:
            pass

    last_poll = 0
    last_3am = ""
    last_6am = ""
    last_8am = ""
    last_9am = ""
    last_10am = ""
    last_5pm = ""
    last_8pm = ""
    last_daily_staff_report = ""
    last_lead_scan = ""
    last_checkout_scan = ""
    last_late_checkin_scan = ""
    last_midnight_continuity_scan = ""
    while True:
        now = get_tz_now()
        today_str = now.date().isoformat()
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if minute_key != last_lead_scan:
            last_lead_scan = minute_key
            try:
                send_due_lead_followup_reminders()
            except Exception as e:
                print(f"lead reminder error: {e}")

        if minute_key != last_checkout_scan:
            last_checkout_scan = minute_key
            try:
                send_checkout_reminders()
            except Exception as e:
                print(f"checkout reminder scan error: {e}")

        if minute_key != last_late_checkin_scan:
            last_late_checkin_scan = minute_key
            try:
                send_late_checkin_reminders()
            except Exception as e:
                print(f"late check-in reminder scan error: {e}")

        if minute_key != last_midnight_continuity_scan:
            last_midnight_continuity_scan = minute_key
            try:
                send_midnight_continuity_reminders()
            except Exception as e:
                print(f"midnight continuity reminder scan error: {e}")

        # Daily local SQLite snapshot with simple retention.
        if now.hour == 3 and now.minute == 0 and last_3am != today_str:
            last_3am = today_str
            try:
                path = create_db_backup_snapshot("auto", retain=14)
                print(f"automatic backup created: {path}")
            except Exception as e:
                print(f"automatic backup error: {e}")

        # Refresh exchange rates daily at 6 AM (once per day)
        if now.hour == 6 and now.minute == 0 and last_6am != today_str:
            last_6am = today_str
            try:
                fetch_exchange_rates()
            except Exception:
                pass

        # Morning tasks at 8 AM (once per day)
        if now.hour == 8 and now.minute == 0 and last_8am != today_str:
            last_8am = today_str
            send_daily_reminders()
            check_invoice_generation()
            check_celebrations()
            try:
                send_overdue_review_reminders()
                auto_generate_reviews()
            except Exception:
                pass
            # Weekly unpaid invoice reminders on Mondays
            if now.weekday() == 0:
                send_unpaid_reminders()
            # Completed-month attendance summary. Disabled unless a dedicated
            # attendance chat is configured, so it never posts to the general group.
            if now.day == 1:
                send_month_end_attendance_summary()

        # Attendance report at 10 AM (some team members log in later)
        if now.hour == 10 and now.minute == 0 and last_10am != today_str:
            last_10am = today_str
            send_absentees_report()

        daily_staff_report_time = get_app_setting("daily_staff_report_time", "09:30") or "09:30"
        if now.strftime("%H:%M") == daily_staff_report_time and last_daily_staff_report != today_str:
            last_daily_staff_report = today_str
            try:
                send_daily_staff_activity_report()
            except Exception as e:
                print(f"daily staff report scheduler error: {e}")

        # Scheduled project reports at 9 AM
        if now.hour == 9 and now.minute == 0 and last_9am != today_str:
            last_9am = today_str
            try:
                run_scheduled_reports()
            except Exception as e:
                print(f"scheduled reports error: {e}")

        # Evening tasks at 5 PM (once per day)
        if now.hour == 17 and now.minute == 0 and last_5pm != today_str:
            last_5pm = today_str
            send_evening_reminders()

        # (8 PM absentee report removed — covered by 10 AM report)

        import time as _t
        if _t.time() - last_poll >= 5:
            poll_telegram_messages()
            last_poll = _t.time()
        _time.sleep(5)


# ── Admin: Test Telegram ────────────────────────────────


@app.route("/admin/test-telegram/<int:emp_id>", methods=["POST"])
@admin_required
def test_telegram(emp_id):
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp and emp["telegram_chat_id"]:
        send_telegram(emp["telegram_chat_id"], f"Test message for {emp['name']} from Task Manager!")
        flash(f"Test message sent to {emp['name']}.", "success")
    else:
        flash("No Telegram chat ID configured.", "error")
    return redirect(url_for("admin_home"))


# ── Attendance: Check-in / Check-out ────────────────────


def general_telegram_category_enabled(category):
    if category == "attendance_event":
        return get_app_setting("telegram_general_attendance_enabled", "1") == "1"
    if category == "celebration":
        return get_app_setting("telegram_general_celebrations_enabled", "1") == "1"
    return get_app_setting("general_telegram_notifications_enabled", "0") == "1"


def send_telegram_group(message, category="general"):
    if not general_telegram_category_enabled(category):
        print(f"[telegram general] skipped — {category} disabled")
        return False
    if is_telegram_enabled() and get_telegram_group_chat_id():
        send_telegram(get_telegram_group_chat_id(), message)
        return True
    return False


@app.route("/checkin", methods=["POST"])
@login_required
def checkin():
    db = get_db()
    emp_id = session["user_id"]
    now = get_tz_now()
    today = now.date().isoformat()
    now_time = now.strftime("%H:%M:%S")

    last = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id DESC LIMIT 1",
        (emp_id, today),
    ).fetchone()
    if last and last["action"] == "checkin":
        flash("You are already checked in. Please check out first.", "error")
        return redirect(request.referrer or url_for("today_view"))

    db.execute(
        "INSERT INTO attendance (employee_id, action, action_date, action_time) VALUES (?,?,?,?)",
        (emp_id, "checkin", today, now_time),
    )
    db.commit()

    name = session.get("user_name", "")
    send_telegram_group(f"<b>{name}</b> checked IN at {now_time} on {today}", category="attendance_event")

    flash(f"Checked in at {now_time}", "success")
    return redirect(request.referrer or url_for("today_view"))


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    db = get_db()
    emp_id = session["user_id"]
    now = get_tz_now()
    today = now.date().isoformat()
    now_time = now.strftime("%H:%M:%S")

    last = db.execute(
        "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id DESC LIMIT 1",
        (emp_id, today),
    ).fetchone()
    if not last or last["action"] != "checkin":
        previous_day = (now.date() - timedelta(days=1)).isoformat()
        previous_open = db.execute("""
            SELECT action, action_date, action_time
            FROM attendance
            WHERE employee_id=? AND action_date <= ?
            ORDER BY action_date DESC, action_time DESC, id DESC
            LIMIT 1
        """, (emp_id, previous_day)).fetchone()
        if previous_open and previous_open["action"] == "checkin":
            flash(
                "Your previous work session crossed midnight. Please check in again for today's attendance day, then check out when done.",
                "error",
            )
        else:
            flash("You are not checked in. Please check in first.", "error")
        return redirect(request.referrer or url_for("today_view"))

    db.execute(
        "INSERT INTO attendance (employee_id, action, action_date, action_time) VALUES (?,?,?,?)",
        (emp_id, "checkout", today, now_time),
    )
    db.commit()

    name = session.get("user_name", "")
    send_telegram_group(f"<b>{name}</b> checked OUT at {now_time} on {today}", category="attendance_event")

    flash(f"Checked out at {now_time}", "success")
    return redirect(request.referrer or url_for("today_view"))


# ── Admin: Attendance Report ────────────────────────────


@app.route("/admin/attendance")
@admin_required
def attendance_report():
    db = get_db()
    employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()

    today_dt = get_tz_now().date()
    emp_filter = request.args.get("employee", 0, type=int)

    # Date range: default to current month
    from_date = request.args.get("from", today_dt.replace(day=1).isoformat())
    to_date = request.args.get("to", today_dt.isoformat())

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        from_dt = today_dt.replace(day=1)
        to_dt = today_dt
        from_date = from_dt.isoformat()
        to_date = to_dt.isoformat()

    # Build date list
    from datetime import timedelta as td
    date_list = []
    d = from_dt
    while d <= to_dt:
        date_list.append(d.isoformat())
        d += td(days=1)

    if emp_filter:
        show_employees = [db.execute("SELECT * FROM employees WHERE id=?", (emp_filter,)).fetchone()]
    else:
        show_employees = db.execute("SELECT * FROM employees WHERE role IN ('employee','sales','accountant','subadmin','manager') ORDER BY name").fetchall()

    show_employee_ids = [emp["id"] for emp in show_employees if emp]
    leave_by_emp = approved_leave_map(db, show_employee_ids, from_date, to_date)
    off_day_by_date = {day_str: is_non_working_date(db, day_str) for day_str in date_list}

    report = {}
    for emp in show_employees:
        if not emp:
            continue
        emp_data = {}
        total_range_minutes = 0
        total_days_present = 0
        leave_dates = set()
        present_dates = set()

        for day_str in date_list:
            leave_row = leave_by_emp.get(emp["id"], {}).get(day_str)
            off_day = off_day_by_date.get(day_str, {"is_off": False, "reason": ""})
            records = db.execute(
                "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id",
                (emp["id"], day_str),
            ).fetchall()

            if not records:
                if leave_row:
                    leave_dates.add(day_str)
                emp_data[day_str] = {
                    "records": [],
                    "total_minutes": 0,
                    "present": False,
                    "leave": leave_row,
                    "leave_type": leave_row["type_name"] if leave_row else "",
                    "off_day": off_day["is_off"],
                    "off_reason": off_day["reason"],
                }
                continue

            total_min = 0
            pairs = []
            i = 0
            while i < len(records):
                if records[i]["action"] == "checkin":
                    checkin_time = records[i]["action_time"]
                    checkin_id = records[i]["id"]
                    checkout_time = None
                    checkout_id = None
                    if i + 1 < len(records) and records[i + 1]["action"] == "checkout":
                        checkout_time = records[i + 1]["action_time"]
                        checkout_id = records[i + 1]["id"]
                        ci = datetime.strptime(checkin_time, "%H:%M:%S")
                        co = datetime.strptime(checkout_time, "%H:%M:%S")
                        diff = (co - ci).total_seconds() / 60
                        total_min += diff
                        pairs.append({"in": checkin_time, "out": checkout_time, "minutes": round(diff, 1),
                                      "in_id": checkin_id, "out_id": checkout_id})
                        i += 2
                    else:
                        pairs.append({"in": checkin_time, "out": "\u2014", "minutes": 0,
                                      "in_id": checkin_id, "out_id": None})
                        i += 1
                else:
                    i += 1

            emp_data[day_str] = {
                "records": pairs,
                "total_minutes": round(total_min, 1),
                "present": True,
                "leave": leave_row,
                "leave_type": leave_row["type_name"] if leave_row else "",
                "off_day": off_day["is_off"],
                "off_reason": off_day["reason"],
            }
            if pairs:
                total_days_present += 1
                present_dates.add(day_str)
            total_range_minutes += total_min

        # Count past absent working days only.
        today_iso = today_dt.isoformat()
        past_dates = [
            d for d in date_list
            if d < today_iso and not off_day_by_date.get(d, {}).get("is_off")
        ]
        days_absent = len(set(past_dates) - present_dates - leave_dates)
        if days_absent < 0:
            days_absent = 0

        report[emp["id"]] = {
            "name": emp["name"],
            "days": emp_data,
            "total_minutes": round(total_range_minutes, 1),
            "total_hours": round(total_range_minutes / 60, 1),
            "days_present": total_days_present,
            "days_absent": days_absent,
            "days_leave": len(leave_dates),
        }

    return render_template(
        "attendance.html",
        employees=employees, emp_filter=emp_filter,
        from_date=from_date, to_date=to_date,
        today_str=today_dt.isoformat(),
        date_list=date_list, report=report,
    )


# ── Admin: Manage Attendance (Add / Edit / Delete) ────


@app.route("/admin/attendance/add", methods=["POST"])
@admin_required
def admin_attendance_add():
    db = get_db()
    emp_id = request.form.get("employee_id", 0, type=int)
    action = request.form.get("action", "").strip()
    action_date = request.form.get("action_date", "").strip()
    action_time = request.form.get("action_time", "").strip()

    if not emp_id or action not in ("checkin", "checkout") or not action_date or not action_time:
        flash("All fields are required (employee, action, date, time).", "error")
        return redirect(request.referrer or url_for("attendance_report"))

    # Validate employee exists
    emp = db.execute("SELECT name FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp:
        flash("Employee not found.", "error")
        return redirect(request.referrer or url_for("attendance_report"))

    # Normalise time to HH:MM:SS
    if len(action_time) == 5:
        action_time += ":00"

    db.execute(
        "INSERT INTO attendance (employee_id, action, action_date, action_time) VALUES (?,?,?,?)",
        (emp_id, action, action_date, action_time),
    )
    db.commit()

    action_label = "Check-in" if action == "checkin" else "Check-out"
    log_activity(db, session["user_id"], f"added attendance",
                 f"{action_label} for {emp['name']} on {action_date} at {action_time}")
    flash(f"{action_label} added for {emp['name']} on {action_date} at {action_time}.", "success")
    return redirect(request.referrer or url_for("attendance_report"))


@app.route("/admin/attendance/edit/<int:record_id>", methods=["POST"])
@admin_required
def admin_attendance_edit(record_id):
    db = get_db()
    new_time = request.form.get("action_time", "").strip()
    if not new_time:
        flash("Time is required.", "error")
        return redirect(request.referrer or url_for("attendance_report"))

    if len(new_time) == 5:
        new_time += ":00"

    record = db.execute("SELECT a.*, e.name as emp_name FROM attendance a JOIN employees e ON a.employee_id=e.id WHERE a.id=?", (record_id,)).fetchone()
    if not record:
        flash("Record not found.", "error")
        return redirect(request.referrer or url_for("attendance_report"))

    db.execute("UPDATE attendance SET action_time=? WHERE id=?", (new_time, record_id))
    db.commit()

    action_label = "Check-in" if record["action"] == "checkin" else "Check-out"
    log_activity(db, session["user_id"], "edited attendance",
                 f"{action_label} time for {record['emp_name']} on {record['action_date']}: {record['action_time']} → {new_time}")
    flash(f"Updated {action_label} time to {new_time}.", "success")
    return redirect(request.referrer or url_for("attendance_report"))


@app.route("/admin/attendance/delete/<int:record_id>", methods=["POST"])
@admin_required
def admin_attendance_delete(record_id):
    db = get_db()
    record = db.execute("SELECT a.*, e.name as emp_name FROM attendance a JOIN employees e ON a.employee_id=e.id WHERE a.id=?", (record_id,)).fetchone()
    if not record:
        flash("Record not found.", "error")
        return redirect(request.referrer or url_for("attendance_report"))

    db.execute("DELETE FROM attendance WHERE id=?", (record_id,))
    db.commit()

    action_label = "Check-in" if record["action"] == "checkin" else "Check-out"
    log_activity(db, session["user_id"], "deleted attendance",
                 f"{action_label} for {record['emp_name']} on {record['action_date']} at {record['action_time']}")
    flash(f"Deleted {action_label} record for {record['emp_name']}.", "success")
    return redirect(request.referrer or url_for("attendance_report"))


# ── Admin: Attendance Export (Excel) ───────────────────


@app.route("/admin/attendance/export")
@admin_required
def attendance_export():
    try:
        import openpyxl
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("attendance_report"))

    db = get_db()
    today_dt = get_tz_now().date()
    emp_filter = request.args.get("employee", 0, type=int)
    year = request.args.get("year", today_dt.year, type=int)
    month = request.args.get("month", today_dt.month, type=int)

    import calendar
    month_name = calendar.month_name[month]
    days_in_month = calendar.monthrange(year, month)[1]

    if emp_filter:
        show_employees = [db.execute("SELECT * FROM employees WHERE id=?", (emp_filter,)).fetchone()]
    else:
        show_employees = db.execute("SELECT * FROM employees WHERE role IN ('employee','sales','accountant','subadmin','manager') ORDER BY name").fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Attendance {month_name} {year}"

    # Header row
    headers = ["Employee", "Date", "Check In", "Check Out", "Hours Worked"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = openpyxl.styles.Font(bold=True)

    row_num = 2
    for emp in show_employees:
        if not emp:
            continue
        for day in range(1, days_in_month + 1):
            day_str = f"{year}-{month:02d}-{day:02d}"
            records = db.execute(
                "SELECT * FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id",
                (emp["id"], day_str),
            ).fetchall()

            if not records:
                continue

            i = 0
            while i < len(records):
                if records[i]["action"] == "checkin":
                    checkin_time = records[i]["action_time"]
                    checkout_time = ""
                    hours = 0
                    if i + 1 < len(records) and records[i + 1]["action"] == "checkout":
                        checkout_time = records[i + 1]["action_time"]
                        ci = datetime.strptime(checkin_time, "%H:%M:%S")
                        co = datetime.strptime(checkout_time, "%H:%M:%S")
                        hours = round((co - ci).total_seconds() / 3600, 2)
                        i += 2
                    else:
                        i += 1

                    ws.cell(row=row_num, column=1, value=emp["name"])
                    ws.cell(row=row_num, column=2, value=day_str)
                    ws.cell(row=row_num, column=3, value=checkin_time)
                    ws.cell(row=row_num, column=4, value=checkout_time)
                    ws.cell(row=row_num, column=5, value=hours)
                    row_num += 1
                else:
                    i += 1

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"attendance_{year}_{month:02d}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ══════════════════════════════════════════════════════════
# ══  SUPER ADMIN: Client Management  ═════════════════════
# ══════════════════════════════════════════════════════════


@app.route("/clients")
@login_required
@require_perm("view_clients")
def clients_list():
    db = get_db()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    # Fetch projects grouped by client for cross-reference display
    client_projects = {}
    all_projects = db.execute("""
        SELECT id, client_id, name, status FROM projects
        WHERE client_id > 0 ORDER BY name
    """).fetchall()
    for p in all_projects:
        cid = p["client_id"]
        if cid not in client_projects:
            client_projects[cid] = []
        client_projects[cid].append(p)
    return render_template("clients.html", clients=clients, client_projects=client_projects)


@app.route("/clients/add", methods=["POST"])
@login_required
@require_perm("create_client")
def client_add():
    name = request.form.get("name", "").strip()
    contact_person = request.form.get("contact_person", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    notes = request.form.get("notes", "").strip()
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO clients (name, contact_person, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
            (name, contact_person, email, phone, address, notes),
        )
        db.commit()
        flash(f"Client '{name}' added.", "success")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>/edit", methods=["POST"])
@login_required
@require_perm("edit_client")
def client_edit(client_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    contact_person = request.form.get("contact_person", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    notes = request.form.get("notes", "").strip()
    if name:
        db.execute("""UPDATE clients SET name=?, contact_person=?, email=?, phone=?, address=?, notes=?
                      WHERE id=?""", (name, contact_person, email, phone, address, notes, client_id))
        db.commit()
        flash("Client updated.", "success")
    return redirect(url_for("clients_list"))


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
@require_perm("delete_client")
def client_delete(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if client:
        inv_count = db.execute("SELECT COUNT(*) as cnt FROM invoices WHERE client_id=?", (client_id,)).fetchone()["cnt"]
        if inv_count > 0:
            flash(f"Cannot delete '{client['name']}' \u2014 has {inv_count} invoices. Delete invoices first.", "error")
        else:
            db.execute("DELETE FROM projects WHERE client_id=?", (client_id,))
            db.execute("DELETE FROM clients WHERE id=?", (client_id,))
            db.commit()
            flash(f"Client '{client['name']}' deleted.", "success")
    return redirect(url_for("clients_list"))


# ══════════════════════════════════════════════════════════
# ══  Lead Management / Sales Pipeline  ═══════════════════
# ══════════════════════════════════════════════════════════


@app.route("/leads")
@login_required
@require_perm("view_leads")
def leads_list():
    db = get_db()
    stage_filter = request.args.get("stage", "").strip()
    assigned_filter = request.args.get("assigned", 0, type=int)
    due_filter = request.args.get("due", "").strip()
    search = request.args.get("q", "").strip()
    now = get_tz_now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.date().isoformat()

    where = ["1=1"]
    params = []
    if stage_filter in LEAD_STAGE_VALUES:
        where.append("l.stage=?")
        params.append(stage_filter)
    if assigned_filter == -1:
        where.append("COALESCE(l.assigned_to, 0)=0")
    elif assigned_filter:
        where.append("l.assigned_to=?")
        params.append(assigned_filter)
    if due_filter == "due":
        where.append("COALESCE(l.next_followup_at, '') != '' AND l.next_followup_at <= ? AND l.stage != 'converted'")
        params.append(now_str)
    elif due_filter == "today":
        where.append("substr(COALESCE(l.next_followup_at, ''), 1, 10) = ? AND l.stage != 'converted'")
        params.append(today_str)
    elif due_filter == "none":
        where.append("COALESCE(l.next_followup_at, '') = '' AND l.stage != 'converted'")
    if search:
        like = f"%{search}%"
        where.append("(l.company_name LIKE ? OR l.contact_person LIKE ? OR l.email LIKE ? OR l.phone LIKE ? OR l.requirement LIKE ?)")
        params.extend([like, like, like, like, like])

    leads = db.execute(f"""
        SELECT l.*, e.name as assigned_name, c.name as converted_client_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        LEFT JOIN clients c ON l.converted_client_id = c.id
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN l.stage='converted' THEN 1 ELSE 0 END,
            CASE WHEN COALESCE(l.next_followup_at, '') = '' THEN 1 ELSE 0 END,
            l.next_followup_at ASC,
            l.updated_at DESC
    """, params).fetchall()

    stage_counts = {stage: 0 for stage, _ in LEAD_STAGES}
    stage_values = {stage: 0 for stage, _ in LEAD_STAGES}
    for row in db.execute("""
        SELECT stage, COUNT(*) as cnt, COALESCE(SUM(estimated_value), 0) as total_value
        FROM leads
        GROUP BY stage
    """).fetchall():
        stage_counts[row["stage"]] = row["cnt"]
        stage_values[row["stage"]] = row["total_value"] or 0
    active_count = sum(count for stage, count in stage_counts.items() if stage != "converted")
    total_count = sum(stage_counts.values())
    pipeline_value = sum(value for stage, value in stage_values.items() if stage != "converted")
    filtered_total_value = sum((lead["estimated_value"] or 0) for lead in leads)
    unassigned_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE COALESCE(assigned_to, 0)=0 AND stage != 'converted'
    """).fetchone()["cnt"]
    due_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE COALESCE(next_followup_at, '') != ''
          AND next_followup_at <= ?
          AND stage != 'converted'
    """, (now_str,)).fetchone()["cnt"]
    today_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE substr(COALESCE(next_followup_at, ''), 1, 10)=?
          AND stage != 'converted'
    """, (today_str,)).fetchone()["cnt"]
    sales_people = db.execute("""
        SELECT id, name, role FROM employees
        WHERE role IN ('sales','manager','admin','superadmin')
        ORDER BY name
    """).fetchall()

    return render_template("leads.html",
        leads=leads, stages=LEAD_STAGES, stage_labels=LEAD_STAGE_LABELS,
        stage_badges=LEAD_STAGE_BADGES, stage_counts=stage_counts,
        stage_values=stage_values, active_count=active_count, total_count=total_count,
        pipeline_value=pipeline_value, filtered_total_value=filtered_total_value,
        unassigned_count=unassigned_count,
        due_count=due_count, today_count=today_count, sales_people=sales_people,
        stage_filter=stage_filter, assigned_filter=assigned_filter,
        due_filter=due_filter, search=search, now_str=now_str, today_str=today_str)


@app.route("/leads/add", methods=["POST"])
@login_required
@require_perm("create_lead")
def lead_add():
    company_name = request.form.get("company_name", "").strip()
    if not company_name:
        flash("Lead name is required.", "error")
        return redirect(url_for("leads_list"))
    stage = request.form.get("stage", "enquiry").strip()
    if stage not in LEAD_STAGE_VALUES:
        stage = "enquiry"
    assigned_to = request.form.get("assigned_to", 0, type=int)
    try:
        estimated_value = float(request.form.get("estimated_value", "0") or 0)
    except ValueError:
        estimated_value = 0
    db = get_db()
    cursor = db.execute("""
        INSERT INTO leads (company_name, contact_person, email, phone, source, stage,
            estimated_value, requirement, notes, assigned_to, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        company_name,
        request.form.get("contact_person", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("phone", "").strip(),
        request.form.get("source", "").strip(),
        stage,
        estimated_value,
        request.form.get("requirement", "").strip(),
        request.form.get("notes", "").strip(),
        assigned_to,
        session["user_id"],
    ))
    lead_id = cursor.lastrowid
    db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                  VALUES (?,?,?,?,?)""", (lead_id, "", stage, "Lead created", session["user_id"]))

    followup_at = parse_lead_followup_at(
        request.form.get("followup_date", ""), request.form.get("followup_time", ""))
    followup_note = request.form.get("followup_note", "").strip()
    if followup_at and followup_note:
        db.execute("""INSERT INTO lead_followups (lead_id, due_at, note, created_by)
                      VALUES (?,?,?,?)""", (lead_id, followup_at, followup_note, session["user_id"]))
        refresh_lead_next_followup(db, lead_id)
    db.commit()
    log_activity(db, session["user_id"], "created lead", "lead", company_name,
                 task_description=request.form.get("requirement", "").strip())
    db.commit()
    flash(f"Lead '{company_name}' added.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>")
@login_required
@require_perm("view_leads")
def lead_detail(lead_id):
    db = get_db()
    lead = db.execute("""
        SELECT l.*, e.name as assigned_name, c.name as converted_client_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        LEFT JOIN clients c ON l.converted_client_id = c.id
        WHERE l.id=?
    """, (lead_id,)).fetchone()
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    followups = db.execute("""
        SELECT lf.*, e.name as created_by_name
        FROM lead_followups lf
        LEFT JOIN employees e ON lf.created_by = e.id
        WHERE lf.lead_id=?
        ORDER BY CASE WHEN lf.status='pending' THEN 0 ELSE 1 END, lf.due_at ASC
    """, (lead_id,)).fetchall()
    history = db.execute("""
        SELECT h.*, e.name as changed_by_name
        FROM lead_stage_history h
        LEFT JOIN employees e ON h.changed_by = e.id
        WHERE h.lead_id=?
        ORDER BY h.created_at DESC, h.id DESC
    """, (lead_id,)).fetchall()
    sales_people = db.execute("""
        SELECT id, name, role FROM employees
        WHERE role IN ('sales','manager','admin','superadmin')
        ORDER BY name
    """).fetchall()
    now = get_tz_now()
    stage_position = {stage: idx for idx, (stage, _) in enumerate(LEAD_STAGES)}
    return render_template("lead_detail.html",
        lead=lead, followups=followups, history=history, sales_people=sales_people,
        stages=LEAD_STAGES, stage_labels=LEAD_STAGE_LABELS,
        stage_badges=LEAD_STAGE_BADGES, today=now.date().isoformat(),
        now_str=now.strftime("%Y-%m-%d %H:%M"), today_str=now.date().isoformat(),
        stage_position=stage_position, current_stage_position=stage_position.get(lead["stage"], 0))


@app.route("/leads/<int:lead_id>/edit", methods=["POST"])
@login_required
@require_perm("edit_lead")
def lead_edit(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    company_name = request.form.get("company_name", "").strip()
    if not company_name:
        flash("Lead name is required.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    stage = request.form.get("stage", lead["stage"]).strip()
    if stage not in LEAD_STAGE_VALUES:
        stage = lead["stage"]
    if stage == "converted" and not lead["converted_client_id"]:
        stage = lead["stage"]
        flash("Use Convert to Client to move a lead into Converted.", "error")
    try:
        estimated_value = float(request.form.get("estimated_value", "0") or 0)
    except ValueError:
        estimated_value = 0
    db.execute("""
        UPDATE leads SET company_name=?, contact_person=?, email=?, phone=?, source=?,
            stage=?, estimated_value=?, requirement=?, notes=?, assigned_to=?,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (
        company_name,
        request.form.get("contact_person", "").strip(),
        request.form.get("email", "").strip(),
        request.form.get("phone", "").strip(),
        request.form.get("source", "").strip(),
        stage,
        estimated_value,
        request.form.get("requirement", "").strip(),
        request.form.get("notes", "").strip(),
        request.form.get("assigned_to", 0, type=int),
        lead_id,
    ))
    if stage != lead["stage"]:
        db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                      VALUES (?,?,?,?,?)""",
                   (lead_id, lead["stage"], stage, request.form.get("stage_note", "").strip(), session["user_id"]))
    db.commit()
    log_activity(db, session["user_id"], "edited lead", "lead", company_name)
    db.commit()
    flash("Lead updated.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/followups/add", methods=["POST"])
@login_required
@require_perm("manage_lead_followups")
def lead_followup_add(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    due_at = parse_lead_followup_at(request.form.get("followup_date", ""), request.form.get("followup_time", ""))
    note = request.form.get("note", "").strip()
    if not due_at or not note:
        flash("Follow-up date/time and note are required.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    db.execute("INSERT INTO lead_followups (lead_id, due_at, note, created_by) VALUES (?,?,?,?)",
               (lead_id, due_at, note, session["user_id"]))
    refresh_lead_next_followup(db, lead_id)
    db.commit()
    flash("Follow-up reminder added.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/followups/<int:followup_id>/complete", methods=["POST"])
@login_required
@require_perm("manage_lead_followups")
def lead_followup_complete(followup_id):
    db = get_db()
    followup = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    if not followup:
        flash("Follow-up not found.", "error")
        return redirect(url_for("leads_list"))
    status = request.form.get("status", "done").strip()
    if status not in ("done", "cancelled"):
        status = "done"
    db.execute("""UPDATE lead_followups SET status=?, completed_at=? WHERE id=?""",
               (status, get_tz_now().strftime("%Y-%m-%d %H:%M"), followup_id))
    refresh_lead_next_followup(db, followup["lead_id"])
    db.commit()
    flash("Follow-up updated.", "success")
    return redirect(url_for("lead_detail", lead_id=followup["lead_id"]))


@app.route("/leads/<int:lead_id>/convert", methods=["POST"])
@login_required
@require_perm("convert_lead")
def lead_convert(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads_list"))
    if lead["converted_client_id"]:
        flash("Lead is already converted.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    client_name = request.form.get("client_name", "").strip() or lead["company_name"]
    notes = request.form.get("client_notes", "").strip()
    if not notes:
        notes = f"Converted from lead #{lead_id}."
        if lead["requirement"]:
            notes += f"\nRequirement: {lead['requirement']}"
        if lead["notes"]:
            notes += f"\nLead notes: {lead['notes']}"
    cursor = db.execute("""
        INSERT INTO clients (name, contact_person, email, phone, address, notes)
        VALUES (?,?,?,?,?,?)
    """, (client_name, lead["contact_person"], lead["email"], lead["phone"], "", notes))
    client_id = cursor.lastrowid
    converted_at = get_tz_now().strftime("%Y-%m-%d %H:%M")
    db.execute("""
        UPDATE leads SET stage='converted', converted_client_id=?, converted_at=?,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (client_id, converted_at, lead_id))
    db.execute("UPDATE lead_followups SET status='cancelled', completed_at=? WHERE lead_id=? AND status='pending'",
               (converted_at, lead_id))
    db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                  VALUES (?,?,?,?,?)""",
               (lead_id, lead["stage"], "converted", f"Converted to client #{client_id}", session["user_id"]))
    refresh_lead_next_followup(db, lead_id)
    db.commit()
    log_activity(db, session["user_id"], "converted lead", "lead", lead["company_name"],
                 notes=f"Client #{client_id}")
    db.commit()
    flash(f"Lead converted to client '{client_name}'.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
@login_required
@require_perm("delete_lead")
def lead_delete(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead:
        db.execute("DELETE FROM lead_followups WHERE lead_id=?", (lead_id,))
        db.execute("DELETE FROM lead_stage_history WHERE lead_id=?", (lead_id,))
        db.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        db.commit()
        flash(f"Lead '{lead['company_name']}' deleted.", "success")
    return redirect(url_for("leads_list"))


# ══════════════════════════════════════════════════════════
# ══  Projects (managed by admin/superadmin/subadmin) ═════
# ══════════════════════════════════════════════════════════


@app.route("/projects")
@login_required
@require_perm("view_projects")
def projects_list():
    db = get_db()
    projects = db.execute("""
        SELECT p.*, COALESCE(c.name, '') as client_name
        FROM projects p
        LEFT JOIN clients c ON p.client_id = c.id
        ORDER BY COALESCE(c.name, 'zzz'), p.name
    """).fetchall()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    role = session.get("role", "employee")
    return render_template("projects.html", projects=projects, clients=clients, role=role)


@app.route("/projects/add", methods=["POST"])
@login_required
@require_perm("create_project")
def project_add():
    client_id = request.form.get("client_id", 0, type=int)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    services = request.form.get("services", "").strip()
    if name:
        db = get_db()
        db.execute(
            "INSERT INTO projects (client_id, name, description, services) VALUES (?,?,?,?)",
            (client_id, name, description, services),
        )
        db.commit()
        flash(f"Project '{name}' added.", "success")
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:project_id>/edit", methods=["POST"])
@login_required
@require_perm("edit_project")
def project_edit(project_id):
    db = get_db()
    client_id = request.form.get("client_id", 0, type=int)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    services = request.form.get("services", "").strip()
    status = request.form.get("status", "active")
    if name:
        db.execute("""UPDATE projects SET client_id=?, name=?, description=?, services=?, status=?
                      WHERE id=?""", (client_id, name, description, services, status, project_id))
        db.commit()
        flash("Project updated.", "success")
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
@require_perm("delete_project")
def project_delete(project_id):
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.commit()
    flash("Project deleted.", "success")
    return redirect(url_for("projects_list"))


# ── Project Links Management ──────────────────────────


@app.route("/projects/<int:project_id>/links/add", methods=["POST"])
@login_required
@require_perm("manage_project_links")
def project_add_link(project_id):
    import json as _json
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("projects_list"))
    link_title = request.form.get("link_title", "").strip()
    link_url = request.form.get("link_url", "").strip()
    if link_title and link_url:
        try:
            links = _json.loads(project["project_links"] or "[]")
        except Exception:
            links = []
        links.append({"title": link_title, "url": link_url})
        db.execute("UPDATE projects SET project_links=? WHERE id=?", (_json.dumps(links), project_id))
        db.commit()
        flash("Link added.", "success")
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:project_id>/links/<int:link_idx>/delete", methods=["POST"])
@login_required
@require_perm("manage_project_links")
def project_delete_link(project_id, link_idx):
    import json as _json
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("projects_list"))
    try:
        links = _json.loads(project["project_links"] or "[]")
    except Exception:
        links = []
    if 0 <= link_idx < len(links):
        links.pop(link_idx)
        db.execute("UPDATE projects SET project_links=? WHERE id=?", (_json.dumps(links), project_id))
        db.commit()
        flash("Link removed.", "success")
    return redirect(url_for("projects_list"))


# ══════════════════════════════════════════════════════════
# ══  Backlog Tasks  ══════════════════════════════════════
# ══════════════════════════════════════════════════════════


@app.route("/backlog/add", methods=["POST"])
@login_required
def add_backlog():
    emp_id = session["user_id"]
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    priority = request.form.get("priority", "medium").strip()
    if title:
        db = get_db()
        db.execute(
            "INSERT INTO backlog_tasks (employee_id, title, description, drive_link, project_id, priority) VALUES (?,?,?,?,?,?)",
            (emp_id, title, description, drive_link, project_id, priority),
        )
        log_activity(db, emp_id, "created", "backlog", title, description)
        db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/backlog/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_backlog(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM backlog_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if task:
        db.execute("DELETE FROM backlog_tasks WHERE id=?", (task_id,))
        log_activity(db, session["user_id"], "deleted", "backlog", task["title"])
        db.commit()
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/backlog/<int:task_id>/convert/recurring", methods=["POST"])
@login_required
def convert_backlog_to_recurring(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM backlog_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("my_dashboard"))
    frequency = request.form.get("frequency", "daily").strip()
    if frequency == "weekly":
        days_list = request.form.getlist("frequency_days_weekly")
        if not days_list:
            days_list = [request.form.get("frequency_day_weekly", "0")]
        frequency_days = ",".join(d for d in days_list if d.strip().isdigit())
        frequency_day = int(days_list[0]) if days_list and days_list[0].strip().isdigit() else 0
    else:
        days_input = request.form.get("frequency_days_monthly", "").strip()
        if days_input:
            frequency_days = ",".join(x.strip() for x in days_input.split(",") if x.strip().isdigit() and 1 <= int(x.strip()) <= 31)
            first_day = next((int(x.strip()) for x in days_input.split(",") if x.strip().isdigit()), 1)
            frequency_day = first_day
        else:
            frequency_day = request.form.get("frequency_day_monthly", request.form.get("frequency_day", 1), type=int)
            frequency_days = str(frequency_day)
    scheduled_time = request.form.get("scheduled_time", "").strip()
    frequency_month = request.form.get("frequency_month", 0, type=int)
    db.execute(
        "INSERT INTO recurring_tasks (employee_id, title, description, drive_link, project_id, frequency, frequency_day, frequency_days, frequency_month, scheduled_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task["employee_id"], task["title"], task["description"], task["drive_link"], task["project_id"], frequency, frequency_day, frequency_days, frequency_month, scheduled_time),
    )
    db.execute("DELETE FROM backlog_tasks WHERE id=?", (task_id,))
    log_activity(db, session["user_id"], "converted backlog to recurring", "recurring", task["title"])
    db.commit()
    flash(f"'{task['title']}' converted to recurring task.", "success")
    return redirect(request.referrer or url_for("my_dashboard"))


@app.route("/backlog/<int:task_id>/convert/oneoff", methods=["POST"])
@login_required
def convert_backlog_to_oneoff(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM backlog_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("my_dashboard"))
    db.execute(
        "INSERT INTO oneoff_tasks (employee_id, title, description, drive_link, project_id) VALUES (?,?,?,?,?)",
        (task["employee_id"], task["title"], task["description"], task["drive_link"], task["project_id"]),
    )
    db.execute("DELETE FROM backlog_tasks WHERE id=?", (task_id,))
    log_activity(db, session["user_id"], "converted backlog to one-time", "oneoff", task["title"])
    db.commit()
    flash(f"\'{task['title']}\' converted to one-time task.", "success")
    return redirect(request.referrer or url_for("my_dashboard"))


# ── Admin: Backlog Management ──────────────────────────


@app.route("/admin/employee/<int:emp_id>/backlog/add", methods=["POST"])
@admin_required
def admin_add_backlog(emp_id):
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    drive_link = request.form.get("drive_link", "").strip()
    project_id = request.form.get("project_id", 0, type=int)
    priority = request.form.get("priority", "medium").strip()
    if title:
        db = get_db()
        db.execute(
            "INSERT INTO backlog_tasks (employee_id, title, description, drive_link, project_id, priority) VALUES (?,?,?,?,?,?)",
            (emp_id, title, description, drive_link, project_id, priority),
        )
        log_activity(db, emp_id, "created (by admin)", "backlog", title, description)
        db.commit()
    return redirect(url_for("admin_view_employee", emp_id=emp_id))


@app.route("/admin/employee/<int:emp_id>/backlog/<int:task_id>/delete", methods=["POST"])
@admin_required
def admin_delete_backlog(emp_id, task_id):
    db = get_db()
    db.execute("DELETE FROM backlog_tasks WHERE id=? AND employee_id=?", (task_id, emp_id))
    db.commit()
    flash("Backlog task deleted.", "success")
    return redirect(url_for("admin_view_employee", emp_id=emp_id))


# ══════════════════════════════════════════════════════════
# ══  SUPER ADMIN: Invoice Management  ════════════════════
# ══════════════════════════════════════════════════════════


@app.route("/invoices")
@login_required
@require_perm("view_invoices")
def invoices_list():
    db = get_db()
    client_filter = request.args.get("client", 0, type=int)
    status_filter = request.args.get("status", "")

    where_clauses = []
    params = []
    if client_filter:
        where_clauses.append("i.client_id = ?")
        params.append(client_filter)
    if status_filter:
        where_clauses.append("i.status = ?")
        params.append(status_filter)
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    invoices = db.execute(f"""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name
        FROM invoices i
        LEFT JOIN clients c ON i.client_id = c.id
        {where_sql}
        ORDER BY i.invoice_date DESC
    """, params).fetchall()

    # Monthly summary with dual currency
    monthly_summary = {}
    for inv in invoices:
        month_key = inv["invoice_date"][:7] if inv["invoice_date"] else "Unknown"
        if month_key not in monthly_summary:
            monthly_summary[month_key] = {
                "paid_inr": 0, "paid_usd": 0,
                "pending_inr": 0, "pending_usd": 0,
                "total_inr": 0, "total_usd": 0,
                "count": 0
            }
        s = monthly_summary[month_key]
        s["count"] += 1
        currency = inv["currency"] or "INR"
        total = inv["total"] or 0
        if currency == "USD":
            s["total_usd"] += total
            if inv["status"] == "paid":
                s["paid_usd"] += total
            else:
                s["pending_usd"] += total
        else:
            s["total_inr"] += total
            if inv["status"] == "paid":
                s["paid_inr"] += total
            else:
                s["pending_inr"] += total

    usd_to_inr = float(get_app_setting("usd_to_inr_rate", "83.50") or "83.50")

    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    # Build currency symbol map
    currency_symbols = {}
    try:
        for c in db.execute("SELECT code, symbol FROM currencies").fetchall():
            currency_symbols[c["code"]] = c["symbol"]
    except Exception:
        pass
    return render_template("invoices.html", invoices=invoices, clients=clients,
                           client_filter=client_filter, status_filter=status_filter,
                           monthly_summary=monthly_summary, usd_to_inr=usd_to_inr,
                           currency_symbols=currency_symbols)


@app.route("/invoices/new")
@superadmin_required
def invoice_new():
    db = get_db()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    company_profiles = db.execute("SELECT * FROM company_profiles ORDER BY is_default DESC, name").fetchall()
    count = db.execute("SELECT COUNT(*) as cnt FROM invoices").fetchone()["cnt"]
    inv_number = f"INV-{get_tz_now().date().strftime('%Y%m')}-{count + 1:04d}"
    clist = get_currencies_list()
    default_sym = next((c["symbol"] for c in clist if c["is_default"]), "₹")
    return render_template("invoice_form.html", invoice=None, currencies=clist, clients=clients,
                           company_profiles=company_profiles, currency_sym=default_sym, default_currency_sym=default_sym,
                           inv_number=inv_number, today=get_tz_now().date().isoformat(), items=[])


@app.route("/invoices/create", methods=["POST"])
@superadmin_required
def invoice_create():
    db = get_db()
    client_id = request.form.get("client_id", 0, type=int)
    invoice_number = request.form.get("invoice_number", "").strip()
    invoice_date = request.form.get("invoice_date", get_tz_now().date().isoformat())
    due_date = request.form.get("due_date", "")
    remarks = request.form.get("remarks", "").strip()
    discount_percent = request.form.get("discount_percent", 0, type=float)
    billing_frequency = request.form.get("billing_frequency", 0, type=int)
    billing_days = request.form.get("billing_days", "").strip()
    # Sanitize billing_days: keep only valid 1-31 day numbers, comma-separated
    if billing_days:
        valid_days = []
        for x in billing_days.split(","):
            x = x.strip()
            if x.isdigit() and 1 <= int(x) <= 31:
                valid_days.append(x)
        billing_days = ",".join(valid_days)
    auto_generate = 1 if billing_frequency > 0 else 0

    descriptions = request.form.getlist("item_description[]")
    quantities = request.form.getlist("item_quantity[]")
    unit_prices = request.form.getlist("item_price[]")

    subtotal = 0
    items_data = []
    for i in range(len(descriptions)):
        desc = descriptions[i].strip()
        if not desc:
            continue
        qty = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
        price = float(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
        item_total = qty * price
        subtotal += item_total
        items_data.append((desc, qty, price, item_total))

    discount_amount = subtotal * (discount_percent / 100)
    total = subtotal - discount_amount

    next_gen = None
    if auto_generate and billing_frequency > 0:
        base = date.fromisoformat(invoice_date)
        nm = base.month + billing_frequency
        ny = base.year + (nm - 1) // 12
        nm = ((nm - 1) % 12) + 1
        try:
            next_gen = date(ny, nm, base.day).isoformat()
        except ValueError:
            import calendar as cal_mod
            last_day = cal_mod.monthrange(ny, nm)[1]
            next_gen = date(ny, nm, last_day).isoformat()

    currency = request.form.get("currency", get_app_setting("default_currency", "INR"))
    company_profile_id = request.form.get("company_profile_id", 0, type=int)
    drive_link = request.form.get("drive_link", "").strip()

    cursor = db.execute("""
        INSERT INTO invoices (client_id, invoice_number, invoice_date, due_date, subtotal,
            discount_percent, discount_amount, total, remarks, status, billing_frequency,
            auto_generate, next_generate_date, currency, company_profile_id, drive_link)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (client_id, invoice_number, invoice_date, due_date, subtotal,
          discount_percent, discount_amount, total, remarks, "draft",
          billing_frequency, auto_generate, next_gen, currency, company_profile_id, drive_link))
    inv_id = cursor.lastrowid

    for desc, qty, price, item_total in items_data:
        db.execute(
            "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, total) VALUES (?,?,?,?,?)",
            (inv_id, desc, qty, price, item_total),
        )
    if billing_days:
        db.execute("UPDATE invoices SET billing_days=? WHERE id=?", (billing_days, inv_id))
    db.commit()
    flash(f"Invoice {invoice_number} created.", "success")
    return redirect(url_for("invoice_view", invoice_id=inv_id))


@app.route("/invoices/<int:invoice_id>")
@login_required
@require_perm("view_invoices")
def invoice_view(invoice_id):
    db = get_db()
    invoice = db.execute("""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name,
               COALESCE(c.contact_person, '') as contact_person,
               COALESCE(c.email, '') as email,
               COALESCE(c.phone, '') as phone,
               COALESCE(c.address, '') as address
        FROM invoices i LEFT JOIN clients c ON i.client_id = c.id
        WHERE i.id=?
    """, (invoice_id,)).fetchone()
    if not invoice:
        flash("Invoice not found.", "error")
        return redirect(url_for("invoices_list"))
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    generated = db.execute("""
        SELECT * FROM invoices WHERE template_invoice_id=? ORDER BY invoice_date DESC
    """, (invoice_id,)).fetchall()
    # Resolve company profile name
    profile_name = ""
    profile_id = invoice["company_profile_id"] if "company_profile_id" in invoice.keys() else 0
    if profile_id:
        profile = db.execute("SELECT name FROM company_profiles WHERE id=?", (profile_id,)).fetchone()
        if profile:
            profile_name = profile["name"]
    currency_sym = get_currency_symbol(invoice["currency"] or "INR")
    return render_template("invoice_view.html", invoice=invoice, items=items,
                           generated=generated, profile_name=profile_name, currency_sym=currency_sym)


@app.route("/invoices/<int:invoice_id>/edit")
@superadmin_required
def invoice_edit(invoice_id):
    db = get_db()
    invoice = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not invoice:
        return redirect(url_for("invoices_list"))
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    company_profiles = db.execute("SELECT * FROM company_profiles ORDER BY is_default DESC, name").fetchall()
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    clist = get_currencies_list()
    inv_cur = invoice["currency"] or "INR"
    cur_sym = get_currency_symbol(inv_cur)
    default_sym = next((c["symbol"] for c in clist if c["is_default"]), "₹")
    return render_template("invoice_form.html", invoice=invoice, currencies=clist, clients=clients,
                           company_profiles=company_profiles, currency_sym=cur_sym, default_currency_sym=default_sym,
                           inv_number=invoice["invoice_number"], today=get_tz_now().date().isoformat(), items=items)


@app.route("/invoices/<int:invoice_id>/update", methods=["POST"])
@superadmin_required
def invoice_update(invoice_id):
    db = get_db()
    client_id = request.form.get("client_id", 0, type=int)
    invoice_number = request.form.get("invoice_number", "").strip()
    invoice_date = request.form.get("invoice_date", get_tz_now().date().isoformat())
    due_date = request.form.get("due_date", "")
    remarks = request.form.get("remarks", "").strip()
    discount_percent = request.form.get("discount_percent", 0, type=float)
    billing_frequency = request.form.get("billing_frequency", 0, type=int)
    billing_days = request.form.get("billing_days", "").strip()
    # Sanitize billing_days: keep only valid 1-31 day numbers, comma-separated
    if billing_days:
        valid_days = []
        for x in billing_days.split(","):
            x = x.strip()
            if x.isdigit() and 1 <= int(x) <= 31:
                valid_days.append(x)
        billing_days = ",".join(valid_days)
    auto_generate = 1 if billing_frequency > 0 else 0

    descriptions = request.form.getlist("item_description[]")
    quantities = request.form.getlist("item_quantity[]")
    unit_prices = request.form.getlist("item_price[]")

    subtotal = 0
    items_data = []
    for i in range(len(descriptions)):
        desc = descriptions[i].strip()
        if not desc:
            continue
        qty = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
        price = float(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
        item_total = qty * price
        subtotal += item_total
        items_data.append((desc, qty, price, item_total))

    discount_amount = subtotal * (discount_percent / 100)
    total = subtotal - discount_amount

    next_gen = None
    if auto_generate and billing_frequency > 0:
        base = date.fromisoformat(invoice_date)
        nm = base.month + billing_frequency
        ny = base.year + (nm - 1) // 12
        nm = ((nm - 1) % 12) + 1
        try:
            next_gen = date(ny, nm, base.day).isoformat()
        except ValueError:
            import calendar as cal_mod
            last_day = cal_mod.monthrange(ny, nm)[1]
            next_gen = date(ny, nm, last_day).isoformat()

    currency = request.form.get("currency", get_app_setting("default_currency", "INR"))
    company_profile_id = request.form.get("company_profile_id", 0, type=int)
    drive_link = request.form.get("drive_link", "").strip()

    db.execute("""
        UPDATE invoices SET client_id=?, invoice_number=?, invoice_date=?, due_date=?,
            subtotal=?, discount_percent=?, discount_amount=?, total=?, remarks=?,
            billing_frequency=?, auto_generate=?, next_generate_date=?, currency=?, company_profile_id=?,
            drive_link=?
        WHERE id=?
    """, (client_id, invoice_number, invoice_date, due_date, subtotal,
          discount_percent, discount_amount, total, remarks,
          billing_frequency, auto_generate, next_gen, currency, company_profile_id, drive_link, invoice_id))

    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    for desc, qty, price, item_total in items_data:
        db.execute(
            "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, total) VALUES (?,?,?,?,?)",
            (invoice_id, desc, qty, price, item_total),
        )
    db.commit()
    flash("Invoice updated.", "success")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@superadmin_required
def invoice_delete(invoice_id):
    db = get_db()
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    db.commit()
    flash("Invoice deleted.", "success")
    return redirect(url_for("invoices_list"))


@app.route("/invoices/<int:invoice_id>/send", methods=["POST"])
@login_required
@require_perm("send_invoice")
def invoice_send(invoice_id):
    db = get_db()
    invoice = db.execute("""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name,
               COALESCE(c.contact_person, '') as contact_person,
               COALESCE(c.email, '') as email,
               COALESCE(c.phone, '') as phone,
               COALESCE(c.address, '') as address
        FROM invoices i LEFT JOIN clients c ON i.client_id = c.id WHERE i.id=?
    """, (invoice_id,)).fetchone()
    if not invoice:
        return redirect(url_for("invoices_list"))

    db.execute("UPDATE invoices SET status='sent' WHERE id=?", (invoice_id,))
    db.commit()

    acct_chat_id = get_accountant_chat_id()
    if acct_chat_id:
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
        currency_sym = get_currency_symbol(invoice["currency"] or "INR")
        company_name = get_app_setting("company_name", get_app_setting("app_name", "Our Company"))

        # Generate simple PDF-like HTML content as text file for Telegram
        msg = (f"<b>📄 Invoice from {company_name}</b>\n\n"
               f"Client: {invoice['client_name']}\n"
               f"Invoice #: {invoice['invoice_number']}\n"
               f"Amount: {currency_sym}{invoice['total']:.2f}\n"
               f"Currency: {invoice['currency'] or 'INR'}\n"
               f"Date: {invoice['invoice_date']}\n"
               f"Due: {invoice['due_date'] or 'N/A'}\n\n"
               f"Please process this invoice.")
        send_telegram(acct_chat_id, msg)
        flash("Invoice sent to accountant via Telegram.", "success")
    else:
        flash("Invoice marked as sent. Configure accountant chat ID in Settings to notify.", "success")

    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/mark-paid", methods=["POST"])
@superadmin_required
def invoice_mark_paid(invoice_id):
    db = get_db()
    invoice = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not invoice:
        return redirect(url_for("invoices_list"))

    if invoice["status"] == "paid":
        db.execute("UPDATE invoices SET status='sent', paid_date=NULL WHERE id=?", (invoice_id,))
        flash("Invoice marked as unpaid.", "success")
    else:
        paid_date = get_tz_now().date().isoformat()
        db.execute("UPDATE invoices SET status='paid', paid_date=? WHERE id=?", (paid_date, invoice_id))
        flash("Invoice marked as paid.", "success")
    db.commit()
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>/pdf")
@login_required
@require_perm("generate_invoice_pdf")
def invoice_pdf(invoice_id):
    db = get_db()
    invoice = db.execute("""
        SELECT i.*, COALESCE(c.name, 'No Client') as client_name,
               COALESCE(c.contact_person, '') as contact_person,
               COALESCE(c.email, '') as email,
               COALESCE(c.phone, '') as phone,
               COALESCE(c.address, '') as address
        FROM invoices i LEFT JOIN clients c ON i.client_id = c.id WHERE i.id=?
    """, (invoice_id,)).fetchone()
    if not invoice:
        return redirect(url_for("invoices_list"))
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()

    # White-label: use company profile if set, otherwise default settings
    profile_id = invoice["company_profile_id"] if "company_profile_id" in invoice.keys() else 0
    if profile_id:
        profile = db.execute("SELECT * FROM company_profiles WHERE id=?", (profile_id,)).fetchone()
        if profile:
            company = {"name": profile["name"], "logo_url": profile["logo_url"],
                       "address": profile["address"], "email": profile["email"], "phone": profile["phone"]}
        else:
            company = None
    else:
        company = None
    if not company:
        company = {
            "name": get_app_setting("company_name", get_app_setting("app_name", "Our Company")),
            "logo_url": get_app_setting("company_logo_url", ""),
            "address": get_app_setting("company_address", ""),
            "email": get_app_setting("company_email", ""),
            "phone": get_app_setting("company_phone", ""),
        }

    currency_sym = get_currency_symbol(invoice["currency"] or "INR")
    return render_template("invoice_pdf.html", invoice=invoice, items=items,
                           company=company, currency_sym=currency_sym)


# ── Settings ──────────────────────────────────────────


@app.route("/settings")
@superadmin_required
def settings_page():
    settings = {}
    for key in ["deploy_key", "timezone", "company_name", "company_logo_url",
                "company_address", "company_email", "company_phone",
                "accountant_chat_id", "accountant_name", "usd_to_inr_rate", "default_currency",
                "sales_telegram_chat_id", "lead_reminder_chat_id",
                "general_telegram_notifications_enabled", "telegram_general_attendance_enabled",
                "telegram_general_celebrations_enabled",
                "monthly_attendance_enabled", "monthly_attendance_chat_id",
                "daily_staff_report_enabled", "daily_staff_report_chat_id", "daily_staff_report_time",
                "app_name", "public_base_url", "primary_color", "app_logo_url",
                "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_enabled",
                "dark_mode", "telegram_bot_token", "telegram_group_chat_id"]:
        settings[key] = get_app_setting(key, "")
    db_token = get_app_setting("telegram_bot_token", "")
    tg_source = "Database" if db_token else ("Environment" if TELEGRAM_BOT_TOKEN else "Not Set")
    currencies = get_currencies_list()
    return render_template("settings.html",
                           settings=settings,
                           currencies=currencies,
                           telegram_active=is_telegram_enabled(),
                           telegram_source=tg_source,
                           app_version=APP_VERSION)


@app.route("/admin/settings/timezone", methods=["POST"])
@admin_required
def update_timezone():
    tz = request.form.get("timezone", "").strip()
    if tz:
        try:
            ZoneInfo(tz)
            set_app_setting("timezone", tz)
            flash(f"Timezone updated to {tz}.", "success")
        except Exception:
            flash("Invalid timezone.", "error")
    return redirect(request.referrer or url_for("settings_page"))


@app.route("/settings/update", methods=["POST"])
@superadmin_required
def update_settings():
    fields = ["company_name", "company_logo_url", "company_address", "company_email",
              "company_phone", "accountant_chat_id", "accountant_name",
              "sales_telegram_chat_id", "lead_reminder_chat_id",
              "general_telegram_notifications_enabled", "telegram_general_attendance_enabled",
              "telegram_general_celebrations_enabled",
              "monthly_attendance_enabled", "monthly_attendance_chat_id",
              "daily_staff_report_enabled", "daily_staff_report_chat_id", "daily_staff_report_time",
              "usd_to_inr_rate", "default_currency",
              "app_name", "public_base_url", "primary_color", "app_logo_url",
              "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_enabled",
              "dark_mode", "telegram_bot_token", "telegram_group_chat_id"]
    # Only update fields that are actually present in the submitted form.
    # Forms on the settings page are split into multiple <form> blocks, and
    # some settings (like app_logo_url) are set via separate uploaders — so
    # overwriting absent fields would wipe them.
    updated = 0
    for field in fields:
        if field in request.form:
            val = request.form.get(field, "").strip()
            set_app_setting(field, val)
            updated += 1
    db = get_db()
    log_activity(db, session["user_id"], "updated settings", "admin", f"Saved {updated} field(s)")
    db.commit()
    flash("Settings updated.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/lead-intake")
@superadmin_required
def lead_intake_settings():
    db = get_db()
    sources = db.execute("""
        SELECT s.*, e.name as assigned_name,
               COUNT(ev.id) as event_count,
               SUM(CASE WHEN ev.status='created' THEN 1 ELSE 0 END) as created_count,
               MAX(ev.created_at) as last_event_at
        FROM lead_intake_sources s
        LEFT JOIN employees e ON s.assigned_to = e.id
        LEFT JOIN lead_intake_events ev ON ev.source_id = s.id
        GROUP BY s.id
        ORDER BY CASE s.source_type
            WHEN 'website' THEN 1 WHEN 'telegram' THEN 2 WHEN 'whatsapp' THEN 3 ELSE 9 END, s.name
    """).fetchall()
    sales_people = db.execute("""
        SELECT id, name, role FROM employees
        WHERE role IN ('sales','manager','admin','superadmin')
        ORDER BY name
    """).fetchall()
    public_base = get_app_setting("public_base_url", "").rstrip("/")
    return render_template(
        "lead_intake_settings.html",
        sources=[lead_intake_source_dict(source) for source in sources],
        sales_people=sales_people,
        stages=LEAD_STAGES,
        public_base=public_base,
    )


@app.route("/settings/lead-intake/sources", methods=["POST"])
@superadmin_required
def lead_intake_source_add():
    source_type = request.form.get("source_type", "website").strip()
    if source_type not in ("website", "telegram", "whatsapp"):
        source_type = "website"
    name = request.form.get("name", "").strip() or f"{source_type.title()} Lead Source"
    default_stage = request.form.get("default_stage", "enquiry").strip()
    if default_stage not in LEAD_STAGE_VALUES or default_stage == "converted":
        default_stage = "enquiry"
    db = get_db()
    db.execute("""
        INSERT INTO lead_intake_sources
            (name, source_type, token, enabled, default_stage, assigned_to, allowed_origins, success_message)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        name,
        source_type,
        secrets.token_urlsafe(24),
        1 if request.form.get("enabled", "1") == "1" else 0,
        default_stage,
        request.form.get("assigned_to", 0, type=int),
        request.form.get("allowed_origins", "*").strip() or "*",
        request.form.get("success_message", "Thanks, we received your enquiry.").strip(),
    ))
    db.commit()
    flash("Lead intake source added.", "success")
    return redirect(url_for("lead_intake_settings"))


@app.route("/settings/lead-intake/sources/<int:source_id>/update", methods=["POST"])
@superadmin_required
def lead_intake_source_update(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        flash("Lead intake source not found.", "error")
        return redirect(url_for("lead_intake_settings"))
    default_stage = request.form.get("default_stage", source["default_stage"]).strip()
    if default_stage not in LEAD_STAGE_VALUES or default_stage == "converted":
        default_stage = "enquiry"
    source_type = request.form.get("source_type", source["source_type"]).strip()
    if source_type not in ("website", "telegram", "whatsapp"):
        source_type = source["source_type"]
    db.execute("""
        UPDATE lead_intake_sources
        SET name=?, source_type=?, enabled=?, default_stage=?, assigned_to=?,
            allowed_origins=?, success_message=?, updated_at=datetime('now','localtime')
        WHERE id=?
    """, (
        request.form.get("name", source["name"]).strip() or source["name"],
        source_type,
        1 if request.form.get("enabled") == "1" else 0,
        default_stage,
        request.form.get("assigned_to", 0, type=int),
        request.form.get("allowed_origins", "*").strip() or "*",
        request.form.get("success_message", "").strip(),
        source_id,
    ))
    db.commit()
    flash("Lead intake source updated.", "success")
    return redirect(url_for("lead_intake_settings"))


@app.route("/settings/lead-intake/sources/<int:source_id>/rotate", methods=["POST"])
@superadmin_required
def lead_intake_source_rotate(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        flash("Lead intake source not found.", "error")
        return redirect(url_for("lead_intake_settings"))
    db.execute("UPDATE lead_intake_sources SET token=?, updated_at=datetime('now','localtime') WHERE id=?",
               (secrets.token_urlsafe(24), source_id))
    db.commit()
    flash("Lead intake token rotated. Update any external forms or webhooks using the old URL.", "success")
    return redirect(url_for("lead_intake_settings"))


@app.route("/settings/lead-intake/sources/<int:source_id>/delete", methods=["POST"])
@superadmin_required
def lead_intake_source_delete(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        flash("Lead intake source not found.", "error")
        return redirect(url_for("lead_intake_settings"))
    db.execute("DELETE FROM lead_intake_sources WHERE id=?", (source_id,))
    db.execute("UPDATE lead_intake_events SET source_id=0 WHERE source_id=?", (source_id,))
    db.commit()
    flash("Lead intake source deleted.", "success")
    return redirect(url_for("lead_intake_settings"))


@app.route("/settings/lead-intake/sources/<int:source_id>/telegram-webhook", methods=["POST"])
@superadmin_required
def lead_intake_set_telegram_webhook(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, source_type="telegram", include_disabled=True)
    if not source:
        flash("Telegram intake source not found.", "error")
        return redirect(url_for("lead_intake_settings"))
    bot_token = get_telegram_bot_token()
    webhook_url = lead_intake_public_url(source)
    if not bot_token or not webhook_url.startswith("https://"):
        flash("Telegram bot token and HTTPS Public Base URL are required.", "error")
        return redirect(url_for("lead_intake_settings"))
    try:
        import urllib.request
        body = json.dumps({
            "url": webhook_url,
            "allowed_updates": ["message", "edited_message"],
            "secret_token": source["token"],
            "drop_pending_updates": False,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if result.get("ok"):
            flash("Telegram lead webhook connected.", "success")
        else:
            flash(f"Telegram webhook failed: {result.get('description', 'Unknown error')}", "error")
    except Exception as e:
        flash(f"Telegram webhook failed: {e}", "error")
    return redirect(url_for("lead_intake_settings"))


@app.route("/settings/company-profiles")
@superadmin_required
def company_profiles():
    db = get_db()
    profiles = db.execute("SELECT * FROM company_profiles ORDER BY is_default DESC, name").fetchall()
    return render_template("company_profiles.html", profiles=profiles)


# ── Currency Management ──────────────────────────────────


@app.route("/settings/currencies")
@superadmin_required
def currencies_page():
    db = get_db()
    currencies = db.execute("SELECT * FROM currencies ORDER BY is_default DESC, code").fetchall()
    return render_template("currencies.html", currencies=currencies)


@app.route("/settings/currencies/add", methods=["POST"])
@superadmin_required
def add_currency():
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    symbol = request.form.get("symbol", "").strip()
    if not code or not name or not symbol:
        flash("All fields are required.", "error")
        return redirect(url_for("currencies_page"))
    db = get_db()
    try:
        db.execute("INSERT INTO currencies (code, name, symbol) VALUES (?,?,?)", (code, name, symbol))
        db.commit()
        flash(f"Currency {code} added.", "success")
    except Exception:
        flash(f"Currency {code} already exists.", "error")
    return redirect(url_for("currencies_page"))


@app.route("/settings/currencies/<int:cid>/delete", methods=["POST"])
@superadmin_required
def delete_currency(cid):
    db = get_db()
    db.execute("DELETE FROM currencies WHERE id=? AND is_default=0", (cid,))
    db.commit()
    flash("Currency removed.", "success")
    return redirect(url_for("currencies_page"))


@app.route("/settings/currencies/<int:cid>/default", methods=["POST"])
@superadmin_required
def set_default_currency(cid):
    db = get_db()
    db.execute("UPDATE currencies SET is_default=0")
    db.execute("UPDATE currencies SET is_default=1 WHERE id=?", (cid,))
    db.commit()
    flash("Default currency updated.", "success")
    return redirect(url_for("currencies_page"))


@app.route("/settings/currencies/refresh-rates", methods=["POST"])
@superadmin_required
def refresh_currency_rates():
    success, msg = fetch_exchange_rates()
    if success:
        flash(f"Exchange rates updated. {msg}", "success")
    else:
        flash(f"Failed to fetch rates: {msg}", "error")
    return redirect(url_for("currencies_page"))


@app.route("/settings/currencies/<int:cid>/rate", methods=["POST"])
@superadmin_required
def update_currency_rate(cid):
    rate = request.form.get("rate", "").strip()
    try:
        rate_val = float(rate)
        if rate_val <= 0:
            raise ValueError
        db = get_db()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute("UPDATE currencies SET exchange_rate=?, rates_updated_at=? WHERE id=?",
                  (round(rate_val, 6), now_str + " (manual)", cid))
        db.commit()
        flash("Rate updated.", "success")
    except (ValueError, TypeError):
        flash("Invalid rate value.", "error")
    return redirect(url_for("currencies_page"))


# ── Profile Image Upload ─────────────────────────────────


@app.route("/profile/upload-image", methods=["POST"])
@login_required
def upload_profile_image():
    if "profile_image" not in request.files:
        flash("No image selected.", "error")
        return redirect(url_for("my_profile"))
    file = request.files["profile_image"]
    if file.filename == "":
        flash("No image selected.", "error")
        return redirect(url_for("my_profile"))
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        flash("Only JPG, PNG, GIF, WebP images allowed.", "error")
        return redirect(url_for("my_profile"))
    import base64
    img_data = file.read()
    if len(img_data) > 2 * 1024 * 1024:
        flash("Image must be under 2MB.", "error")
        return redirect(url_for("my_profile"))
    mime = f"image/{ext[1:]}"
    if ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    data_uri = f"data:{mime};base64,{base64.b64encode(img_data).decode()}"
    db = get_db()
    db.execute("UPDATE employees SET profile_image=? WHERE id=?", (data_uri, session["user_id"]))
    db.commit()
    flash("Profile image updated.", "success")
    return redirect(url_for("my_profile"))


@app.route("/settings/upload-logo", methods=["POST"])
@superadmin_required
def upload_app_logo():
    if "logo_file" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("settings_page"))
    file = request.files["logo_file"]
    if file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("settings_page"))
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        flash("Only JPG, PNG, GIF, WebP, SVG images allowed.", "error")
        return redirect(url_for("settings_page"))
    import base64 as b64mod
    img_data = file.read()
    if len(img_data) > 2 * 1024 * 1024:
        flash("Logo must be under 2MB.", "error")
        return redirect(url_for("settings_page"))
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
    mime = mime_map.get(ext, f"image/{ext[1:]}")
    data_uri = f"data:{mime};base64,{b64mod.b64encode(img_data).decode()}"
    set_app_setting("app_logo_url", data_uri)
    flash("Logo uploaded.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/company-profiles/add", methods=["POST"])
@superadmin_required
def company_profile_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Company name is required.", "error")
        return redirect(url_for("company_profiles"))
    db = get_db()
    db.execute(
        "INSERT INTO company_profiles (name, logo_url, address, email, phone) VALUES (?,?,?,?,?)",
        (name, request.form.get("logo_url", "").strip(),
         request.form.get("address", "").strip(),
         request.form.get("email", "").strip(),
         request.form.get("phone", "").strip()),
    )
    db.commit()
    flash(f"Company profile '{name}' added.", "success")
    return redirect(url_for("company_profiles"))


@app.route("/settings/company-profiles/<int:profile_id>/edit", methods=["POST"])
@superadmin_required
def company_profile_edit(profile_id):
    db = get_db()
    name = request.form.get("name", "").strip()
    if name:
        db.execute("""UPDATE company_profiles SET name=?, logo_url=?, address=?, email=?, phone=?
                      WHERE id=?""",
                   (name, request.form.get("logo_url", "").strip(),
                    request.form.get("address", "").strip(),
                    request.form.get("email", "").strip(),
                    request.form.get("phone", "").strip(), profile_id))
        db.commit()
        flash("Profile updated.", "success")
    return redirect(url_for("company_profiles"))


@app.route("/settings/company-profiles/<int:profile_id>/default", methods=["POST"])
@superadmin_required
def company_profile_set_default(profile_id):
    db = get_db()
    db.execute("UPDATE company_profiles SET is_default=0")
    db.execute("UPDATE company_profiles SET is_default=1 WHERE id=?", (profile_id,))
    db.commit()
    flash("Default profile updated.", "success")
    return redirect(url_for("company_profiles"))


@app.route("/settings/company-profiles/<int:profile_id>/delete", methods=["POST"])
@superadmin_required
def company_profile_delete(profile_id):
    db = get_db()
    db.execute("DELETE FROM company_profiles WHERE id=?", (profile_id,))
    db.commit()
    flash("Profile deleted.", "success")
    return redirect(url_for("company_profiles"))


# ── Employee Self-Edit Profile ──────────────────────────


@app.route("/profile", methods=["GET", "POST"])
@login_required
def my_profile():
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (session["user_id"],)).fetchone()
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        department = request.form.get("department", "").strip()
        birthday = request.form.get("birthday", "").strip()
        joining_date = request.form.get("joining_date", "").strip()
        wedding_anniversary = request.form.get("wedding_anniversary", "").strip()
        db.execute("""UPDATE employees SET email=?, phone=?, department=?, birthday=?, joining_date=?, wedding_anniversary=? WHERE id=?""",
                   (email, phone, department, birthday, joining_date, wedding_anniversary, session["user_id"]))
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("my_profile"))
    return render_template("profile.html", employee=emp)


# ── Task Comments ────────────────────────────────────────


@app.route("/task/<task_type>/<int:task_id>/comment", methods=["POST"])
@login_required
def add_comment(task_type, task_id):
    comment = request.form.get("comment", "").strip()
    if comment and task_type in ("recurring", "oneoff"):
        db = get_db()
        db.execute("INSERT INTO task_comments (task_id, task_type, employee_id, comment) VALUES (?,?,?,?)",
                   (task_id, task_type, session["user_id"], comment))
        db.commit()
    return redirect(request.referrer or url_for("today_view"))


# ── Notifications Center ────────────────────────────────


@app.route("/notifications")
@login_required
def notifications_view():
    db = get_db()
    notifs = db.execute(
        "SELECT * FROM notifications WHERE employee_id=? ORDER BY created_at DESC LIMIT 50",
        (session["user_id"],)).fetchall()
    # Mark all as read
    db.execute("UPDATE notifications SET read=1 WHERE employee_id=? AND read=0", (session["user_id"],))
    db.commit()
    return render_template("notifications.html", notifications=notifs)


# ── Data Export ──────────────────────────────────────────


@app.route("/admin/export-data")
@superadmin_required
def export_all_data():
    """Export all data as a single Excel file with multiple sheets."""
    import openpyxl
    wb = openpyxl.Workbook()
    db = get_db()

    tables = ["employees", "recurring_tasks", "oneoff_tasks", "clients", "projects",
              "invoices", "invoice_items", "attendance", "activity_log", "app_settings"]

    for i, table in enumerate(tables):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = table
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            headers = rows[0].keys()
            ws.append(list(headers))
            for row in rows:
                row_data = []
                for h in headers:
                    val = row[h]
                    if h == "password":
                        val = "***"
                    row_data.append(val)
                ws.append(row_data)
        else:
            ws.append([f"No data in {table}"])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"task_manager_export_{get_tz_now().date().isoformat()}.xlsx"
    return send_file(output, download_name=filename, as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Admin Dashboard ──────────────────────────────────────


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    db = get_db()
    now = get_tz_now()
    today = now.date().isoformat()

    # Attendance today
    employees = db.execute("SELECT * FROM employees WHERE role IN ('employee','sales','accountant','subadmin','manager') ORDER BY name").fetchall()
    attendance_today = {}
    for emp in employees:
        last = db.execute("SELECT action FROM attendance WHERE employee_id=? AND action_date=? ORDER BY id DESC LIMIT 1",
                         (emp["id"], today)).fetchone()
        attendance_today[emp["id"]] = last["action"] if last else "absent"

    checked_in = sum(1 for v in attendance_today.values() if v == "checkin")

    # Tasks stats
    total_recurring = db.execute("SELECT COUNT(*) as cnt FROM recurring_tasks WHERE active=1").fetchone()["cnt"]
    today_completions = db.execute("SELECT COUNT(*) as cnt FROM recurring_completions WHERE completion_date=?", (today,)).fetchone()["cnt"]
    pending_oneoff = db.execute("SELECT COUNT(*) as cnt FROM oneoff_tasks WHERE completed=0").fetchone()["cnt"]

    # Celebrations this week
    celebrations = []
    for emp in db.execute("SELECT * FROM employees").fetchall():
        for field, label in [("birthday", "Birthday"), ("joining_date", "Work Anniversary"), ("wedding_anniversary", "Wedding Anniversary")]:
            val = emp[field]
            if not val:
                continue
            try:
                mmdd = val[5:]
                for offset in range(7):
                    check = now.date() + timedelta(days=offset)
                    if check.strftime("%m-%d") == mmdd:
                        years = check.year - int(val[:4])
                        if label == "Birthday" or years > 0:
                            celebrations.append({"name": emp["name"], "type": label, "date": check.isoformat(), "days": offset, "years": years})
                        break
            except Exception:
                pass
    celebrations.sort(key=lambda x: x["days"])

    # Recent activity
    recent_activity = db.execute("""SELECT al.*, e.name as employee_name FROM activity_log al
        JOIN employees e ON al.employee_id = e.id ORDER BY al.created_at DESC LIMIT 10""").fetchall()

    # Unpaid invoices (superadmin only)
    unpaid = []
    if session.get("role") == "superadmin":
        unpaid = db.execute("SELECT SUM(total) as total, currency FROM invoices WHERE status != 'paid' GROUP BY currency").fetchall()

    # Build currency symbol map for template
    currency_symbols = {}
    try:
        for c in db.execute("SELECT code, symbol FROM currencies").fetchall():
            currency_symbols[c["code"]] = c["symbol"]
    except Exception:
        pass

    return render_template("admin_dashboard.html",
        employees=employees, attendance_today=attendance_today, checked_in=checked_in,
        total_employees=len(employees), total_recurring=total_recurring,
        today_completions=today_completions, pending_oneoff=pending_oneoff,
        celebrations=celebrations, recent_activity=recent_activity, unpaid=unpaid,
        currency_symbols=currency_symbols, today=today)


# ── Client Portal (read-only invoice view via token) ─────


@app.route("/invoices/<int:invoice_id>/share", methods=["POST"])
@superadmin_required
def share_invoice(invoice_id):
    db = get_db()
    token = secrets.token_urlsafe(32)
    db.execute("UPDATE invoices SET share_token=? WHERE id=?", (token, invoice_id))
    db.commit()
    flash(f"Share link created!", "success")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/portal/invoice/<token>")
def client_portal_invoice(token):
    if not token:
        return "Invalid link", 404
    db = get_db()
    invoice = db.execute("""SELECT i.*, COALESCE(c.name, 'No Client') as client_name,
        COALESCE(c.contact_person, '') as contact_person,
        COALESCE(c.email, '') as client_email,
        COALESCE(c.phone, '') as client_phone,
        COALESCE(c.address, '') as client_address FROM invoices i
        LEFT JOIN clients c ON i.client_id = c.id WHERE i.share_token=?""", (token,)).fetchone()
    if not invoice:
        return "Invoice not found", 404
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
    currency_sym = get_currency_symbol(invoice["currency"] or "INR")
    return render_template("client_portal.html", invoice=invoice, items=items, currency_sym=currency_sym)


# ── Admin: Reset Password (admin-initiated) ─────────────


@app.route("/admin/employee/<int:emp_id>/reset-password", methods=["POST"])
@login_required
@require_perm("reset_member_password")
def admin_reset_password(emp_id):
    db = get_db()
    temp_pw = secrets.token_hex(4)  # 8 char random password
    db.execute("UPDATE employees SET password=?, must_change_password=1 WHERE id=?",
               (hash_password(temp_pw), emp_id))
    db.commit()
    flash(f"Password reset. Temporary password: {temp_pw} (user must change on next login)", "success")
    return redirect(url_for("admin_home"))


# ── Admin: Shift Management ──────────────────────────


@app.route("/admin/shifts")
@login_required
@require_perm("manage_shifts")
def admin_shifts():
    db = get_db()
    shifts = db.execute("SELECT * FROM shifts ORDER BY start_time").fetchall()
    # Count employees per shift
    shift_counts = {}
    for s in shifts:
        cnt = db.execute("SELECT COUNT(*) as cnt FROM employees WHERE shift_id=?", (s["id"],)).fetchone()["cnt"]
        shift_counts[s["id"]] = cnt
    return render_template("admin_shifts.html", shifts=shifts, shift_counts=shift_counts)


@app.route("/admin/shifts/add", methods=["POST"])
@login_required
@require_perm("manage_shifts")
def admin_add_shift():
    name = request.form.get("name", "").strip()
    start_time = request.form.get("start_time", "09:00").strip()
    end_time = request.form.get("end_time", "18:00").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO shifts (name, start_time, end_time) VALUES (?,?,?)",
                   (name, start_time, end_time))
        db.commit()
        flash(f"Shift '{name}' added.", "success")
    return redirect(url_for("admin_shifts"))


@app.route("/admin/shifts/<int:shift_id>/edit", methods=["POST"])
@login_required
@require_perm("manage_shifts")
def admin_edit_shift(shift_id):
    name = request.form.get("name", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    if name:
        db = get_db()
        db.execute("UPDATE shifts SET name=?, start_time=?, end_time=? WHERE id=?",
                   (name, start_time, end_time, shift_id))
        db.commit()
        flash(f"Shift '{name}' updated.", "success")
    return redirect(url_for("admin_shifts"))


@app.route("/admin/shifts/<int:shift_id>/delete", methods=["POST"])
@login_required
@require_perm("manage_shifts")
def admin_delete_shift(shift_id):
    db = get_db()
    emp_count = db.execute("SELECT COUNT(*) as cnt FROM employees WHERE shift_id=?", (shift_id,)).fetchone()["cnt"]
    if emp_count > 0:
        flash(f"Cannot delete — {emp_count} member(s) assigned to this shift. Reassign them first.", "error")
    else:
        db.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
        db.commit()
        flash("Shift deleted.", "success")
    return redirect(url_for("admin_shifts"))


# ── Admin: Holiday Management ──────────────────────────


@app.route("/admin/holidays")
@login_required
@require_perm("manage_holidays")
def admin_holidays():
    db = get_db()
    today = get_tz_now().date()
    selected_month = request.args.get("month", today.strftime("%Y-%m")).strip()
    month_start = parse_year_month(selected_month) or today.replace(day=1)
    month_end = month_end_for(month_start)
    selected_month = month_start.strftime("%Y-%m")

    holiday_rows = db.execute("SELECT * FROM holidays ORDER BY holiday_date").fetchall()
    holidays = [holiday_display_row(row) for row in holiday_rows]
    month_holiday_rows = db.execute(
        "SELECT * FROM holidays WHERE holiday_date LIKE ? ORDER BY holiday_date",
        (f"{selected_month}-%",),
    ).fetchall()
    month_holidays = [holiday_display_row(row) for row in month_holiday_rows]
    month_holiday_by_date = {row["holiday_date"]: row for row in month_holidays}
    weekly_off = get_app_setting("weekly_off_days", "6")
    try:
        off_days = [int(x.strip()) for x in weekly_off.split(",") if x.strip()]
    except ValueError:
        off_days = [6]
    off_day_set = set(off_days)
    day_names_short = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    month_days = []
    current = month_start
    while current <= month_end:
        day_str = current.isoformat()
        holiday = month_holiday_by_date.get(day_str)
        month_days.append({
            "date": day_str,
            "day": current.day,
            "weekday": day_names_short[current.weekday()],
            "is_weekly_off": current.weekday() in off_day_set,
            "holiday_name": holiday["name"] if holiday else "",
        })
        current += timedelta(days=1)
    return render_template(
        "admin_holidays.html",
        holidays=holidays,
        month_holidays=month_holidays,
        month_days=month_days,
        selected_month=selected_month,
        off_days=off_days,
    )


@app.route("/admin/holidays/add", methods=["POST"])
@login_required
@require_perm("manage_holidays")
def admin_add_holiday():
    holiday_date = request.form.get("holiday_date", "").strip()
    name = request.form.get("name", "").strip()
    target_month = holiday_date[:7] if parse_iso_date(holiday_date) else None
    if not parse_iso_date(holiday_date):
        flash("Please choose a valid holiday date.", "error")
        return redirect(url_for("admin_holidays"))
    if not name:
        flash("Holiday name is required.", "error")
        return redirect(url_for("admin_holidays", month=target_month))

    db = get_db()
    try:
        db.execute("INSERT INTO holidays (holiday_date, name) VALUES (?,?)", (holiday_date, name))
        db.commit()
        flash(f"Holiday '{name}' on {holiday_date} added.", "success")
    except sqlite3.IntegrityError:
        flash(f"A holiday already exists on {holiday_date}.", "error")
    return redirect(url_for("admin_holidays", month=target_month))


@app.route("/admin/holidays/monthly", methods=["POST"])
@login_required
@require_perm("manage_holidays")
def admin_save_monthly_holidays():
    selected_month = request.form.get("year_month", "").strip()
    month_start = parse_year_month(selected_month)
    if not month_start:
        flash("Choose a valid month before saving holidays.", "error")
        return redirect(url_for("admin_holidays"))

    month_key = month_start.strftime("%Y-%m")
    selected_dates = request.form.getlist("holiday_dates")
    holiday_name = request.form.get("name", "").strip() or "Monthly Holiday"
    replace_existing = request.form.get("replace_existing") == "1"

    valid_dates = []
    invalid_dates = 0
    for value in selected_dates:
        holiday_day = parse_iso_date(value)
        if not holiday_day or holiday_day.year != month_start.year or holiday_day.month != month_start.month:
            invalid_dates += 1
            continue
        valid_dates.append(holiday_day.isoformat())
    valid_dates = sorted(set(valid_dates))

    if not valid_dates and not replace_existing:
        flash("Choose at least one date to add for this month.", "error")
        return redirect(url_for("admin_holidays", month=month_key))

    db = get_db()
    removed = 0
    if replace_existing:
        if valid_dates:
            placeholders = ",".join("?" for _ in valid_dates)
            params = [f"{month_key}-%", *valid_dates]
            cursor = db.execute(
                f"DELETE FROM holidays WHERE holiday_date LIKE ? AND holiday_date NOT IN ({placeholders})",
                params,
            )
        else:
            cursor = db.execute("DELETE FROM holidays WHERE holiday_date LIKE ?", (f"{month_key}-%",))
        removed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    added = 0
    skipped = 0
    for day_str in valid_dates:
        cursor = db.execute(
            "INSERT OR IGNORE INTO holidays (holiday_date, name) VALUES (?,?)",
            (day_str, holiday_name),
        )
        if cursor.rowcount:
            added += 1
        else:
            skipped += 1

    db.commit()
    details = [f"{added} added"]
    if skipped:
        details.append(f"{skipped} already existed")
    if removed:
        details.append(f"{removed} removed")
    if invalid_dates:
        details.append(f"{invalid_dates} invalid ignored")
    flash(f"Monthly holidays for {month_key} saved: {', '.join(details)}.", "success")
    return redirect(url_for("admin_holidays", month=month_key))


@app.route("/admin/holidays/<int:holiday_id>/delete", methods=["POST"])
@login_required
@require_perm("manage_holidays")
def admin_delete_holiday(holiday_id):
    db = get_db()
    db.execute("DELETE FROM holidays WHERE id=?", (holiday_id,))
    db.commit()
    flash("Holiday removed.", "success")
    target_month = request.form.get("month", "").strip()
    if parse_year_month(target_month):
        return redirect(url_for("admin_holidays", month=target_month))
    return redirect(url_for("admin_holidays"))


@app.route("/admin/holidays/weekly-off", methods=["POST"])
@login_required
@require_perm("manage_holidays")
def admin_save_weekly_off():
    days = request.form.getlist("weekly_off_days")
    # Validate each is 0..6
    valid = sorted(set(int(d) for d in days if d.isdigit() and 0 <= int(d) <= 6))
    set_app_setting("weekly_off_days", ",".join(str(d) for d in valid))
    flash("Weekly off days saved.", "success")
    return redirect(url_for("admin_holidays"))


@app.route("/admin/holidays/import-template")
@admin_required
def holidays_template():
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Holidays"
        ws.append(["holiday_date", "name"])
        ws.append(["2026-01-01", "New Year"])
        ws.append(["2026-01-26", "Republic Day"])
        ws.append(["2026-08-15", "Independence Day"])
        ws.append(["2026-10-02", "Gandhi Jayanti"])
        ws.append(["2026-12-25", "Christmas"])
        output = io.BytesIO(); wb.save(output); output.seek(0)
        return send_file(output, as_attachment=True, download_name="holidays_template.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:
        flash("openpyxl not installed.", "error")
        return redirect(url_for("admin_holidays"))


@app.route("/admin/holidays/import", methods=["POST"])
@login_required
@require_perm("manage_holidays")
def admin_import_holidays():
    file = request.files.get("excel_file")
    if not file or not file.filename.endswith(('.xlsx', '.xls')):
        flash("Please upload a valid .xlsx file.", "error")
        return redirect(url_for("admin_holidays"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True)
        ws = wb.active
        headers = [str(c.value or "").strip().lower().replace(" ", "_") for c in next(ws.iter_rows(min_row=1, max_row=1))]
        db = get_db()
        added = 0; skipped = 0
        for row in ws.iter_rows(min_row=2):
            data = {headers[i]: str(c.value or "").strip() for i, c in enumerate(row) if i < len(headers)}
            d = data.get("holiday_date") or data.get("date", "")
            n = data.get("name", "")
            if not d or not n:
                continue
            try:
                db.execute("INSERT INTO holidays (holiday_date, name) VALUES (?,?)", (d, n))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        db.commit()
        flash(f"Holidays import: {added} added, {skipped} duplicates skipped.", "success" if added else "error")
    except Exception as e:
        flash(f"Import error: {str(e)}", "error")
    return redirect(url_for("admin_holidays"))


# ══════════════════════════════════════════════════════════
# ══  Access Control: Permission Management  ══════════════
# ══════════════════════════════════════════════════════════


@app.route("/admin/permissions")
@superadmin_required
def admin_permissions():
    db = get_db()
    rows = db.execute("SELECT role, permission, allowed FROM role_permissions").fetchall()
    matrix = {}  # {role: {permission: bool}}
    for r in rows:
        matrix.setdefault(r["role"], {})[r["permission"]] = bool(r["allowed"])
    # Group permissions for display
    groups = {}
    for perm_id, label, group in PERMISSIONS:
        groups.setdefault(group, []).append((perm_id, label))
    roles = ROLE_ORDER
    role_labels = ROLE_LABELS
    return render_template("admin_permissions.html",
                           perm_groups=groups, matrix=matrix,
                           roles=roles, role_labels=role_labels)


@app.route("/admin/permissions/save", methods=["POST"])
@superadmin_required
def admin_save_permissions():
    db = get_db()
    roles = ROLE_ORDER
    valid_perms = {p[0] for p in PERMISSIONS}
    # Read every role/perm checkbox; presence = allowed=1, absence = allowed=0
    rows = []
    for role in roles:
        for perm_id in valid_perms:
            field = f"perm_{role}_{perm_id}"
            allowed = 1 if request.form.get(field) else 0
            rows.append((role, perm_id, allowed))
    db.executemany(
        "INSERT OR REPLACE INTO role_permissions (role, permission, allowed) VALUES (?,?,?)",
        rows
    )
    db.commit()
    log_activity(db, session["user_id"], "updated permissions", "admin", f"{len(rows)} entries")
    db.commit()
    flash("Permissions saved.", "success")
    return redirect(url_for("admin_permissions"))


@app.route("/admin/permissions/reset", methods=["POST"])
@superadmin_required
def admin_reset_permissions():
    db = get_db()
    db.execute("DELETE FROM role_permissions")
    seed_permissions(db)
    flash("Permissions reset to defaults.", "success")
    return redirect(url_for("admin_permissions"))


# ── Kanban Board View ────────────────────────────────────


@app.route("/kanban")
@login_required
def kanban_view():
    db = get_db()
    filter_emp = request.args.get("employee_id", "")
    all_employees = []

    if session.get("role") in ADMIN_ROLES:
        all_employees = db.execute("SELECT id, name FROM employees ORDER BY name").fetchall()
        if filter_emp:
            emp_row = db.execute("SELECT * FROM employees WHERE id=?", (filter_emp,)).fetchone()
            employees = [emp_row] if emp_row else db.execute("SELECT * FROM employees ORDER BY name").fetchall()
        else:
            employees = db.execute("SELECT * FROM employees ORDER BY name").fetchall()
    else:
        employees = [db.execute("SELECT * FROM employees WHERE id=?", (session["user_id"],)).fetchone()]

    today = get_tz_now().date().isoformat()
    columns = {"todo": [], "in_progress": [], "done": []}

    for emp in employees:
        # Recurring tasks
        tasks = db.execute("SELECT * FROM recurring_tasks WHERE employee_id=? AND active=1", (emp["id"],)).fetchall()
        for t in tasks:
            comp = db.execute("SELECT id FROM recurring_completions WHERE task_id=? AND completion_date=?",
                             (t["id"], today)).fetchone()
            item = {"id": t["id"], "type": "recurring", "title": t["title"], "employee": emp["name"], "employee_id": emp["id"], "description": t["description"] or "", "drive_link": t["drive_link"] or ""}
            if comp:
                columns["done"].append(item)
            else:
                columns["todo"].append(item)

        # One-off tasks
        oneoffs = db.execute("SELECT * FROM oneoff_tasks WHERE employee_id=? AND completed=0", (emp["id"],)).fetchall()
        for t in oneoffs:
            columns["todo"].append({"id": t["id"], "type": "oneoff", "title": t["title"], "employee": emp["name"], "employee_id": emp["id"], "description": t["description"] or "", "drive_link": t["drive_link"] or ""})

        completed_oneoffs = db.execute("SELECT * FROM oneoff_tasks WHERE employee_id=? AND completed=1 AND completion_date=?",
                                      (emp["id"], today)).fetchall()
        for t in completed_oneoffs:
            columns["done"].append({"id": t["id"], "type": "oneoff", "title": t["title"], "employee": emp["name"], "employee_id": emp["id"], "description": t["description"] or "", "drive_link": t["drive_link"] or ""})

    return render_template("kanban.html", columns=columns, today=today,
                           all_employees=all_employees, filter_emp=filter_emp)


# ── Time Tracking with Timer ─────────────────────────────


@app.route("/timer/save", methods=["POST"])
@login_required
def save_timer():
    task_id = request.form.get("task_id")
    task_type = request.form.get("task_type")
    minutes = float(request.form.get("minutes", 0))
    notes = request.form.get("notes", "")
    if not task_id or not task_type or minutes <= 0:
        flash("Invalid timer data.", "error")
        return redirect(request.referrer or url_for("today_view"))
    db = get_db()
    today = get_tz_now().date().isoformat()
    if task_type == "recurring":
        task = db.execute("SELECT * FROM recurring_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
        if not task:
            flash("Task not found.", "error")
            return redirect(request.referrer or url_for("today_view"))
        project_id = request.form.get("project_id", task["project_id"] or 0, type=int)
        project_error = _project_required_response(db, project_id)
        if project_error:
            return project_error
        if project_id != (task["project_id"] or 0):
            db.execute("UPDATE recurring_tasks SET project_id=? WHERE id=?", (project_id, task_id))
        existing = db.execute("SELECT id FROM recurring_completions WHERE task_id=? AND completion_date=?",
                             (task_id, today)).fetchone()
        if not existing:
            db.execute("INSERT INTO recurring_completions (task_id, completion_date, time_minutes, notes) VALUES (?,?,?,?)",
                       (task_id, today, minutes, notes))
            if task:
                log_activity(db, task["employee_id"], "completed", "recurring", task["title"],
                           task["description"], today, minutes, notes)
    elif task_type == "oneoff":
        task = db.execute("SELECT * FROM oneoff_tasks WHERE id=? AND employee_id=?", (task_id, session["user_id"])).fetchone()
        if not task:
            flash("Task not found.", "error")
            return redirect(request.referrer or url_for("today_view"))
        project_id = request.form.get("project_id", task["project_id"] or 0, type=int)
        project_error = _project_required_response(db, project_id)
        if project_error:
            return project_error
        db.execute("UPDATE oneoff_tasks SET completed=1, completion_date=?, time_minutes=?, notes=?, project_id=? WHERE id=?",
                   (today, minutes, notes, project_id, task_id))
        if task:
            log_activity(db, task["employee_id"], "completed", "oneoff", task["title"],
                       task["description"], today, minutes, notes)
    db.commit()
    flash("Timer saved!", "success")
    return redirect(request.referrer or url_for("today_view"))


# ── REST API for Automations ───────────────────────────────

def api_auth_required(f):
    """Decorator for API key authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key", "")
        stored_key = get_app_setting("deploy_key", "")
        if not api_key or api_key != stored_key:
            return jsonify({"error": "unauthorized", "message": "Valid X-API-Key header required"}), 401
        return f(*args, **kwargs)
    return decorated


def api_lead_intake_source_payload(data, existing=None):
    source_type = (data.get("source_type") or (existing["source_type"] if existing else "website")).strip()
    if source_type not in ("website", "telegram", "whatsapp"):
        source_type = "website"
    default_stage = (data.get("default_stage") or (existing["default_stage"] if existing else "enquiry")).strip()
    if default_stage not in LEAD_STAGE_VALUES or default_stage == "converted":
        default_stage = "enquiry"
    return {
        "name": (data.get("name") or (existing["name"] if existing else f"{source_type.title()} Lead Source")).strip(),
        "source_type": source_type,
        "enabled": 1 if safe_bool(data.get("enabled"), bool(existing["enabled"]) if existing else True) else 0,
        "default_stage": default_stage,
        "assigned_to": safe_int(data.get("assigned_to"), existing["assigned_to"] if existing else 0),
        "allowed_origins": (data.get("allowed_origins") or (existing["allowed_origins"] if existing else "*")).strip() or "*",
        "success_message": (data.get("success_message") or (existing["success_message"] if existing else "Thanks, we received your enquiry.")).strip(),
    }


@app.route("/api/v1/lead-intake/sources", methods=["GET"])
@api_auth_required
def api_list_lead_intake_sources():
    db = get_db()
    rows = db.execute("""
        SELECT s.*, e.name as assigned_name,
               COUNT(ev.id) as event_count,
               SUM(CASE WHEN ev.status='created' THEN 1 ELSE 0 END) as created_count,
               MAX(ev.created_at) as last_event_at
        FROM lead_intake_sources s
        LEFT JOIN employees e ON s.assigned_to = e.id
        LEFT JOIN lead_intake_events ev ON ev.source_id = s.id
        GROUP BY s.id
        ORDER BY s.source_type, s.name
    """).fetchall()
    return jsonify({"sources": [lead_intake_source_dict(row) for row in rows]})


@app.route("/api/v1/lead-intake/sources", methods=["POST"])
@api_auth_required
def api_create_lead_intake_source():
    data = request.get_json(silent=True) or {}
    payload = api_lead_intake_source_payload(data)
    db = get_db()
    cursor = db.execute("""
        INSERT INTO lead_intake_sources
            (name, source_type, token, enabled, default_stage, assigned_to, allowed_origins, success_message)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        payload["name"], payload["source_type"], secrets.token_urlsafe(24), payload["enabled"],
        payload["default_stage"], payload["assigned_to"], payload["allowed_origins"], payload["success_message"],
    ))
    db.commit()
    source = get_lead_intake_source(db, source_id=cursor.lastrowid, include_disabled=True)
    return jsonify({"status": "created", "source": lead_intake_source_dict(source)}), 201


@app.route("/api/v1/lead-intake/sources/<int:source_id>", methods=["PUT"])
@api_auth_required
def api_update_lead_intake_source(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    payload = api_lead_intake_source_payload(data, source)
    db.execute("""
        UPDATE lead_intake_sources
        SET name=?, source_type=?, enabled=?, default_stage=?, assigned_to=?,
            allowed_origins=?, success_message=?, updated_at=datetime('now','localtime')
        WHERE id=?
    """, (
        payload["name"], payload["source_type"], payload["enabled"], payload["default_stage"],
        payload["assigned_to"], payload["allowed_origins"], payload["success_message"], source_id,
    ))
    db.commit()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    return jsonify({"status": "updated", "source": lead_intake_source_dict(source)})


@app.route("/api/v1/lead-intake/sources/<int:source_id>/rotate", methods=["POST"])
@api_auth_required
def api_rotate_lead_intake_source(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        return jsonify({"error": "not_found"}), 404
    db.execute("UPDATE lead_intake_sources SET token=?, updated_at=datetime('now','localtime') WHERE id=?",
               (secrets.token_urlsafe(24), source_id))
    db.commit()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    return jsonify({"status": "rotated", "source": lead_intake_source_dict(source)})


@app.route("/api/v1/lead-intake/sources/<int:source_id>", methods=["DELETE"])
@api_auth_required
def api_delete_lead_intake_source(source_id):
    db = get_db()
    source = get_lead_intake_source(db, source_id=source_id, include_disabled=True)
    if not source:
        return jsonify({"error": "not_found"}), 404
    db.execute("DELETE FROM lead_intake_sources WHERE id=?", (source_id,))
    db.execute("UPDATE lead_intake_events SET source_id=0 WHERE source_id=?", (source_id,))
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/v1/lead-intake/telegram/<token>", methods=["POST"])
def api_telegram_lead_intake(token):
    db = get_db()
    source = get_lead_intake_source(db, token=token, source_type="telegram")
    if not source:
        return jsonify({"error": "not_found"}), 404
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret_header and secret_header != token:
        return jsonify({"error": "unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    external_id = str(update.get("update_id", ""))
    duplicate = lead_intake_duplicate(db, "telegram", external_id)
    if duplicate:
        return jsonify({"status": "duplicate", "lead_id": duplicate["lead_id"]})
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    if handle_telegram_hr_group_command(db, source, update, message, text, external_id):
        return jsonify({"status": "handled"})
    if handle_telegram_delete_report_command(db, source, update, message, text, external_id):
        return jsonify({"status": "handled"})
    if handle_telegram_sales_command(db, source, update, message, text, external_id):
        return jsonify({"status": "handled"})
    if not should_create_text_lead(text):
        log_lead_intake_event(db, source, "telegram", "ignored", update, 0, external_id)
        db.commit()
        if chat_id and text.lower().startswith("/start"):
            first_name = message.get("from", {}).get("first_name", "there")
            send_telegram(
                chat_id,
                f"Hi {first_name}!\\n\\nYour Telegram Chat ID is:\\n<code>{chat_id}</code>\\n\\n{lead_intake_help_text()}"
            )
        return jsonify({"status": "ignored", "message": "Send /lead followed by lead details."})
    maybe_auto_bind_sales_group(db, message)
    if not telegram_sales_command_allowed(db, message):
        log_lead_intake_event(db, source, "telegram", "unauthorized_create", update, 0, external_id)
        db.commit()
        if chat_id:
            send_telegram(chat_id, "Lead creation is enabled only in the configured Sales Team group or a linked superadmin private chat.")
        return jsonify({"status": "unauthorized"}), 403
    data = parse_lead_text_message(text)
    if not data:
        log_lead_intake_event(db, source, "telegram", "empty_lead", update, 0, external_id)
        db.commit()
        if chat_id:
            send_telegram(chat_id, telegram_sales_help_text())
        return jsonify({"status": "ignored", "message": "Lead details missing."})
    sender = message.get("from") or {}
    if sender.get("username"):
        data["source"] = f"Telegram @{sender['username']}"
    else:
        data["source"] = "Telegram"
    lead_id, normalized = create_lead_from_intake(db, source, data, "telegram", update, external_id)
    db.commit()
    sales_chat_id = get_sales_telegram_chat_id()
    if chat_id and str(chat_id) == str(sales_chat_id):
        lead = fetch_lead_for_telegram(db, lead_id)
        send_telegram(chat_id, format_lead_for_telegram(lead, "Lead saved"), telegram_open_lead_markup(lead_id))
    else:
        notify_lead_intake_created(source, lead_id, normalized)
        if chat_id:
            send_telegram(chat_id, f"Lead saved in Task Manager: #{lead_id}", telegram_open_lead_markup(lead_id))
    return jsonify({"status": "created", "lead_id": lead_id})


@app.route("/api/v1/lead-intake/whatsapp/<token>", methods=["GET", "POST"])
def api_whatsapp_lead_intake(token):
    db = get_db()
    source = get_lead_intake_source(db, token=token, source_type="whatsapp")
    if not source:
        return ("Not found", 404)
    if request.method == "GET":
        mode = request.args.get("hub.mode", "")
        verify_token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and verify_token == token:
            return make_response(challenge, 200)
        return make_response("Verification failed", 403)

    payload = request.get_json(silent=True) or {}
    created = []
    notifications = []
    ignored = 0
    entries = payload.get("entry", []) if isinstance(payload.get("entry"), list) else []
    for entry in entries:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            contacts_by_wa = {}
            for contact in value.get("contacts", []) or []:
                wa_id = contact.get("wa_id", "")
                contacts_by_wa[wa_id] = ((contact.get("profile") or {}).get("name") or "").strip()
            for message in value.get("messages", []) or []:
                external_id = message.get("id", "")
                if lead_intake_duplicate(db, "whatsapp", external_id):
                    continue
                text = ""
                if message.get("type") == "text":
                    text = ((message.get("text") or {}).get("body") or "").strip()
                if not should_create_text_lead(text):
                    log_lead_intake_event(db, source, "whatsapp", "ignored", message, 0, external_id)
                    ignored += 1
                    continue
                data = parse_lead_text_message(text)
                from_number = message.get("from", "")
                if from_number and not data.get("phone"):
                    data["phone"] = from_number
                if from_number and contacts_by_wa.get(from_number) and not data.get("contact_person"):
                    data["contact_person"] = contacts_by_wa[from_number]
                data["source"] = "WhatsApp"
                lead_id, normalized = create_lead_from_intake(db, source, data, "whatsapp", message, external_id)
                created.append(lead_id)
                notifications.append((lead_id, normalized))
    db.commit()
    for lead_id, normalized in notifications:
        notify_lead_intake_created(source, lead_id, normalized)
    return jsonify({"status": "ok", "created": created, "ignored": ignored})


@app.route("/api/v1/lead-intake/<token>", methods=["POST", "OPTIONS"])
def api_public_lead_intake(token):
    db = get_db()
    source = get_lead_intake_source(db, token=token, source_type="website")
    if not source:
        return lead_intake_response({"error": "not_found"}, None, 404)
    if request.method == "OPTIONS":
        return lead_intake_response({"status": "ok"}, source)
    origin = request.headers.get("Origin", "")
    if not lead_intake_origin_allowed(source, origin):
        log_lead_intake_event(db, source, "website", "blocked_origin", {"origin": origin}, 0, "")
        db.commit()
        return lead_intake_response({"error": "origin_not_allowed"}, source, 403)
    payload = lead_intake_request_payload()
    if (payload.get("_lead_hp") or payload.get("_hp") or payload.get("honeypot")):
        log_lead_intake_event(db, source, "website", "spam_ignored", payload, 0, "")
        db.commit()
        return lead_intake_response({"status": "ok", "message": source["success_message"]}, source)
    lead_id, normalized = create_lead_from_intake(db, source, payload, "website", payload, "")
    db.commit()
    notify_lead_intake_created(source, lead_id, normalized)
    return lead_intake_response({
        "status": "created",
        "lead_id": lead_id,
        "message": source["success_message"] or "Thanks, we received your enquiry.",
    }, source, 201)


def api_parse_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def api_parse_float(value, default=0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def api_parse_lead_followup_due(data):
    due_at = (data.get("due_at") or data.get("followup_due_at") or "").strip()
    if due_at:
        due_at = due_at.replace("T", " ")[:16]
        try:
            datetime.strptime(due_at, "%Y-%m-%d %H:%M")
            return due_at
        except ValueError:
            return ""
    return parse_lead_followup_at(
        data.get("followup_date") or data.get("date") or "",
        data.get("followup_time") or data.get("time") or "",
    )


def api_fetch_lead(db, lead_id):
    return db.execute("""
        SELECT l.*, e.name as assigned_name, c.name as converted_client_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        LEFT JOIN clients c ON l.converted_client_id = c.id
        WHERE l.id=?
    """, (lead_id,)).fetchone()


def api_lead_dict(db, lead_id, include_details=False):
    lead = api_fetch_lead(db, lead_id)
    if not lead:
        return None
    result = dict(lead)
    if include_details:
        followups = db.execute("""
            SELECT lf.*, e.name as created_by_name
            FROM lead_followups lf
            LEFT JOIN employees e ON lf.created_by = e.id
            WHERE lf.lead_id=?
            ORDER BY CASE WHEN lf.status='pending' THEN 0 ELSE 1 END, lf.due_at ASC
        """, (lead_id,)).fetchall()
        history = db.execute("""
            SELECT h.*, e.name as changed_by_name
            FROM lead_stage_history h
            LEFT JOIN employees e ON h.changed_by = e.id
            WHERE h.lead_id=?
            ORDER BY h.created_at DESC, h.id DESC
        """, (lead_id,)).fetchall()
        result["followups"] = [dict(f) for f in followups]
        result["stage_history"] = [dict(h) for h in history]
    return result


def api_lead_stats(db):
    now = get_tz_now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.date().isoformat()
    stage_counts = {stage: 0 for stage, _ in LEAD_STAGES}
    stage_values = {stage: 0 for stage, _ in LEAD_STAGES}
    for row in db.execute("""
        SELECT stage, COUNT(*) as cnt, COALESCE(SUM(estimated_value), 0) as total_value
        FROM leads
        GROUP BY stage
    """).fetchall():
        stage_counts[row["stage"]] = row["cnt"]
        stage_values[row["stage"]] = row["total_value"] or 0
    active_count = sum(count for stage, count in stage_counts.items() if stage != "converted")
    total_count = sum(stage_counts.values())
    pipeline_value = sum(value for stage, value in stage_values.items() if stage != "converted")
    due_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE COALESCE(next_followup_at, '') != ''
          AND next_followup_at <= ?
          AND stage != 'converted'
    """, (now_str,)).fetchone()["cnt"]
    today_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE substr(COALESCE(next_followup_at, ''), 1, 10)=?
          AND stage != 'converted'
    """, (today_str,)).fetchone()["cnt"]
    unassigned_count = db.execute("""
        SELECT COUNT(*) as cnt FROM leads
        WHERE COALESCE(assigned_to, 0)=0 AND stage != 'converted'
    """).fetchone()["cnt"]
    return {
        "stage_counts": stage_counts,
        "stage_values": stage_values,
        "active_count": active_count,
        "total_count": total_count,
        "pipeline_value": pipeline_value,
        "due_count": due_count,
        "today_count": today_count,
        "unassigned_count": unassigned_count,
        "as_of": now_str,
    }


def api_update_lead_stage(db, lead, stage, note="", changed_by=0):
    if stage not in LEAD_STAGE_VALUES:
        return False, "invalid_stage"
    if stage == "converted" and not lead["converted_client_id"]:
        return False, "use_convert_endpoint"
    if stage != lead["stage"]:
        db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                      VALUES (?,?,?,?,?)""",
                   (lead["id"], lead["stage"], stage, note, changed_by))
    return True, ""


# -- Leads API --

@app.route("/api/v1/leads/stages", methods=["GET"])
@api_auth_required
def api_lead_stages():
    return jsonify({
        "stages": [{"value": stage, "label": label} for stage, label in LEAD_STAGES],
        "stage_labels": LEAD_STAGE_LABELS,
    })


@app.route("/api/v1/leads/stats", methods=["GET"])
@api_auth_required
def api_leads_stats():
    return jsonify({"stats": api_lead_stats(get_db())})


@app.route("/api/v1/leads", methods=["GET"])
@api_auth_required
def api_list_leads():
    db = get_db()
    stage_filter = request.args.get("stage", "").strip()
    assigned_raw = request.args.get("assigned_to", request.args.get("assigned", "")).strip()
    due_filter = request.args.get("due", "").strip()
    search = request.args.get("q", "").strip()
    limit = max(1, min(api_parse_int(request.args.get("limit"), 250), 1000))
    now = get_tz_now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    today_str = now.date().isoformat()

    where = ["1=1"]
    params = []
    if stage_filter:
        if stage_filter not in LEAD_STAGE_VALUES:
            return jsonify({"error": "invalid_stage", "valid_stages": list(LEAD_STAGE_VALUES)}), 400
        where.append("l.stage=?")
        params.append(stage_filter)
    if assigned_raw:
        if assigned_raw in ("-1", "unassigned"):
            where.append("COALESCE(l.assigned_to, 0)=0")
        else:
            assigned_to = api_parse_int(assigned_raw, None)
            if assigned_to is None:
                return jsonify({"error": "assigned_to must be an integer, -1, or unassigned"}), 400
            where.append("l.assigned_to=?")
            params.append(assigned_to)
    if due_filter == "due":
        where.append("COALESCE(l.next_followup_at, '') != '' AND l.next_followup_at <= ? AND l.stage != 'converted'")
        params.append(now_str)
    elif due_filter == "today":
        where.append("substr(COALESCE(l.next_followup_at, ''), 1, 10)=? AND l.stage != 'converted'")
        params.append(today_str)
    elif due_filter == "none":
        where.append("COALESCE(l.next_followup_at, '') = '' AND l.stage != 'converted'")
    elif due_filter:
        return jsonify({"error": "due must be due, today, none, or blank"}), 400
    if search:
        like = f"%{search}%"
        where.append("(l.company_name LIKE ? OR l.contact_person LIKE ? OR l.email LIKE ? OR l.phone LIKE ? OR l.requirement LIKE ? OR l.notes LIKE ?)")
        params.extend([like, like, like, like, like, like])

    params.append(limit)
    leads = db.execute(f"""
        SELECT l.*, e.name as assigned_name, c.name as converted_client_name
        FROM leads l
        LEFT JOIN employees e ON l.assigned_to = e.id
        LEFT JOIN clients c ON l.converted_client_id = c.id
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN l.stage='converted' THEN 1 ELSE 0 END,
            CASE WHEN COALESCE(l.next_followup_at, '') = '' THEN 1 ELSE 0 END,
            l.next_followup_at ASC,
            l.updated_at DESC
        LIMIT ?
    """, params).fetchall()
    return jsonify({"leads": [dict(lead) for lead in leads], "stats": api_lead_stats(db)})


@app.route("/api/v1/leads", methods=["POST"])
@api_auth_required
def api_create_lead():
    data = request.get_json(silent=True) or {}
    company_name = (data.get("company_name") or data.get("name") or "").strip()
    if not company_name:
        return jsonify({"error": "company_name required"}), 400
    stage = (data.get("stage") or "enquiry").strip()
    if stage not in LEAD_STAGE_VALUES or stage == "converted":
        return jsonify({"error": "stage must be enquiry, discussion, confirmation, or on_hold"}), 400
    estimated_value = api_parse_float(data.get("estimated_value"), 0)
    assigned_to = api_parse_int(data.get("assigned_to"), 0)
    created_by = api_parse_int(data.get("created_by"), 0)

    followup_data = data.get("followup") if isinstance(data.get("followup"), dict) else data
    followup_note = (followup_data.get("note") or followup_data.get("followup_note") or "").strip()
    followup_due = api_parse_lead_followup_due(followup_data)
    if followup_note or followup_due:
        if not followup_note or not followup_due:
            return jsonify({"error": "followup requires note and due_at, or note plus followup_date/followup_time"}), 400

    db = get_db()
    cursor = db.execute("""
        INSERT INTO leads (company_name, contact_person, email, phone, source, stage,
            estimated_value, requirement, notes, assigned_to, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        company_name,
        (data.get("contact_person") or "").strip(),
        (data.get("email") or "").strip(),
        (data.get("phone") or "").strip(),
        (data.get("source") or "").strip(),
        stage,
        estimated_value,
        (data.get("requirement") or "").strip(),
        (data.get("notes") or "").strip(),
        assigned_to,
        created_by,
    ))
    lead_id = cursor.lastrowid
    db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                  VALUES (?,?,?,?,?)""",
               (lead_id, "", stage, (data.get("stage_note") or "Lead created via API").strip(), created_by))
    if followup_note and followup_due:
        db.execute("""INSERT INTO lead_followups (lead_id, due_at, note, created_by)
                      VALUES (?,?,?,?)""", (lead_id, followup_due, followup_note, created_by))
        refresh_lead_next_followup(db, lead_id)
    db.commit()
    if created_by:
        log_activity(db, created_by, "created lead via API", "lead", company_name,
                     task_description=(data.get("requirement") or "").strip())
        db.commit()
    return jsonify({"status": "created", "lead_id": lead_id, "lead": api_lead_dict(db, lead_id, include_details=True)}), 201


@app.route("/api/v1/leads/<int:lead_id>", methods=["GET"])
@api_auth_required
def api_get_lead(lead_id):
    lead = api_lead_dict(get_db(), lead_id, include_details=True)
    if not lead:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"lead": lead})


@app.route("/api/v1/leads/<int:lead_id>", methods=["PUT"])
@api_auth_required
def api_update_lead(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []

    text_fields = ["company_name", "contact_person", "email", "phone", "source", "requirement", "notes"]
    if "name" in data and "company_name" not in data:
        data["company_name"] = data["name"]
    for col in text_fields:
        if col in data:
            value = (data.get(col) or "").strip()
            if col == "company_name" and not value:
                return jsonify({"error": "company_name cannot be blank"}), 400
            fields.append(f"{col}=?")
            values.append(value)
    if "estimated_value" in data:
        fields.append("estimated_value=?")
        values.append(api_parse_float(data.get("estimated_value"), 0))
    if "assigned_to" in data:
        fields.append("assigned_to=?")
        values.append(api_parse_int(data.get("assigned_to"), 0))
    if "stage" in data:
        stage = (data.get("stage") or "").strip()
        ok, err = api_update_lead_stage(
            db, lead, stage,
            note=(data.get("stage_note") or "").strip(),
            changed_by=api_parse_int(data.get("changed_by"), 0),
        )
        if not ok:
            return jsonify({"error": err}), 400
        fields.append("stage=?")
        values.append(stage)
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    fields.append("updated_at=datetime('now','localtime')")
    values.append(lead_id)
    db.execute(f"UPDATE leads SET {', '.join(fields)} WHERE id=?", values)
    refresh_lead_next_followup(db, lead_id)
    db.commit()
    actor_id = api_parse_int(data.get("changed_by"), 0)
    updated_lead = api_lead_dict(db, lead_id, include_details=True)
    if actor_id:
        log_activity(db, actor_id, "updated lead via API", "lead", updated_lead["company_name"])
        db.commit()
    return jsonify({"status": "updated", "lead": updated_lead})


@app.route("/api/v1/leads/<int:lead_id>", methods=["DELETE"])
@api_auth_required
def api_delete_lead(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return jsonify({"error": "not_found"}), 404
    db.execute("DELETE FROM lead_followups WHERE lead_id=?", (lead_id,))
    db.execute("DELETE FROM lead_stage_history WHERE lead_id=?", (lead_id,))
    db.execute("DELETE FROM leads WHERE id=?", (lead_id,))
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/v1/leads/<int:lead_id>/stage", methods=["POST", "PUT"])
@api_auth_required
def api_set_lead_stage(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    stage = (data.get("stage") or "").strip()
    ok, err = api_update_lead_stage(
        db, lead, stage,
        note=(data.get("note") or data.get("stage_note") or "").strip(),
        changed_by=api_parse_int(data.get("changed_by"), 0),
    )
    if not ok:
        return jsonify({"error": err}), 400
    db.execute("UPDATE leads SET stage=?, updated_at=datetime('now','localtime') WHERE id=?", (stage, lead_id))
    db.commit()
    return jsonify({"status": "updated", "lead": api_lead_dict(db, lead_id, include_details=True)})


@app.route("/api/v1/leads/<int:lead_id>/history", methods=["GET"])
@api_auth_required
def api_lead_history(lead_id):
    db = get_db()
    if not db.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone():
        return jsonify({"error": "not_found"}), 404
    rows = db.execute("""
        SELECT h.*, e.name as changed_by_name
        FROM lead_stage_history h
        LEFT JOIN employees e ON h.changed_by = e.id
        WHERE h.lead_id=?
        ORDER BY h.created_at DESC, h.id DESC
    """, (lead_id,)).fetchall()
    return jsonify({"history": [dict(row) for row in rows]})


@app.route("/api/v1/leads/<int:lead_id>/followups", methods=["GET"])
@api_auth_required
def api_list_lead_followups(lead_id):
    db = get_db()
    if not db.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone():
        return jsonify({"error": "not_found"}), 404
    rows = db.execute("""
        SELECT lf.*, e.name as created_by_name
        FROM lead_followups lf
        LEFT JOIN employees e ON lf.created_by = e.id
        WHERE lf.lead_id=?
        ORDER BY CASE WHEN lf.status='pending' THEN 0 ELSE 1 END, lf.due_at ASC
    """, (lead_id,)).fetchall()
    return jsonify({"followups": [dict(row) for row in rows]})


@app.route("/api/v1/leads/<int:lead_id>/followups", methods=["POST"])
@api_auth_required
def api_create_lead_followup(lead_id):
    db = get_db()
    if not db.execute("SELECT id FROM leads WHERE id=?", (lead_id,)).fetchone():
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    due_at = api_parse_lead_followup_due(data)
    note = (data.get("note") or "").strip()
    if not due_at or not note:
        return jsonify({"error": "note and due_at, or note plus followup_date/followup_time, required"}), 400
    created_by = api_parse_int(data.get("created_by"), 0)
    cursor = db.execute("""INSERT INTO lead_followups (lead_id, due_at, note, created_by)
                           VALUES (?,?,?,?)""", (lead_id, due_at, note, created_by))
    followup_id = cursor.lastrowid
    refresh_lead_next_followup(db, lead_id)
    db.commit()
    followup = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    return jsonify({"status": "created", "followup_id": followup_id, "followup": dict(followup)}), 201


@app.route("/api/v1/leads/followups/<int:followup_id>", methods=["PUT"])
@api_auth_required
def api_update_lead_followup(followup_id):
    db = get_db()
    followup = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    if not followup:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []
    if "note" in data:
        note = (data.get("note") or "").strip()
        if not note:
            return jsonify({"error": "note cannot be blank"}), 400
        fields.append("note=?")
        values.append(note)
    if any(key in data for key in ("due_at", "followup_due_at", "followup_date", "date")):
        due_at = api_parse_lead_followup_due(data)
        if not due_at:
            return jsonify({"error": "invalid due_at"}), 400
        fields.append("due_at=?")
        values.append(due_at)
    if "telegram_sent_at" in data:
        fields.append("telegram_sent_at=?")
        values.append(data.get("telegram_sent_at") or "")
    if "status" in data:
        status = (data.get("status") or "").strip()
        if status not in ("pending", "done", "cancelled"):
            return jsonify({"error": "status must be pending, done, or cancelled"}), 400
        fields.append("status=?")
        values.append(status)
        fields.append("completed_at=?")
        values.append((data.get("completed_at") or get_tz_now().strftime("%Y-%m-%d %H:%M")) if status in ("done", "cancelled") else "")
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(followup_id)
    db.execute(f"UPDATE lead_followups SET {', '.join(fields)} WHERE id=?", values)
    refresh_lead_next_followup(db, followup["lead_id"])
    db.commit()
    updated = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    return jsonify({"status": "updated", "followup": dict(updated)})


@app.route("/api/v1/leads/followups/<int:followup_id>/complete", methods=["POST"])
@api_auth_required
def api_complete_lead_followup(followup_id):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "done").strip()
    if status not in ("done", "cancelled"):
        return jsonify({"error": "status must be done or cancelled"}), 400
    db = get_db()
    followup = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    if not followup:
        return jsonify({"error": "not_found"}), 404
    completed_at = data.get("completed_at") or get_tz_now().strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE lead_followups SET status=?, completed_at=? WHERE id=?", (status, completed_at, followup_id))
    refresh_lead_next_followup(db, followup["lead_id"])
    db.commit()
    updated = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    return jsonify({"status": "updated", "followup": dict(updated)})


@app.route("/api/v1/leads/followups/<int:followup_id>", methods=["DELETE"])
@api_auth_required
def api_delete_lead_followup(followup_id):
    db = get_db()
    followup = db.execute("SELECT * FROM lead_followups WHERE id=?", (followup_id,)).fetchone()
    if not followup:
        return jsonify({"error": "not_found"}), 404
    db.execute("DELETE FROM lead_followups WHERE id=?", (followup_id,))
    refresh_lead_next_followup(db, followup["lead_id"])
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/v1/leads/<int:lead_id>/convert", methods=["POST"])
@api_auth_required
def api_convert_lead(lead_id):
    db = get_db()
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return jsonify({"error": "not_found"}), 404
    if lead["converted_client_id"]:
        return jsonify({"error": "already_converted", "client_id": lead["converted_client_id"]}), 409
    data = request.get_json(silent=True) or {}
    client_name = (data.get("client_name") or lead["company_name"]).strip()
    client_notes = (data.get("client_notes") or "").strip()
    if not client_notes:
        client_notes = f"Converted from lead #{lead_id} via API."
        if lead["requirement"]:
            client_notes += f"\nRequirement: {lead['requirement']}"
        if lead["notes"]:
            client_notes += f"\nLead notes: {lead['notes']}"
    cursor = db.execute("""
        INSERT INTO clients (name, contact_person, email, phone, address, notes)
        VALUES (?,?,?,?,?,?)
    """, (client_name, lead["contact_person"], lead["email"], lead["phone"], data.get("address", ""), client_notes))
    client_id = cursor.lastrowid
    converted_at = get_tz_now().strftime("%Y-%m-%d %H:%M")
    db.execute("""
        UPDATE leads SET stage='converted', converted_client_id=?, converted_at=?,
            updated_at=datetime('now','localtime')
        WHERE id=?
    """, (client_id, converted_at, lead_id))
    db.execute("UPDATE lead_followups SET status='cancelled', completed_at=? WHERE lead_id=? AND status='pending'",
               (converted_at, lead_id))
    db.execute("""INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, note, changed_by)
                  VALUES (?,?,?,?,?)""",
               (lead_id, lead["stage"], "converted", f"Converted to client #{client_id} via API",
                api_parse_int(data.get("changed_by"), 0)))
    refresh_lead_next_followup(db, lead_id)
    db.commit()
    actor_id = api_parse_int(data.get("changed_by"), 0)
    if actor_id:
        log_activity(db, actor_id, "converted lead via API", "lead", lead["company_name"],
                     notes=f"Client #{client_id}")
        db.commit()
    return jsonify({
        "status": "converted",
        "lead_id": lead_id,
        "client_id": client_id,
        "lead": api_lead_dict(db, lead_id, include_details=True),
    })


# -- Roles / Permissions API --

@app.route("/api/v1/roles", methods=["GET"])
@api_auth_required
def api_list_roles():
    permissions = [{"id": perm_id, "label": label, "group": group} for perm_id, label, group in PERMISSIONS]
    role_permissions = {}
    for role in ROLE_ORDER:
        role_permissions[role] = {}
        for perm_id, _, _ in PERMISSIONS:
            role_permissions[role][perm_id] = True if role == "superadmin" else has_perm(role, perm_id)
    return jsonify({
        "roles": ROLE_ORDER,
        "role_labels": ROLE_LABELS,
        "permissions": permissions,
        "role_permissions": role_permissions,
    })


@app.route("/api/v1/roles/<role>/permissions", methods=["PUT"])
@api_auth_required
def api_update_role_permissions(role):
    if role not in ROLE_ORDER:
        return jsonify({"error": "unknown_role"}), 404
    if role == "superadmin":
        return jsonify({"error": "superadmin permissions are always enabled"}), 400
    data = request.get_json(silent=True) or {}
    updates = data.get("permissions", data)
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "permissions object required"}), 400
    valid_perms = {perm_id for perm_id, _, _ in PERMISSIONS}
    invalid = sorted([perm for perm in updates if perm not in valid_perms])
    if invalid:
        return jsonify({"error": "unknown_permissions", "permissions": invalid}), 400
    db = get_db()
    for perm_id, allowed in updates.items():
        db.execute("INSERT OR REPLACE INTO role_permissions (role, permission, allowed) VALUES (?,?,?)",
                   (role, perm_id, 1 if bool(allowed) else 0))
    db.commit()
    return jsonify({"status": "updated", "role": role, "permissions": {perm: has_perm(role, perm) for perm in valid_perms}})


# -- Employees API --

@app.route("/api/v1/employees", methods=["GET"])
@api_auth_required
def api_list_employees():
    db = get_db()
    employees = db.execute("SELECT id, name, username, role, email, phone, department, telegram_chat_id, birthday, joining_date, wedding_anniversary, created_at FROM employees ORDER BY name").fetchall()
    return jsonify({"employees": [dict(e) for e in employees]})


@app.route("/api/v1/employees/<int:emp_id>", methods=["GET"])
@api_auth_required
def api_get_employee(emp_id):
    db = get_db()
    emp = db.execute("SELECT id, name, username, role, email, phone, department, telegram_chat_id, birthday, joining_date, wedding_anniversary, created_at FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"employee": dict(emp)})


@app.route("/api/v1/employees", methods=["POST"])
@api_auth_required
def api_create_employee():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "").strip()
    if not name or not username or not password:
        return jsonify({"error": "name, username, and password required"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM employees WHERE username=?", (username,)).fetchone()
    if existing:
        return jsonify({"error": "username already exists"}), 409
    role = data.get("role", "employee")
    if role not in ROLE_ORDER:
        role = "employee"
    db.execute(
        """INSERT INTO employees (name, username, password, role, telegram_chat_id, email, phone, department, birthday, joining_date, wedding_anniversary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (name, username, hash_password(password), role,
         data.get("telegram_chat_id", ""), data.get("email", ""), data.get("phone", ""),
         data.get("department", ""), data.get("birthday", ""), data.get("joining_date", ""),
         data.get("wedding_anniversary", "")),
    )
    emp_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE employees SET must_change_password=1 WHERE id=?", (emp_id,))
    db.commit()
    return jsonify({"status": "created", "employee_id": emp_id}), 201


@app.route("/api/v1/employees/<int:emp_id>", methods=["PUT"])
@api_auth_required
def api_update_employee(emp_id):
    db = get_db()
    emp = db.execute("SELECT id FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []
    for col in ["name", "email", "phone", "department", "telegram_chat_id", "role", "birthday", "joining_date", "wedding_anniversary"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if "password" in data and data["password"]:
        fields.append("password=?")
        values.append(hash_password(data["password"]))
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(emp_id)
    db.execute(f"UPDATE employees SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/employees/<int:emp_id>", methods=["DELETE"])
@api_auth_required
def api_delete_employee(emp_id):
    db = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp:
        return jsonify({"error": "not_found"}), 404
    if emp["role"] in ("admin", "superadmin"):
        return jsonify({"error": "cannot delete admin/superadmin via API"}), 403
    db.execute("DELETE FROM recurring_completions WHERE task_id IN (SELECT id FROM recurring_tasks WHERE employee_id=?)", (emp_id,))
    db.execute("DELETE FROM recurring_tasks WHERE employee_id=?", (emp_id,))
    db.execute("DELETE FROM oneoff_tasks WHERE employee_id=?", (emp_id,))
    db.execute("DELETE FROM login_log WHERE employee_id=?", (emp_id,))
    db.execute("DELETE FROM attendance WHERE employee_id=?", (emp_id,))
    db.execute("UPDATE leave_request_history SET actor_id=0 WHERE actor_id=?", (emp_id,))
    db.execute("UPDATE leave_requests SET created_by=0 WHERE created_by=?", (emp_id,))
    db.execute("UPDATE leave_requests SET reviewed_by=0 WHERE reviewed_by=?", (emp_id,))
    db.execute("DELETE FROM leave_request_history WHERE leave_request_id IN (SELECT id FROM leave_requests WHERE employee_id=?)", (emp_id,))
    db.execute("DELETE FROM leave_requests WHERE employee_id=?", (emp_id,))
    db.execute("DELETE FROM activity_log WHERE employee_id=?", (emp_id,))
    db.execute("DELETE FROM employees WHERE id=?", (emp_id,))
    db.commit()
    return jsonify({"status": "deleted"})


# -- Clients API --

@app.route("/api/v1/clients", methods=["GET"])
@api_auth_required
def api_list_clients():
    db = get_db()
    clients = db.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return jsonify({"clients": [dict(c) for c in clients]})


@app.route("/api/v1/clients/<int:client_id>", methods=["GET"])
@api_auth_required
def api_get_client(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"client": dict(client)})


@app.route("/api/v1/clients", methods=["POST"])
@api_auth_required
def api_create_client():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO clients (name, contact_person, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
        (name, data.get("contact_person", ""), data.get("email", ""), data.get("phone", ""),
         data.get("address", ""), data.get("notes", "")),
    )
    db.commit()
    client_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"status": "created", "client_id": client_id}), 201


@app.route("/api/v1/clients/<int:client_id>", methods=["PUT"])
@api_auth_required
def api_update_client(client_id):
    db = get_db()
    client = db.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []
    for col in ["name", "contact_person", "email", "phone", "address", "notes"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(client_id)
    db.execute(f"UPDATE clients SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/clients/<int:client_id>", methods=["DELETE"])
@api_auth_required
def api_delete_client(client_id):
    db = get_db()
    client = db.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return jsonify({"error": "not_found"}), 404
    db.execute("DELETE FROM clients WHERE id=?", (client_id,))
    db.commit()
    return jsonify({"status": "deleted"})


# -- Projects API --

@app.route("/api/v1/projects", methods=["GET"])
@api_auth_required
def api_list_projects():
    db = get_db()
    projects = db.execute("""SELECT p.*, COALESCE(c.name, '') as client_name FROM projects p
                            LEFT JOIN clients c ON p.client_id = c.id ORDER BY COALESCE(c.name, 'zzz'), p.name""").fetchall()
    return jsonify({"projects": [dict(p) for p in projects]})


@app.route("/api/v1/projects/<int:project_id>", methods=["GET"])
@api_auth_required
def api_get_project(project_id):
    db = get_db()
    project = db.execute("""SELECT p.*, COALESCE(c.name, '') as client_name FROM projects p
                           LEFT JOIN clients c ON p.client_id = c.id WHERE p.id=?""", (project_id,)).fetchone()
    if not project:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"project": dict(project)})


@app.route("/api/v1/projects", methods=["POST"])
@api_auth_required
def api_create_project():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    client_id = data.get("client_id")
    if not name or not client_id:
        return jsonify({"error": "name and client_id required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO projects (client_id, name, description, services, status) VALUES (?,?,?,?,?)",
        (client_id, name, data.get("description", ""), data.get("services", ""), data.get("status", "active")),
    )
    db.commit()
    project_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"status": "created", "project_id": project_id}), 201


@app.route("/api/v1/projects/<int:project_id>", methods=["PUT"])
@api_auth_required
def api_update_project(project_id):
    db = get_db()
    project = db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []
    for col in ["client_id", "name", "description", "services", "status"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(project_id)
    db.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/projects/<int:project_id>", methods=["DELETE"])
@api_auth_required
def api_delete_project(project_id):
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.commit()
    return jsonify({"status": "deleted"})


# -- Tasks API (recurring + one-off) --

@app.route("/api/v1/tasks/recurring", methods=["GET"])
@api_auth_required
def api_list_recurring_tasks():
    db = get_db()
    emp_id = request.args.get("employee_id")
    if emp_id:
        tasks = db.execute("""SELECT rt.*, e.name as employee_name FROM recurring_tasks rt
                             JOIN employees e ON rt.employee_id = e.id
                             WHERE rt.employee_id=? ORDER BY rt.title""", (emp_id,)).fetchall()
    else:
        tasks = db.execute("""SELECT rt.*, e.name as employee_name FROM recurring_tasks rt
                             JOIN employees e ON rt.employee_id = e.id ORDER BY e.name, rt.title""").fetchall()
    return jsonify({"tasks": [dict(t) for t in tasks]})


@app.route("/api/v1/tasks/recurring", methods=["POST"])
@api_auth_required
def api_create_recurring_task():
    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    title = data.get("title", "").strip()
    project_id = _safe_project_id(data.get("project_id"))
    if not employee_id or not title or not project_id:
        return jsonify({"error": "employee_id, title and project_id required"}), 400
    db = get_db()
    project_error = _project_required_response(db, project_id, html=False, message="valid project_id required")
    if project_error:
        return project_error
    db.execute(
        "INSERT INTO recurring_tasks (employee_id, title, description, frequency, frequency_day, frequency_month, drive_link, project_id) VALUES (?,?,?,?,?,?,?,?)",
        (employee_id, title, data.get("description", ""),
         data.get("frequency", "daily"), data.get("frequency_day", 0), data.get("frequency_month", 0),
         data.get("drive_link", ""), project_id),
    )
    db.commit()
    task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"status": "created", "task_id": task_id}), 201


@app.route("/api/v1/tasks/recurring/<int:task_id>", methods=["PUT"])
@api_auth_required
def api_update_recurring_task(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM recurring_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    if "project_id" in data:
        project_error = _project_required_response(db, _safe_project_id(data.get("project_id")), html=False, message="valid project_id required")
        if project_error:
            return project_error
    fields = []
    values = []
    for col in ["title", "description", "frequency", "frequency_day", "frequency_month", "active", "drive_link", "project_id"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(task_id)
    db.execute(f"UPDATE recurring_tasks SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/tasks/recurring/<int:task_id>", methods=["DELETE"])
@api_auth_required
def api_delete_recurring_task(task_id):
    db = get_db()
    db.execute("DELETE FROM recurring_completions WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM recurring_tasks WHERE id=?", (task_id,))
    db.commit()
    return jsonify({"status": "deleted"})


@app.route("/api/v1/tasks/oneoff", methods=["GET"])
@api_auth_required
def api_list_oneoff_tasks():
    db = get_db()
    emp_id = request.args.get("employee_id")
    status = request.args.get("status")  # "pending" or "completed"
    query = "SELECT ot.*, e.name as employee_name FROM oneoff_tasks ot JOIN employees e ON ot.employee_id = e.id WHERE 1=1"
    params = []
    if emp_id:
        query += " AND ot.employee_id=?"
        params.append(emp_id)
    if status == "pending":
        query += " AND ot.completed=0"
    elif status == "completed":
        query += " AND ot.completed=1"
    query += " ORDER BY ot.created_at DESC"
    tasks = db.execute(query, params).fetchall()
    return jsonify({"tasks": [dict(t) for t in tasks]})


@app.route("/api/v1/tasks/oneoff", methods=["POST"])
@api_auth_required
def api_create_oneoff_task():
    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    title = data.get("title", "").strip()
    project_id = _safe_project_id(data.get("project_id"))
    if not employee_id or not title or not project_id:
        return jsonify({"error": "employee_id, title and project_id required"}), 400
    db = get_db()
    project_error = _project_required_response(db, project_id, html=False, message="valid project_id required")
    if project_error:
        return project_error
    db.execute(
        "INSERT INTO oneoff_tasks (employee_id, title, description, drive_link, project_id) VALUES (?,?,?,?,?)",
        (employee_id, title, data.get("description", ""), data.get("drive_link", ""), project_id),
    )
    db.commit()
    task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"status": "created", "task_id": task_id}), 201


@app.route("/api/v1/tasks/oneoff/<int:task_id>", methods=["PUT"])
@api_auth_required
def api_update_oneoff_task(task_id):
    db = get_db()
    task = db.execute("SELECT * FROM oneoff_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    completed_value = data.get("completed")
    completing = completed_value is True or completed_value == 1 or str(completed_value).lower() in ("1", "true", "yes")
    if "project_id" in data and not completing:
        project_error = _project_required_response(db, _safe_project_id(data.get("project_id")), html=False, message="valid project_id required")
        if project_error:
            return project_error
    if completing:
        project_id = _safe_project_id(data.get("project_id") or task["project_id"])
        project_error = _project_required_response(db, project_id, html=False)
        if project_error:
            return project_error
        data["project_id"] = project_id
    fields = []
    values = []
    for col in ["title", "description", "completed", "completion_date", "time_minutes", "notes", "drive_link", "project_id"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(task_id)
    db.execute(f"UPDATE oneoff_tasks SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/tasks/oneoff/<int:task_id>", methods=["DELETE"])
@api_auth_required
def api_delete_oneoff_task(task_id):
    db = get_db()
    db.execute("DELETE FROM oneoff_tasks WHERE id=?", (task_id,))
    db.commit()
    return jsonify({"status": "deleted"})


# -- Invoices API --

@app.route("/api/v1/invoices", methods=["GET"])
@api_auth_required
def api_list_invoices():
    db = get_db()
    client_id = request.args.get("client_id")
    status = request.args.get("status")
    query = """SELECT i.*, COALESCE(c.name, 'No Client') as client_name FROM invoices i
               LEFT JOIN clients c ON i.client_id = c.id WHERE 1=1"""
    params = []
    if client_id:
        query += " AND i.client_id=?"
        params.append(client_id)
    if status:
        query += " AND i.status=?"
        params.append(status)
    query += " ORDER BY i.invoice_date DESC"
    invoices = db.execute(query, params).fetchall()
    result = []
    for inv in invoices:
        inv_dict = dict(inv)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv["id"],)).fetchall()
        inv_dict["items"] = [dict(item) for item in items]
        result.append(inv_dict)
    return jsonify({"invoices": result})


@app.route("/api/v1/invoices/<int:inv_id>", methods=["GET"])
@api_auth_required
def api_get_invoice(inv_id):
    db = get_db()
    inv = db.execute("""SELECT i.*, COALESCE(c.name, 'No Client') as client_name FROM invoices i
                       LEFT JOIN clients c ON i.client_id = c.id WHERE i.id=?""", (inv_id,)).fetchone()
    if not inv:
        return jsonify({"error": "not_found"}), 404
    inv_dict = dict(inv)
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv_id,)).fetchall()
    inv_dict["items"] = [dict(item) for item in items]
    return jsonify({"invoice": inv_dict})


@app.route("/api/v1/invoices", methods=["POST"])
@api_auth_required
def api_create_invoice():
    data = request.get_json(silent=True) or {}
    client_id = data.get("client_id")
    invoice_number = data.get("invoice_number", "").strip()
    if not client_id or not invoice_number:
        return jsonify({"error": "client_id and invoice_number required"}), 400
    db = get_db()
    invoice_date = data.get("invoice_date", get_tz_now().date().isoformat())
    cursor = db.execute(
        """INSERT INTO invoices (client_id, invoice_number, invoice_date, due_date, subtotal,
           discount_percent, discount_amount, total, remarks, status, currency, company_profile_id, drive_link)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (client_id, invoice_number, invoice_date, data.get("due_date", ""),
         data.get("subtotal", 0), data.get("discount_percent", 0), data.get("discount_amount", 0),
         data.get("total", 0), data.get("remarks", ""), data.get("status", "draft"),
         data.get("currency", "INR"), data.get("company_profile_id", 0), data.get("drive_link", "")),
    )
    inv_id = cursor.lastrowid
    for item in data.get("items", []):
        qty = item.get("quantity", 1)
        price = item.get("unit_price", 0)
        db.execute(
            "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, total) VALUES (?,?,?,?,?)",
            (inv_id, item.get("description", ""), qty, price, qty * price),
        )
    db.commit()
    return jsonify({"status": "created", "invoice_id": inv_id}), 201


@app.route("/api/v1/invoices/<int:inv_id>", methods=["PUT"])
@api_auth_required
def api_update_invoice(inv_id):
    db = get_db()
    inv = db.execute("SELECT id FROM invoices WHERE id=?", (inv_id,)).fetchone()
    if not inv:
        return jsonify({"error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    fields = []
    values = []
    for col in ["client_id", "invoice_number", "invoice_date", "due_date", "subtotal",
                "discount_percent", "discount_amount", "total", "remarks", "status",
                "currency", "company_profile_id", "paid_date", "drive_link", "share_token"]:
        if col in data:
            fields.append(f"{col}=?")
            values.append(data[col])
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    values.append(inv_id)
    db.execute(f"UPDATE invoices SET {', '.join(fields)} WHERE id=?", values)
    # Update items if provided
    if "items" in data:
        db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
        for item in data["items"]:
            qty = item.get("quantity", 1)
            price = item.get("unit_price", 0)
            db.execute(
                "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price, total) VALUES (?,?,?,?,?)",
                (inv_id, item.get("description", ""), qty, price, qty * price),
            )
    db.commit()
    return jsonify({"status": "updated"})


@app.route("/api/v1/invoices/<int:inv_id>", methods=["DELETE"])
@api_auth_required
def api_delete_invoice(inv_id):
    db = get_db()
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
    db.commit()
    return jsonify({"status": "deleted"})


# -- Attendance API --

@app.route("/api/v1/attendance", methods=["GET"])
@api_auth_required
def api_list_attendance():
    db = get_db()
    emp_id = request.args.get("employee_id")
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    query = """SELECT a.*, e.name as employee_name FROM attendance a
               JOIN employees e ON a.employee_id = e.id WHERE 1=1"""
    params = []
    if emp_id:
        query += " AND a.employee_id=?"
        params.append(emp_id)
    if date_from:
        query += " AND a.action_date>=?"
        params.append(date_from)
    if date_to:
        query += " AND a.action_date<=?"
        params.append(date_to)
    query += " ORDER BY a.action_date DESC, a.action_time DESC"
    records = db.execute(query, params).fetchall()
    return jsonify({"attendance": [dict(r) for r in records]})


@app.route("/api/v1/attendance", methods=["POST"])
@api_auth_required
def api_record_attendance():
    data = request.get_json(silent=True) or {}
    employee_id = data.get("employee_id")
    action = data.get("action")
    if not employee_id or action not in ("checkin", "checkout"):
        return jsonify({"error": "employee_id and action (checkin/checkout) required"}), 400
    now = get_tz_now()
    action_date = data.get("date", now.date().isoformat())
    action_time = data.get("time", now.strftime("%H:%M:%S"))
    db = get_db()
    db.execute(
        "INSERT INTO attendance (employee_id, action, action_date, action_time) VALUES (?,?,?,?)",
        (employee_id, action, action_date, action_time),
    )
    db.commit()
    return jsonify({"status": "recorded"}), 201


# -- Leave API --

def api_leave_dict(row):
    if not row:
        return None
    data = dict(row)
    data["status_label"] = LEAVE_STATUS_LABELS.get(data.get("status"), data.get("status", ""))
    data["status_badge"] = LEAVE_STATUS_BADGES.get(data.get("status"), "badge-draft")
    data["start_day_label"] = LEAVE_DAY_PARTS.get(data.get("start_day_part"), "Full day")
    data["end_day_label"] = LEAVE_DAY_PARTS.get(data.get("end_day_part"), "Full day")
    return data


@app.route("/api/v1/leave-types", methods=["GET"])
@api_auth_required
def api_leave_types():
    db = get_db()
    include_inactive = request.args.get("include_inactive", "0") == "1"
    query = "SELECT * FROM leave_types"
    if not include_inactive:
        query += " WHERE active=1"
    query += " ORDER BY active DESC, paid DESC, name"
    rows = db.execute(query).fetchall()
    return jsonify({"leave_types": [dict(row) for row in rows]})


@app.route("/api/v1/leaves", methods=["GET"])
@api_auth_required
def api_list_leaves():
    db = get_db()
    status = (request.args.get("status") or "").strip()
    employee_id = request.args.get("employee_id", type=int)
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    limit = max(1, min(request.args.get("limit", 200, type=int), 500))

    where = ["1=1"]
    params = []
    if status:
        if status not in LEAVE_STATUS_VALUES:
            return jsonify({"error": "invalid_status", "allowed": sorted(LEAVE_STATUS_VALUES)}), 400
        where.append("lr.status=?")
        params.append(status)
    if employee_id:
        where.append("lr.employee_id=?")
        params.append(employee_id)
    if date_from:
        where.append("lr.end_date>=?")
        params.append(date_from)
    if date_to:
        where.append("lr.start_date<=?")
        params.append(date_to)

    rows = db.execute(f"""
        SELECT lr.*, e.name as employee_name, e.department as employee_department,
               COALESCE(lt.name, lr.leave_type_name, 'Leave') as type_name,
               COALESCE(lt.code, '') as type_code,
               COALESCE(lt.paid, 1) as paid,
               COALESCE(rb.name, '') as reviewer_name
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
        LEFT JOIN employees rb ON rb.id = lr.reviewed_by
        WHERE {' AND '.join(where)}
        ORDER BY lr.start_date DESC, lr.id DESC
        LIMIT ?
    """, [*params, limit]).fetchall()
    return jsonify({"leave_requests": [api_leave_dict(row) for row in rows]})


@app.route("/api/v1/leaves", methods=["POST"])
@api_auth_required
def api_create_leave():
    data = request.get_json(silent=True) or {}
    db = get_db()
    employee_id = api_parse_int(data.get("employee_id"), 0)
    if not employee_id:
        return jsonify({"error": "employee_id required"}), 400
    employee = db.execute("SELECT id, name FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not employee:
        return jsonify({"error": "employee_not_found"}), 404

    leave_type = None
    leave_type_id = api_parse_int(data.get("leave_type_id"), 0)
    if leave_type_id:
        leave_type = db.execute("SELECT * FROM leave_types WHERE id=? AND active=1", (leave_type_id,)).fetchone()
    if not leave_type and data.get("leave_type_code"):
        leave_type = db.execute(
            "SELECT * FROM leave_types WHERE code=? AND active=1",
            ((data.get("leave_type_code") or "").strip(),),
        ).fetchone()
    if not leave_type:
        return jsonify({"error": "valid leave_type_id or leave_type_code required"}), 400

    start_date = (data.get("start_date") or "").strip()
    end_date = (data.get("end_date") or start_date).strip()
    start_part = (data.get("start_day_part") or "full").strip()
    end_part = (data.get("end_day_part") or "full").strip()
    reason = (data.get("reason") or "").strip()
    handover_notes = (data.get("handover_notes") or "").strip()
    if not parse_iso_date(start_date) or not parse_iso_date(end_date) or parse_iso_date(start_date) > parse_iso_date(end_date):
        return jsonify({"error": "valid start_date and end_date required"}), 400
    if start_part not in LEAVE_DAY_PARTS:
        start_part = "full"
    if end_part not in LEAVE_DAY_PARTS:
        end_part = "full"
    if not reason:
        return jsonify({"error": "reason required"}), 400
    total_days = calculate_leave_days(db, start_date, end_date, start_part, end_part)
    if total_days <= 0:
        return jsonify({"error": "selected range has no working leave days"}), 400
    conflict = leave_request_overlaps(db, employee_id, start_date, end_date)
    if conflict:
        return jsonify({"error": "overlapping_leave", "leave_id": conflict["id"], "status": conflict["status"]}), 409

    actor_id = api_parse_int(data.get("created_by"), 0)
    cursor = db.execute("""
        INSERT INTO leave_requests
            (employee_id, leave_type_id, leave_type_name, start_date, end_date,
             start_day_part, end_day_part, total_days, reason, handover_notes, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        employee_id, leave_type["id"], leave_type["name"], start_date, end_date,
        start_part, end_part, total_days, reason, handover_notes, actor_id,
    ))
    leave_id = cursor.lastrowid
    add_leave_history(db, leave_id, actor_id, "", "pending", "Submitted via API")
    if actor_id:
        log_activity(db, employee_id, "requested via API", "leave",
                     f"{leave_type['name']} leave: {start_date} to {end_date}",
                     reason, notes=handover_notes, actor_id=actor_id)
    notify_leave_reviewers(db, leave_id, employee["name"], leave_type["name"], start_date, end_date)
    db.commit()
    row = db.execute("""
        SELECT lr.*, e.name as employee_name, COALESCE(lt.name, lr.leave_type_name, 'Leave') as type_name
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.id=?
    """, (leave_id,)).fetchone()
    return jsonify({"status": "created", "leave_id": leave_id, "leave": api_leave_dict(row)}), 201


@app.route("/api/v1/leaves/<int:leave_id>/status", methods=["PUT", "POST"])
@api_auth_required
def api_update_leave_status(leave_id):
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip()
    if new_status not in ("approved", "rejected", "cancelled"):
        return jsonify({"error": "status must be approved, rejected, or cancelled"}), 400
    db = get_db()
    leave = db.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        return jsonify({"error": "not_found"}), 404
    if new_status in ("approved", "rejected") and leave["status"] != "pending":
        return jsonify({"error": "only pending requests can be approved or rejected"}), 409
    if new_status == "cancelled" and leave["status"] not in ("pending", "approved"):
        return jsonify({"error": "only pending or approved requests can be cancelled"}), 409

    actor_id = api_parse_int(data.get("reviewed_by", data.get("actor_id")), 0)
    note = (data.get("review_note") or data.get("note") or "").strip()
    old_status = leave["status"]
    if new_status == "cancelled":
        db.execute("""
            UPDATE leave_requests
            SET status='cancelled', cancelled_at=datetime('now','localtime'),
                reviewed_by=COALESCE(NULLIF(?, 0), reviewed_by),
                reviewed_at=CASE WHEN ? != 0 THEN datetime('now','localtime') ELSE reviewed_at END,
                review_note=CASE WHEN ? != '' THEN ? ELSE review_note END,
                updated_at=datetime('now','localtime')
            WHERE id=?
        """, (actor_id, actor_id, note, note, leave_id))
    else:
        db.execute("""
            UPDATE leave_requests
            SET status=?, reviewed_by=?, reviewed_at=datetime('now','localtime'),
                review_note=?, updated_at=datetime('now','localtime')
            WHERE id=?
        """, (new_status, actor_id, note, leave_id))
    add_leave_history(db, leave_id, actor_id, old_status, new_status, note)
    if actor_id:
        log_activity(db, leave["employee_id"], new_status, "leave",
                     f"Leave #{leave_id}: {leave['start_date']} to {leave['end_date']}",
                     notes=note, actor_id=actor_id)
    notify_leave_employee(db, leave, LEAVE_STATUS_LABELS[new_status], note)
    db.commit()
    row = db.execute("""
        SELECT lr.*, e.name as employee_name, COALESCE(lt.name, lr.leave_type_name, 'Leave') as type_name
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        LEFT JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.id=?
    """, (leave_id,)).fetchone()
    return jsonify({"status": "updated", "leave": api_leave_dict(row)})


# -- Celebrations API --

@app.route("/api/v1/celebrations", methods=["GET"])
@api_auth_required
def api_celebrations():
    """Get upcoming celebrations (birthdays, work anniversaries, wedding anniversaries)."""
    db = get_db()
    days_ahead = int(request.args.get("days", 30))
    today = get_tz_now().date()
    employees = db.execute("SELECT id, name, birthday, joining_date, wedding_anniversary FROM employees").fetchall()
    celebrations = []
    for emp in employees:
        for field, label in [("birthday", "Birthday"), ("joining_date", "Work Anniversary"), ("wedding_anniversary", "Wedding Anniversary")]:
            val = emp[field]
            if not val:
                continue
            try:
                event_mmdd = val[5:]
                event_year = int(val[:4])
                for check_date_offset in range(days_ahead + 1):
                    check_date = today + timedelta(days=check_date_offset)
                    if check_date.strftime("%m-%d") == event_mmdd:
                        years = check_date.year - event_year
                        if label == "Birthday" or years > 0:
                            celebrations.append({
                                "employee_id": emp["id"],
                                "employee_name": emp["name"],
                                "type": label,
                                "date": check_date.isoformat(),
                                "years": years,
                                "days_until": check_date_offset,
                            })
                        break
            except Exception:
                pass
    celebrations.sort(key=lambda x: x["days_until"])
    return jsonify({"celebrations": celebrations})


# -- Notifications / Webhooks API --

@app.route("/api/v1/send-notification", methods=["POST"])
@api_auth_required
def api_send_notification():
    """Send a notification to an employee or group via Telegram."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400
    target = data.get("target", "")  # "employee", "group", "accountant"
    if target == "employee":
        emp_id = data.get("employee_id")
        if not emp_id:
            return jsonify({"error": "employee_id required for employee target"}), 400
        db = get_db()
        emp = db.execute("SELECT telegram_chat_id FROM employees WHERE id=?", (emp_id,)).fetchone()
        if not emp or not emp["telegram_chat_id"]:
            return jsonify({"error": "employee has no telegram_chat_id"}), 400
        send_telegram(emp["telegram_chat_id"], message)
        return jsonify({"status": "sent", "target": "employee", "employee_id": emp_id})
    elif target == "group":
        send_telegram_group(message)
        return jsonify({"status": "sent", "target": "group"})
    elif target == "accountant":
        acct_chat_id = get_accountant_chat_id()
        if not acct_chat_id:
            return jsonify({"error": "no accountant chat ID configured"}), 400
        send_telegram(acct_chat_id, message)
        return jsonify({"status": "sent", "target": "accountant"})
    elif target == "chat_id":
        chat_id = data.get("chat_id", "")
        if not chat_id:
            return jsonify({"error": "chat_id required"}), 400
        send_telegram(chat_id, message)
        return jsonify({"status": "sent", "target": "chat_id", "chat_id": chat_id})
    else:
        return jsonify({"error": "target must be employee, group, accountant, or chat_id"}), 400


# -- Activity Log API --

@app.route("/api/v1/activity-log", methods=["GET"])
@api_auth_required
def api_activity_log():
    db = get_db()
    emp_id = request.args.get("employee_id")
    limit = int(request.args.get("limit", 100))
    query = """SELECT al.*, e.name as employee_name FROM activity_log al
               JOIN employees e ON al.employee_id = e.id WHERE 1=1"""
    params = []
    if emp_id:
        query += " AND al.employee_id=?"
        params.append(emp_id)
    query += " ORDER BY al.created_at DESC LIMIT ?"
    params.append(limit)
    logs = db.execute(query, params).fetchall()
    return jsonify({"activity_log": [dict(l) for l in logs]})


# -- Settings API --

@app.route("/api/v1/settings", methods=["GET"])
@api_auth_required
def api_get_settings():
    db = get_db()
    settings = db.execute("SELECT key, value FROM app_settings").fetchall()
    result = {}
    for s in settings:
        if s["key"] != "deploy_key":  # don't expose deploy key
            result[s["key"]] = s["value"]
    return jsonify({"settings": result})


@app.route("/api/v1/settings", methods=["PUT"])
@api_auth_required
def api_update_settings():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "no settings to update"}), 400
    for key, value in data.items():
        if key == "deploy_key":
            continue  # protect deploy key
        set_app_setting(key, str(value))
    return jsonify({"status": "updated"})


# -- Comments API --

@app.route("/api/v1/comments", methods=["GET"])
@api_auth_required
def api_list_comments():
    db = get_db()
    task_id = request.args.get("task_id")
    task_type = request.args.get("task_type")
    query = """SELECT tc.*, e.name as employee_name FROM task_comments tc
               JOIN employees e ON tc.employee_id = e.id WHERE 1=1"""
    params = []
    if task_id:
        query += " AND tc.task_id=?"
        params.append(task_id)
    if task_type:
        query += " AND tc.task_type=?"
        params.append(task_type)
    query += " ORDER BY tc.created_at DESC"
    comments = db.execute(query, params).fetchall()
    return jsonify({"comments": [dict(c) for c in comments]})


@app.route("/api/v1/comments", methods=["POST"])
@api_auth_required
def api_create_comment():
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    task_type = data.get("task_type")
    comment = data.get("comment", "").strip()
    employee_id = data.get("employee_id")
    if not all([task_id, task_type, comment, employee_id]):
        return jsonify({"error": "task_id, task_type, comment, employee_id required"}), 400
    db = get_db()
    db.execute("INSERT INTO task_comments (task_id, task_type, employee_id, comment) VALUES (?,?,?,?)",
               (task_id, task_type, employee_id, comment))
    db.commit()
    return jsonify({"status": "created"}), 201


# -- Notifications API --

@app.route("/api/v1/notifications", methods=["GET"])
@api_auth_required
def api_notifications():
    db = get_db()
    emp_id = request.args.get("employee_id")
    unread_only = request.args.get("unread", "0") == "1"
    query = "SELECT * FROM notifications WHERE 1=1"
    params = []
    if emp_id:
        query += " AND employee_id=?"
        params.append(emp_id)
    if unread_only:
        query += " AND read=0"
    query += " ORDER BY created_at DESC LIMIT 100"
    notifs = db.execute(query, params).fetchall()
    return jsonify({"notifications": [dict(n) for n in notifs]})




# ── Reviews ──────────────────────────────────────────


@app.route("/reviews")
@login_required
def reviews_list():
    """All users can view reviews. Manager/admin/superadmin can manage."""
    db = get_db()
    role = session.get("role", "")
    user_id = session["user_id"]
    can_manage = role in MANAGER_ROLES

    # Get all review configs with project info
    configs = db.execute("""
        SELECT rc.*, p.name as project_name, p.status as project_status
        FROM review_configs rc
        JOIN projects p ON rc.project_id = p.id
        WHERE rc.active = 1
        ORDER BY p.name
    """).fetchall()

    # Get all reviews with project + config info
    status_filter = request.args.get("status", "all")
    project_filter = request.args.get("project", 0, type=int)

    query = """
        SELECT r.*, p.name as project_name, rc.frequency,
               rc.participants
        FROM reviews r
        JOIN review_configs rc ON r.review_config_id = rc.id
        JOIN projects p ON r.project_id = p.id
        WHERE 1=1
    """
    params = []
    if status_filter and status_filter != "all":
        query += " AND r.status = ?"
        params.append(status_filter)
    if project_filter:
        query += " AND r.project_id = ?"
        params.append(project_filter)
    query += " ORDER BY r.scheduled_date DESC, r.scheduled_time DESC"
    reviews = db.execute(query, params).fetchall()

    # Enrich with participant names
    enriched_reviews = []
    for rev in reviews:
        rev_dict = dict(rev)
        participant_ids = [p.strip() for p in (rev["participants"] or "").split(",") if p.strip()]
        names = []
        for pid in participant_ids:
            emp = db.execute("SELECT name FROM employees WHERE id=?", (pid,)).fetchone()
            if emp:
                names.append(emp["name"])
        rev_dict["participant_names"] = names
        # Check if current user is a participant
        rev_dict["is_participant"] = str(user_id) in participant_ids
        enriched_reviews.append(rev_dict)

    projects = db.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    employees = db.execute("SELECT id, name, role FROM employees ORDER BY name").fetchall()

    today_str = get_tz_now().date().isoformat()

    return render_template("reviews.html",
        reviews=enriched_reviews, configs=configs,
        projects=projects, employees=employees,
        can_manage=can_manage, status_filter=status_filter,
        project_filter=project_filter, today_str=today_str)


@app.route("/reviews/setup")
@manager_required
def review_setup():
    """Configure review frequency + participants per project."""
    db = get_db()
    projects = db.execute("""
        SELECT p.*, c.name as client_name
        FROM projects p
        LEFT JOIN clients c ON p.client_id = c.id
        WHERE p.status = 'active'
        ORDER BY p.name
    """).fetchall()

    configs = {}
    for row in db.execute("SELECT * FROM review_configs").fetchall():
        configs[row["project_id"]] = dict(row)

    employees = db.execute("SELECT id, name, role FROM employees ORDER BY name").fetchall()

    return render_template("review_setup.html",
        projects=projects, configs=configs, employees=employees)


@app.route("/reviews/setup/save", methods=["POST"])
@manager_required
def review_setup_save():
    """Save review config for a project."""
    db = get_db()
    project_id = request.form.get("project_id", 0, type=int)
    frequency = request.form.get("frequency", "monthly")
    participants = request.form.getlist("participants")
    active = 1 if request.form.get("active") else 0

    if not project_id:
        flash("Project is required.", "error")
        return redirect(url_for("review_setup"))

    if frequency not in ("weekly", "biweekly", "monthly", "quarterly"):
        frequency = "monthly"

    participants_str = ",".join(participants)

    existing = db.execute("SELECT id FROM review_configs WHERE project_id=?", (project_id,)).fetchone()
    if existing:
        db.execute("""UPDATE review_configs
                      SET frequency=?, participants=?, active=?
                      WHERE project_id=?""",
                   (frequency, participants_str, active, project_id))
    else:
        db.execute("""INSERT INTO review_configs (project_id, frequency, participants, active)
                      VALUES (?,?,?,?)""",
                   (project_id, frequency, participants_str, active))
    db.commit()

    project = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    pname = project["name"] if project else f"#{project_id}"
    log_activity(db, session["user_id"], "configured review",
                 f"Review setup for {pname}", f"Frequency: {frequency}, Participants: {len(participants)}")
    flash(f"Review config saved for {pname} ({frequency}).", "success")
    return redirect(url_for("review_setup"))


@app.route("/reviews/schedule", methods=["POST"])
@manager_required
def review_schedule():
    """Schedule a new review instance."""
    db = get_db()
    config_id = request.form.get("config_id", 0, type=int)
    scheduled_date = request.form.get("scheduled_date", "").strip()
    scheduled_time = request.form.get("scheduled_time", "10:00").strip()

    if not config_id or not scheduled_date:
        flash("Review config and date are required.", "error")
        return redirect(url_for("reviews_list"))

    config = db.execute("SELECT * FROM review_configs WHERE id=?", (config_id,)).fetchone()
    if not config:
        flash("Review config not found.", "error")
        return redirect(url_for("reviews_list"))

    project_id = config["project_id"]

    db.execute("""INSERT INTO reviews (review_config_id, project_id, scheduled_date, scheduled_time, status)
                  VALUES (?,?,?,?,?)""",
               (config_id, project_id, scheduled_date, scheduled_time, "pending"))
    db.commit()

    project = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    pname = project["name"] if project else f"#{project_id}"

    # Notify participants
    participant_ids = [p.strip() for p in (config["participants"] or "").split(",") if p.strip()]
    for pid in participant_ids:
        try:
            create_notification(db, int(pid),
                f"Review scheduled for {pname} on {scheduled_date} at {scheduled_time}",
                "info", "/reviews")
        except:
            pass
    db.commit()

    flash(f"Review scheduled for {pname} on {scheduled_date} at {scheduled_time}.", "success")
    return redirect(url_for("reviews_list"))


@app.route("/reviews/<int:review_id>/complete", methods=["POST"])
@manager_required
def review_complete(review_id):
    """Mark a review as complete with MOM (Minutes of Meeting)."""
    db = get_db()
    review = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not review:
        flash("Review not found.", "error")
        return redirect(url_for("reviews_list"))

    mom = request.form.get("mom", "").strip()
    if not mom:
        flash("Minutes of Meeting (MOM) are required to complete the review.", "error")
        return redirect(url_for("reviews_list"))

    now = get_tz_now()
    db.execute("""UPDATE reviews SET status='completed', mom=?, completed_at=?, completed_by=?
                  WHERE id=?""",
               (mom, now.strftime("%Y-%m-%d %H:%M"), session["user_id"], review_id))
    db.commit()

    project = db.execute("SELECT name FROM projects WHERE id=?", (review["project_id"],)).fetchone()
    pname = project["name"] if project else f"#{review['project_id']}"

    # Notify participants that review is complete
    config = db.execute("SELECT participants FROM review_configs WHERE id=?", (review["review_config_id"],)).fetchone()
    if config:
        participant_ids = [p.strip() for p in (config["participants"] or "").split(",") if p.strip()]
        for pid in participant_ids:
            try:
                create_notification(db, int(pid),
                    f"Review completed for {pname}. MOM available.",
                    "success", "/reviews")
            except:
                pass
        db.commit()

    log_activity(db, session["user_id"], "completed review",
                 f"Review for {pname}", f"MOM: {mom[:100]}...")
    flash(f"Review for {pname} marked complete.", "success")
    return redirect(url_for("reviews_list"))


@app.route("/reviews/<int:review_id>/delete", methods=["POST"])
@manager_required
def review_delete(review_id):
    """Delete a scheduled review."""
    db = get_db()
    review = db.execute("SELECT r.*, p.name as project_name FROM reviews r JOIN projects p ON r.project_id=p.id WHERE r.id=?", (review_id,)).fetchone()
    if not review:
        flash("Review not found.", "error")
        return redirect(url_for("reviews_list"))

    db.execute("DELETE FROM reviews WHERE id=?", (review_id,))
    db.commit()

    flash(f"Review for {review['project_name']} deleted.", "success")
    return redirect(url_for("reviews_list"))


# ── API Endpoints ─────────────────────────────────────

def create_db_backup_snapshot(label="manual", retain=14):
    """Create an online SQLite backup and prune old automatic backups."""
    import glob
    app_dir = os.path.dirname(os.path.abspath(__file__))
    backup_dir = get_app_setting("backup_dir", os.path.join(app_dir, "backups"))
    os.makedirs(backup_dir, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in label)[:32] or "backup"
    now = get_tz_now().strftime("%Y%m%d-%H%M%S")
    final_path = os.path.join(backup_dir, f"tasks-{safe_label}-{now}.db")
    tmp_path = f"{final_path}.tmp"
    src = dst = None
    try:
        src = open_db()
        dst = sqlite3.connect(tmp_path, timeout=DB_TIMEOUT_SECONDS)
        src.backup(dst)
        dst.close(); dst = None
        src.close(); src = None
        os.replace(tmp_path, final_path)
        if label == "auto" and retain:
            backups = sorted(glob.glob(os.path.join(backup_dir, "tasks-auto-*.db")))
            for old_path in backups[:-retain]:
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        return final_path
    finally:
        if dst:
            dst.close()
        if src:
            src.close()
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


@app.route("/admin/backup")
@login_required
@require_perm("manage_app_settings")
def admin_backup():
    """Download a SQLite snapshot. Uses the online backup API to avoid
    capturing mid-transaction state."""
    import tempfile
    now = get_tz_now().strftime("%Y%m%d-%H%M%S")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tasks-backup-", suffix=".db")
    os.close(tmp_fd)
    src = dst = None
    response_ready = False
    try:
        src = open_db()
        dst = sqlite3.connect(tmp_path, timeout=DB_TIMEOUT_SECONDS)
        src.backup(dst)
        dst.close(); dst = None
        src.close(); src = None
        db_admin = get_db()
        log_activity(db_admin, session["user_id"], "downloaded backup", "admin", f"tasks-{now}.db")
        db_admin.commit()
        response = send_file(tmp_path, as_attachment=True,
                             download_name=f"tasks-backup-{now}.db",
                             mimetype="application/x-sqlite3")
        response_ready = True

        def cleanup_tmp_backup(path=tmp_path):
            try:
                os.remove(path)
            except OSError:
                pass

        response.call_on_close(cleanup_tmp_backup)
        return response
    except Exception as e:
        flash(f"Backup failed: {e}", "error")
        return redirect(url_for("settings_page"))
    finally:
        if dst:
            dst.close()
        if src:
            src.close()
        if not response_ready:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.route("/api/health")
def api_health():
    db = None
    try:
        db = open_db()
        db.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "version": APP_VERSION, "db": "ok"})
    except Exception as e:
        logger.exception("Health check failed")
        return jsonify({"status": "degraded", "version": APP_VERSION, "db": "error", "error": str(e)}), 503
    finally:
        if db:
            db.close()


@app.route("/api/health/full")
def api_health_full():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    lock_path = os.path.join(app_dir, ".scheduler.lock")
    db_status = "ok"
    quick_check = ""
    db_size = 0
    db = None
    try:
        db = open_db()
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    except Exception as e:
        db_status = "error"
        quick_check = str(e)
    finally:
        if db:
            db.close()

    scheduler_pid = ""
    try:
        with open(lock_path, "r") as f:
            scheduler_pid = f.read().strip()
    except OSError:
        pass

    try:
        import shutil
        usage = shutil.disk_usage(app_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        disk = {}

    status = "ok" if db_status == "ok" and quick_check == "ok" else "degraded"
    return jsonify({
        "status": status,
        "version": APP_VERSION,
        "time": get_tz_now().isoformat(timespec="seconds"),
        "db": {"status": db_status, "quick_check": quick_check, "bytes": db_size},
        "scheduler": {"lock_pid": scheduler_pid},
        "disk": disk,
    }), 200 if status == "ok" else 503


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    api_key = request.headers.get("X-API-Key", "")
    stored_key = get_app_setting("deploy_key", "")
    if not api_key or api_key != stored_key:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "files" not in data:
        return jsonify({"error": "missing files dict"}), 400
    if not isinstance(data["files"], dict) or not data["files"]:
        return jsonify({"error": "files must be a non-empty object"}), 400
    if len(data["files"]) > 50:
        return jsonify({"error": "too_many_files", "max_files": 50}), 400

    app_dir = os.path.dirname(os.path.abspath(__file__))
    decoded_files = []
    total_bytes = 0
    max_bytes = 8 * 1024 * 1024
    for filename, content_b64 in data["files"].items():
        if not isinstance(filename, str) or filename.startswith("/") or "\x00" in filename:
            return jsonify({"error": "invalid_filename", "filename": str(filename)}), 400
        safe_path = os.path.normpath(os.path.join(app_dir, filename))
        if not safe_path.startswith(app_dir + os.sep):
            return jsonify({"error": "path_outside_app", "filename": filename}), 400
        if os.path.isdir(safe_path):
            return jsonify({"error": "target_is_directory", "filename": filename}), 400
        try:
            file_bytes = base64.b64decode(content_b64, validate=True)
        except Exception:
            return jsonify({"error": "invalid_base64", "filename": filename}), 400
        total_bytes += len(file_bytes)
        if total_bytes > max_bytes:
            return jsonify({"error": "payload_too_large", "max_decoded_bytes": max_bytes}), 413
        decoded_files.append((filename, safe_path, file_bytes))

    written = []
    for filename, safe_path, file_bytes in decoded_files:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        tmp_path = f"{safe_path}.tmp-{secrets.token_hex(6)}"
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, safe_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        written.append(filename)

    # Auto-restart if requested
    if data.get("restart"):
        import subprocess
        subprocess.Popen(["systemctl", "restart", "task-manager"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"status": "ok", "files_written": written, "restarted": True})

    return jsonify({"status": "ok", "files_written": written, "note": "Restart service to apply: sudo systemctl restart task-manager"})


@app.route("/api/deploy-key")
@superadmin_required
def api_deploy_key():
    key = get_app_setting("deploy_key", "")
    return jsonify({"deploy_key": key})


# ── Reports & Analytics ────────────────────────────────────────

def send_email_rich(to_emails, subject, body_html, cc_emails=None, attachments=None):
    """Send email to one or many recipients with optional CC and PDF attachments.
    to_emails: list or comma-separated string
    attachments: list of (filename, bytes, mime) tuples
    Returns (ok: bool, error: str)
    """
    if get_app_setting("smtp_enabled", "0") != "1":
        return False, "SMTP not enabled in settings"
    host = get_app_setting("smtp_host", "")
    if not host:
        return False, "SMTP host not configured"

    if isinstance(to_emails, str):
        to_list = [e.strip() for e in to_emails.replace(";", ",").split(",") if e.strip()]
    else:
        to_list = [e.strip() for e in (to_emails or []) if e and e.strip()]
    if isinstance(cc_emails, str):
        cc_list = [e.strip() for e in cc_emails.replace(";", ",").split(",") if e.strip()]
    else:
        cc_list = [e.strip() for e in (cc_emails or []) if e and e.strip()]
    if not to_list:
        return False, "No recipients"

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.mime.application import MIMEApplication

        port = int(get_app_setting("smtp_port", "587") or "587")
        user = get_app_setting("smtp_user", "")
        password = get_app_setting("smtp_password", "")
        from_email = get_app_setting("smtp_from", user)

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)

        for att in attachments or []:
            fname, data, mime = att
            maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2] if "/" in mime else ("application", "octet-stream")
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)

        all_recipients = to_list + cc_list
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_email, all_recipients, msg.as_string())
        return True, ""
    except Exception as e:
        print(f"send_email_rich error: {e}")
        return False, str(e)


def render_pdf_from_html(html_str):
    """Convert an HTML string to PDF bytes.
    Prefers xhtml2pdf if available; otherwise returns None.
    """
    try:
        from xhtml2pdf import pisa
        import io as _io
        buf = _io.BytesIO()
        pisa_status = pisa.CreatePDF(src=html_str, dest=buf, encoding="utf-8")
        if pisa_status.err:
            return None
        return buf.getvalue()
    except Exception as e:
        print(f"PDF render error: {e}")
        return None


def get_report_data(project_id, date_from, date_to):
    """Aggregate completions for a project within a date range.
    Returns a dict ready for rendering.
    Includes both recurring task completions and one-off task completions.
    """
    db = get_db()
    project = db.execute("""
        SELECT p.*, c.name as client_name, c.email as client_email,
               c.contact_person, c.phone as client_phone, c.address as client_address
        FROM projects p LEFT JOIN clients c ON p.client_id = c.id
        WHERE p.id=?
    """, (project_id,)).fetchone()
    if not project:
        return None

    # Recurring completions in range
    recurring_rows = db.execute("""
        SELECT rc.completion_date as cdate, rc.time_minutes as mins, rc.notes as notes,
               rt.id as task_id, rt.title as task_title, rt.description as task_desc,
               rt.employee_id as emp_id,
               e.name as emp_name, e.hourly_rate as hourly_rate,
               'recurring' as ttype
        FROM recurring_completions rc
        JOIN recurring_tasks rt ON rc.task_id = rt.id
        LEFT JOIN employees e ON rt.employee_id = e.id
        WHERE rt.project_id = ? AND rc.completion_date >= ? AND rc.completion_date <= ?
        ORDER BY rc.completion_date, e.name
    """, (project_id, date_from, date_to)).fetchall()

    oneoff_rows = db.execute("""
        SELECT ot.completion_date as cdate, COALESCE(ot.time_minutes,0) as mins,
               ot.notes as notes, ot.id as task_id, ot.title as task_title,
               ot.description as task_desc,
               ot.employee_id as emp_id, e.name as emp_name, e.hourly_rate as hourly_rate,
               'one-time' as ttype
        FROM oneoff_tasks ot
        LEFT JOIN employees e ON ot.employee_id = e.id
        WHERE ot.project_id = ? AND ot.completed = 1
              AND ot.completion_date >= ? AND ot.completion_date <= ?
        ORDER BY ot.completion_date, e.name
    """, (project_id, date_from, date_to)).fetchall()

    entries = []
    for r in list(recurring_rows) + list(oneoff_rows):
        entries.append({
            "date": r["cdate"],
            "emp_id": r["emp_id"],
            "emp_name": r["emp_name"] or "(unknown)",
            "hourly_rate": float(r["hourly_rate"] or 0),
            "task_title": r["task_title"],
            "task_desc": r["task_desc"] or "",
            "ttype": r["ttype"],
            "minutes": float(r["mins"] or 0),
            "notes": r["notes"] or "",
        })
    entries.sort(key=lambda x: (x["date"], x["emp_name"]))

    total_minutes = sum(e["minutes"] for e in entries)
    total_cost = sum(e["minutes"] / 60.0 * e["hourly_rate"] for e in entries)

    # Per employee breakdown
    per_emp = {}
    for e in entries:
        k = (e["emp_id"], e["emp_name"], e["hourly_rate"])
        if k not in per_emp:
            per_emp[k] = {"emp_id": e["emp_id"], "name": e["emp_name"],
                          "hourly_rate": e["hourly_rate"],
                          "minutes": 0.0, "cost": 0.0, "task_count": 0}
        per_emp[k]["minutes"] += e["minutes"]
        per_emp[k]["cost"] += e["minutes"] / 60.0 * e["hourly_rate"]
        per_emp[k]["task_count"] += 1
    employees_list = sorted(per_emp.values(), key=lambda x: -x["minutes"])

    # Per day breakdown
    per_day = {}
    for e in entries:
        per_day.setdefault(e["date"], 0.0)
        per_day[e["date"]] += e["minutes"]
    days_sorted = sorted(per_day.items())

    # Per task breakdown
    per_task = {}
    for e in entries:
        k = (e["task_title"], e["ttype"])
        if k not in per_task:
            per_task[k] = {"title": e["task_title"], "ttype": e["ttype"],
                           "minutes": 0.0, "count": 0}
        per_task[k]["minutes"] += e["minutes"]
        per_task[k]["count"] += 1
    tasks_list = sorted(per_task.values(), key=lambda x: -x["minutes"])

    currency_code = get_app_setting("default_currency", "INR")
    currency_sym = get_currency_symbol(currency_code)

    return {
        "project": dict(project),
        "date_from": date_from,
        "date_to": date_to,
        "entries": entries,
        "employees": employees_list,
        "tasks": tasks_list,
        "days": days_sorted,
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60.0, 2),
        "total_cost": round(total_cost, 2),
        "contributors": len(employees_list),
        "task_count": sum(t["count"] for t in tasks_list),
        "currency_code": currency_code,
        "currency_sym": currency_sym,
    }


def get_client_report_data(client_id, date_from, date_to):
    """Aggregate report data across all projects for a client."""
    db = get_db()
    projects = db.execute("SELECT id, name FROM projects WHERE client_id=?", (client_id,)).fetchall()
    if not projects:
        return None
    client = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not client:
        return None

    all_entries = []
    per_emp = {}
    per_task = {}
    per_day = {}
    total_minutes = 0
    total_cost = 0
    project_names = []

    for proj in projects:
        r = get_report_data(proj["id"], date_from, date_to)
        if not r:
            continue
        project_names.append(proj["name"])
        all_entries.extend(r["entries"])
        total_minutes += r["total_minutes"]
        total_cost += r["total_cost"]
        for e in r["employees"]:
            k = (e["emp_id"], e["name"], e["hourly_rate"])
            if k not in per_emp:
                per_emp[k] = {"emp_id": e["emp_id"], "name": e["name"],
                              "hourly_rate": e["hourly_rate"],
                              "minutes": 0.0, "cost": 0.0, "task_count": 0}
            per_emp[k]["minutes"] += e["minutes"]
            per_emp[k]["cost"] += e["cost"]
            per_emp[k]["task_count"] += e["task_count"]
        for t in r["tasks"]:
            k = (t["title"], t["ttype"])
            if k not in per_task:
                per_task[k] = {"title": t["title"], "ttype": t["ttype"], "minutes": 0.0, "count": 0}
            per_task[k]["minutes"] += t["minutes"]
            per_task[k]["count"] += t["count"]
        for day_date, mins in r["days"]:
            per_day[day_date] = per_day.get(day_date, 0) + mins

    if not all_entries:
        return None

    all_entries.sort(key=lambda x: (x["date"], x["emp_name"]))
    employees_list = sorted(per_emp.values(), key=lambda x: -x["minutes"])
    tasks_list = sorted(per_task.values(), key=lambda x: -x["minutes"])
    days_sorted = sorted(per_day.items())

    currency_code = get_app_setting("default_currency", "INR")
    currency_sym = get_currency_symbol(currency_code)

    return {
        "project": {"name": f"{client['name']} (All Projects)", "client_name": client["name"],
                     "client_email": client.get("email", "")},
        "project_names": project_names,
        "date_from": date_from,
        "date_to": date_to,
        "entries": all_entries,
        "employees": employees_list,
        "tasks": tasks_list,
        "days": days_sorted,
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60.0, 2),
        "total_cost": round(total_cost, 2),
        "contributors": len(employees_list),
        "task_count": sum(t["count"] for t in tasks_list),
        "currency_code": currency_code,
        "currency_sym": currency_sym,
    }


def _get_company_info(use_branding=True):
    if not use_branding:
        return None
    return {
        "name": get_app_setting("company_name", get_app_setting("app_name", "Our Company")),
        "logo_url": get_app_setting("company_logo_url", get_app_setting("app_logo_url", "")),
        "address": get_app_setting("company_address", ""),
        "email": get_app_setting("company_email", ""),
        "phone": get_app_setting("company_phone", ""),
    }


def _compute_range(range_key, date_from=None, date_to=None):
    today = get_tz_now().date()
    if range_key == "this_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif range_key == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
    elif range_key == "this_month":
        start = today.replace(day=1)
        import calendar as _cal
        _, ld = _cal.monthrange(today.year, today.month)
        end = today.replace(day=ld)
    elif range_key == "last_month":
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
    elif range_key == "last_30":
        end = today
        start = today - timedelta(days=29)
    elif range_key == "custom" and date_from and date_to:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:
            start = today.replace(day=1)
            end = today
    else:
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    return start.isoformat(), end.isoformat()


@app.route("/reports")
@admin_required
def reports_home():
    db = get_db()
    projects = db.execute("""
        SELECT p.id, p.name, p.status, c.name as client_name, c.email as client_email
        FROM projects p LEFT JOIN clients c ON p.client_id = c.id
        ORDER BY p.status, p.name
    """).fetchall()

    project_id = request.args.get("project_id", 0, type=int)
    client_id = request.args.get("client_id", 0, type=int)
    range_key = request.args.get("range", "this_month")
    date_from_q = request.args.get("date_from", "")
    date_to_q = request.args.get("date_to", "")
    date_from, date_to = _compute_range(range_key, date_from_q, date_to_q)

    report = None
    if project_id:
        report = get_report_data(project_id, date_from, date_to)
    elif client_id:
        report = get_client_report_data(client_id, date_from, date_to)

    clients = db.execute("SELECT id, name FROM clients ORDER BY name").fetchall()

    # Preset subject
    default_subject = ""
    default_to = ""
    if report:
        default_subject = f"Project Report: {report['project']['name']} ({date_from} to {date_to})"
        default_to = report['project'].get("client_email") or ""

    return render_template("reports.html",
        projects=projects,
        project_id=project_id,
        clients=clients,
        client_id=client_id,
        range_key=range_key,
        date_from=date_from,
        date_to=date_to,
        report=report,
        default_subject=default_subject,
        default_to=default_to,
        currency_sym=get_currency_symbol(get_app_setting("default_currency", "INR")),
    )


@app.route("/reports/pdf")
@admin_required
def reports_pdf():
    project_id = request.args.get("project_id", 0, type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    include_branding = request.args.get("branding", "1") != "0"
    summary = request.args.get("summary", "")
    if not project_id or not date_from or not date_to:
        return redirect(url_for("reports_home"))
    report = get_report_data(project_id, date_from, date_to)
    if not report:
        return redirect(url_for("reports_home"))
    company = _get_company_info(include_branding)
    html = render_template("report_pdf.html", report=report, company=company,
                           include_branding=include_branding, summary=summary,
                           for_pdf=True)
    pdf_bytes = render_pdf_from_html(html)
    if pdf_bytes:
        resp = make_response(pdf_bytes)
        resp.headers["Content-Type"] = "application/pdf"
        fname = f"report_{report['project']['name']}_{date_from}_{date_to}.pdf".replace(" ", "_")
        resp.headers["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp
    # Fallback: return print-styled HTML (user can Ctrl+P → Save as PDF)
    return html


@app.route("/reports/send", methods=["POST"])
@admin_required
def reports_send():
    project_id = request.form.get("project_id", 0, type=int)
    date_from = request.form.get("date_from", "")
    date_to = request.form.get("date_to", "")
    to_emails = request.form.get("to_emails", "").strip()
    cc_emails = request.form.get("cc_emails", "").strip()
    subject = request.form.get("subject", "").strip()
    summary = request.form.get("summary", "").strip()
    include_branding = 1 if request.form.get("include_branding") else 0
    attach_pdf = 1 if request.form.get("attach_pdf") else 0

    if not project_id or not date_from or not date_to or not to_emails or not subject:
        flash("Project, date range, subject and recipients are required.", "error")
        return redirect(url_for("reports_home", project_id=project_id, range="custom",
                                date_from=date_from, date_to=date_to))

    report = get_report_data(project_id, date_from, date_to)
    if not report:
        flash("Project not found.", "error")
        return redirect(url_for("reports_home"))

    company = _get_company_info(bool(include_branding))
    body_html = render_template("report_email.html", report=report, company=company,
                                include_branding=bool(include_branding), summary=summary)

    attachments = []
    if attach_pdf:
        pdf_html = render_template("report_pdf.html", report=report, company=company,
                                   include_branding=bool(include_branding),
                                   summary=summary, for_pdf=True)
        pdf_bytes = render_pdf_from_html(pdf_html)
        if pdf_bytes:
            fname = f"report_{report['project']['name']}_{date_from}_{date_to}.pdf".replace(" ", "_")
            attachments.append((fname, pdf_bytes, "application/pdf"))
        else:
            flash("PDF library not available on server — sent without attachment. Install xhtml2pdf to enable.", "warning")

    ok, err = send_email_rich(to_emails, subject, body_html, cc_emails, attachments)

    db = get_db()
    db.execute("""
        INSERT INTO report_log (project_id, date_from, date_to, recipients, cc, subject,
                                sent_by, sent_via, success, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (project_id, date_from, date_to, to_emails, cc_emails, subject,
          session.get("user_id"), "manual", 1 if ok else 0, err or ""))
    db.commit()

    if ok:
        flash(f"Report sent to {to_emails}" + (f" (cc: {cc_emails})" if cc_emails else ""), "success")
    else:
        flash(f"Email send failed: {err}", "error")

    return redirect(url_for("reports_home", project_id=project_id, range="custom",
                            date_from=date_from, date_to=date_to))


@app.route("/reports/schedules")
@admin_required
def reports_schedules():
    db = get_db()
    schedules = db.execute("""
        SELECT rs.*, p.name as project_name, c.name as client_name, c.email as client_email
        FROM report_schedules rs
        LEFT JOIN projects p ON rs.project_id = p.id
        LEFT JOIN clients c ON p.client_id = c.id
        ORDER BY rs.active DESC, p.name
    """).fetchall()
    projects = db.execute("""
        SELECT p.id, p.name, c.name as client_name, c.email as client_email
        FROM projects p LEFT JOIN clients c ON p.client_id = c.id
        WHERE p.status='active' ORDER BY p.name
    """).fetchall()
    recent_sends = db.execute("""
        SELECT rl.*, p.name as project_name
        FROM report_log rl LEFT JOIN projects p ON rl.project_id = p.id
        ORDER BY rl.id DESC LIMIT 25
    """).fetchall()
    return render_template("report_schedules.html",
        schedules=schedules, projects=projects, recent_sends=recent_sends)


@app.route("/reports/schedules/save", methods=["POST"])
@admin_required
def reports_schedules_save():
    sched_id = request.form.get("id", 0, type=int)
    project_id = request.form.get("project_id", 0, type=int)
    frequency = request.form.get("frequency", "weekly").strip()
    frequency_day = request.form.get("frequency_day", 1, type=int)
    recipients = request.form.get("recipients", "").strip()
    cc = request.form.get("cc", "").strip()
    subject_template = request.form.get("subject_template", "").strip()
    include_branding = 1 if request.form.get("include_branding") else 0
    active = 1 if request.form.get("active") else 0
    if not project_id or not recipients:
        flash("Project and recipients are required.", "error")
        return redirect(url_for("reports_schedules"))
    db = get_db()
    if sched_id:
        db.execute("""
            UPDATE report_schedules SET project_id=?, frequency=?, frequency_day=?,
                recipients=?, cc=?, subject_template=?, include_branding=?, active=?
            WHERE id=?
        """, (project_id, frequency, frequency_day, recipients, cc, subject_template,
              include_branding, active, sched_id))
        flash("Schedule updated.", "success")
    else:
        db.execute("""
            INSERT INTO report_schedules (project_id, frequency, frequency_day,
                recipients, cc, subject_template, include_branding, active, created_by)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (project_id, frequency, frequency_day, recipients, cc, subject_template,
              include_branding, active, session.get("user_id")))
        flash("Schedule created.", "success")
    db.commit()
    return redirect(url_for("reports_schedules"))


@app.route("/reports/schedules/<int:sched_id>/delete", methods=["POST"])
@admin_required
def reports_schedules_delete(sched_id):
    db = get_db()
    db.execute("DELETE FROM report_schedules WHERE id=?", (sched_id,))
    db.commit()
    flash("Schedule deleted.", "success")
    return redirect(url_for("reports_schedules"))


@app.route("/reports/schedules/<int:sched_id>/toggle", methods=["POST"])
@admin_required
def reports_schedules_toggle(sched_id):
    db = get_db()
    sched = db.execute("SELECT active FROM report_schedules WHERE id=?", (sched_id,)).fetchone()
    if sched:
        db.execute("UPDATE report_schedules SET active=? WHERE id=?", (0 if sched["active"] else 1, sched_id))
        db.commit()
        flash("Schedule updated.", "success")
    return redirect(url_for("reports_schedules"))


@app.route("/reports/schedules/<int:sched_id>/run", methods=["POST"])
@admin_required
def reports_schedules_run(sched_id):
    """Trigger a scheduled send immediately."""
    ok, msg = _run_scheduled_report(sched_id, force=True)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("reports_schedules"))


def _run_scheduled_report(sched_id, force=False):
    """Execute one scheduled report send. Returns (ok, message)."""
    db = get_db()
    sched = db.execute("SELECT * FROM report_schedules WHERE id=?", (sched_id,)).fetchone()
    if not sched:
        return False, "Schedule not found"
    if not sched["active"] and not force:
        return False, "Schedule inactive"

    today = get_tz_now().date()
    freq = sched["frequency"]
    if freq == "weekly":
        # Report covers previous 7 days (last Mon..Sun or a trailing 7-day window)
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
    elif freq == "biweekly":
        end = today - timedelta(days=1)
        start = end - timedelta(days=13)
    elif freq == "monthly":
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
    else:
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)

    report = get_report_data(sched["project_id"], start.isoformat(), end.isoformat())
    if not report:
        return False, "Project not found"

    subject_tpl = sched["subject_template"] or "Project Report: {project} ({from} to {to})"
    subject = (subject_tpl
        .replace("{project}", report["project"]["name"])
        .replace("{from}", start.isoformat())
        .replace("{to}", end.isoformat())
        .replace("{client}", report["project"].get("client_name") or ""))

    company = _get_company_info(bool(sched["include_branding"]))
    body_html = render_template("report_email.html", report=report, company=company,
                                include_branding=bool(sched["include_branding"]), summary="")

    attachments = []
    pdf_html = render_template("report_pdf.html", report=report, company=company,
                               include_branding=bool(sched["include_branding"]),
                               summary="", for_pdf=True)
    pdf_bytes = render_pdf_from_html(pdf_html)
    if pdf_bytes:
        fname = f"report_{report['project']['name']}_{start.isoformat()}_{end.isoformat()}.pdf".replace(" ", "_")
        attachments.append((fname, pdf_bytes, "application/pdf"))

    ok, err = send_email_rich(sched["recipients"], subject, body_html, sched["cc"], attachments)

    db.execute("""
        INSERT INTO report_log (project_id, date_from, date_to, recipients, cc, subject,
                                sent_by, sent_via, success, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (sched["project_id"], start.isoformat(), end.isoformat(),
          sched["recipients"], sched["cc"], subject, None,
          "auto" if not force else "manual-run", 1 if ok else 0, err or ""))
    db.execute("UPDATE report_schedules SET last_sent_date=? WHERE id=?",
               (today.isoformat(), sched_id))
    db.commit()
    return ok, ("Sent" if ok else f"Failed: {err}")


def run_scheduled_reports():
    """Called daily by scheduler. Sends any reports due today."""
    db = open_db()
    db.row_factory = sqlite3.Row
    today = datetime.now(get_tz()).date()
    today_str = today.isoformat()
    try:
        schedules = db.execute("SELECT * FROM report_schedules WHERE active=1").fetchall()
        for sched in schedules:
            if sched["last_sent_date"] == today_str:
                continue
            freq = sched["frequency"]
            fday = sched["frequency_day"] or 0
            due = False
            if freq == "weekly":
                # frequency_day: 0=Mon..6=Sun
                due = (today.weekday() == fday)
            elif freq == "biweekly":
                # send every 14 days aligned to weekday fday
                if today.weekday() == fday:
                    last = sched["last_sent_date"]
                    if not last:
                        due = True
                    else:
                        try:
                            ld = datetime.strptime(last, "%Y-%m-%d").date()
                            due = (today - ld).days >= 14
                        except Exception:
                            due = True
            elif freq == "monthly":
                # frequency_day: day of month (1..28)
                target_day = max(1, min(28, fday or 1))
                due = (today.day == target_day)
            if due:
                # Use a fresh app context to render templates
                with app.app_context(), app.test_request_context():
                    _run_scheduled_report(sched["id"], force=False)
    except Exception as e:
        print(f"run_scheduled_reports error: {e}")
    finally:
        db.close()


# ── Unassigned Tasks Backfill ────────────────────────────

@app.route("/tasks/unassigned")
@admin_required
def tasks_unassigned():
    db = get_db()
    recurring = db.execute("""
        SELECT rt.*, e.name as emp_name
        FROM recurring_tasks rt
        LEFT JOIN employees e ON rt.employee_id = e.id
        WHERE COALESCE(rt.project_id, 0) = 0 AND rt.active = 1
        ORDER BY e.name, rt.title
    """).fetchall()
    oneoff = db.execute("""
        SELECT ot.*, e.name as emp_name
        FROM oneoff_tasks ot
        LEFT JOIN employees e ON ot.employee_id = e.id
        WHERE COALESCE(ot.project_id, 0) = 0
        ORDER BY e.name, ot.title
    """).fetchall()
    projects = db.execute("""
        SELECT p.id, p.name, c.name as client_name
        FROM projects p LEFT JOIN clients c ON p.client_id = c.id
        WHERE p.status='active' ORDER BY p.name
    """).fetchall()
    return render_template("unassigned_tasks.html",
        recurring=recurring, oneoff=oneoff, projects=projects)


@app.route("/tasks/assign-project", methods=["POST"])
@admin_required
def tasks_assign_project():
    task_id = request.form.get("task_id", 0, type=int)
    task_type = request.form.get("task_type", "")
    project_id = request.form.get("project_id", 0, type=int)
    db = get_db()
    if task_type == "recurring":
        db.execute("UPDATE recurring_tasks SET project_id=? WHERE id=?", (project_id, task_id))
    elif task_type == "oneoff":
        db.execute("UPDATE oneoff_tasks SET project_id=? WHERE id=?", (project_id, task_id))
    db.commit()
    return redirect(request.referrer or url_for("tasks_unassigned"))


@app.route("/tasks/bulk-assign", methods=["POST"])
@admin_required
def tasks_bulk_assign():
    project_id = request.form.get("project_id", 0, type=int)
    ids_recurring = request.form.getlist("recurring_ids")
    ids_oneoff = request.form.getlist("oneoff_ids")
    db = get_db()
    count = 0
    for tid in ids_recurring:
        try:
            db.execute("UPDATE recurring_tasks SET project_id=? WHERE id=?", (project_id, int(tid)))
            count += 1
        except Exception:
            pass
    for tid in ids_oneoff:
        try:
            db.execute("UPDATE oneoff_tasks SET project_id=? WHERE id=?", (project_id, int(tid)))
            count += 1
        except Exception:
            pass
    db.commit()
    flash(f"Linked {count} task(s) to project.", "success")
    return redirect(url_for("tasks_unassigned"))


# ── Initialize DB on startup ──────────────────────────────
init_db()

_scheduler_lock_handle = None


def acquire_scheduler_lock():
    """Allow exactly one gunicorn worker to run the background scheduler."""
    global _scheduler_lock_handle
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scheduler.lock")
    try:
        import fcntl
        lock_file = open(lock_path, "a+")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n")
        lock_file.flush()
        _scheduler_lock_handle = lock_file
        return True
    except BlockingIOError:
        return False
    except Exception as e:
        print(f"scheduler lock unavailable: {e}")
        return False


_sched = None
if os.environ.get("TASK_MANAGER_DISABLE_SCHEDULER", "") != "1":
    if acquire_scheduler_lock():
        _sched = threading.Thread(target=scheduler_loop, daemon=True)
        _sched.start()
        print("Background scheduler started in this worker.", flush=True)
    else:
        print("Background scheduler already active in another worker.", flush=True)

# ── Startup ─────────────────────────────────────────────


if __name__ == "__main__":
    print(f"Task Manager v{APP_VERSION}")
    print("Telegram reminders enabled (8 AM / 5 PM)" if is_telegram_enabled() else "Telegram not configured (can be set in Settings).")
    print("Task Manager running at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=True)
