# Copyright (c) 2026, Smurik Solutions (built on original by osama.ahmed@deliverydevs.com)
# For license information, please see license.txt
#
# ZKTeco Checkin Sync — multi-device edition.
#
# Functional additions over the single-device version:
#   1. Multi-device support        -> loops over ZKTeco Config.devices child table
#   2. Device-scoped sync cursors  -> each device row has its own last_sync
#   3. Unmapped user detection     -> unmapped emp_codes routed to ZKTeco Punch Review
#   4. Two-way sync (push)         -> push_employee_to_device()
#   5. Sync log / audit            -> one ZKTeco Sync Log row per device per run
#   6. Raw transaction retention   -> device transaction id stored + used as dedup key
#   7. Manual review queue         -> ambiguous punches -> ZKTeco Punch Review (Pending)
#
# Backwards compatibility: if no devices are configured in the child table, the
# code falls back to the legacy single-device fields (server_ip/server_port/token)
# so existing installs keep working until migrated.

import frappe
from frappe.model.document import Document
from frappe import _
import requests
from frappe.utils import today, now_datetime, get_datetime, cint, get_url_to_list
from datetime import datetime, timedelta
import json
import time


# ---------------------------------------------------------------------------
# Tunables (overridable via ZKTeco Config fields if they exist)
# ---------------------------------------------------------------------------
DEFAULT_OVERLAP_HOURS = 24
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 500
HTTP_TIMEOUT = 60


def _cfg_int(cfg, fieldname, default):
    try:
        val = getattr(cfg, fieldname, None)
        if val:
            return int(val)
    except Exception:
        pass
    return default


class ZKTecoConfig(Document):
    pass


# ===========================================================================
# Device resolution (multi-device + legacy fallback)  [#1, #2]
# ===========================================================================

class _Device:
    """Lightweight view over a device: either a child row or the legacy single."""

    def __init__(self, name, server_ip, server_port, token, username=None,
                 password=None, last_sync=None, source_row=None):
        self.name = name
        self.server_ip = server_ip
        self.server_port = server_port
        self.token = token
        self.username = username
        self.password = password
        self.last_sync = last_sync
        self.source_row = source_row  # child docname, or None for legacy


def _get_devices(cfg):
    """
    Return a list of _Device. Prefer the `devices` child table; fall back to
    the legacy single-device config fields when the table is empty/absent.
    """
    devices = []

    child_rows = getattr(cfg, "devices", None) or []
    for row in child_rows:
        if not row.get("enabled"):
            continue
        if not (row.get("server_ip") and row.get("server_port") and row.get("token")):
            continue
        devices.append(_Device(
            name=row.get("device_name") or f"{row.get('server_ip')}:{row.get('server_port')}",
            server_ip=row.get("server_ip"),
            server_port=row.get("server_port"),
            token=row.get("token"),
            username=row.get("username"),
            password=row.get("password"),
            last_sync=row.get("last_sync"),
            source_row=row.get("name"),
        ))

    if devices:
        return devices

    # Legacy single-device fallback.
    if cfg.get("server_ip") and cfg.get("server_port") and cfg.get("token"):
        devices.append(_Device(
            name="Default (legacy)",
            server_ip=cfg.get("server_ip"),
            server_port=cfg.get("server_port"),
            token=cfg.get("token"),
            username=cfg.get("username"),
            password=cfg.get("get_password") and cfg.get_password("password") or None,
            last_sync=cfg.get("last_sync"),
            source_row=None,
        ))

    return devices


def _set_device_cursor(cfg, device, when, created_delta):
    """Persist last_sync + total for a device (child row or legacy single)."""
    if device.source_row:
        frappe.db.set_value(
            "ZKTeco Device", device.source_row,
            {"last_sync": when},
            update_modified=False,
        )
        # bump total on the child row
        prev = frappe.db.get_value("ZKTeco Device", device.source_row, "total_synced_records") or 0
        frappe.db.set_value(
            "ZKTeco Device", device.source_row,
            {"total_synced_records": prev + created_delta},
            update_modified=False,
        )
    else:
        # Legacy single fields on the parent Single.
        frappe.db.set_single_value("ZKTeco Config", "last_sync", when)
        prev = frappe.db.get_single_value("ZKTeco Config", "total_synced_records") or 0
        frappe.db.set_single_value("ZKTeco Config", "total_synced_records", prev + created_delta)


# ===========================================================================
# Token / connection
# ===========================================================================

@frappe.whitelist()
def register_api_token(device_name=None):
    """
    Register a DRF API token. If device_name is given, register for that child
    device; otherwise use the legacy single-device fields.
    """
    cfg = frappe.get_single("ZKTeco Config")

    if device_name:
        row = _find_child_device(cfg, device_name)
        if not row:
            frappe.throw(_("Device '{0}' not found.").format(device_name))
        server_ip, server_port = row.server_ip, row.server_port
        username = (row.username or "").strip()
        password = row.get_password("password") if row.get("password") else None
    else:
        server_ip, server_port = cfg.server_ip, cfg.server_port
        username = (cfg.username or "").strip()
        password = cfg.get_password("password")

    if not all([server_ip, server_port, username, password]):
        frappe.throw(_("Please configure server IP, port, username, and password."))

    login_url = f"http://{server_ip}:{server_port}/api-token-auth/"
    try:
        response = requests.post(
            login_url, json={"username": username, "password": password}, timeout=15
        )
        if response.status_code != 200:
            frappe.throw(_("Login Failed: {0}").format(response.text))
        token = response.json().get("token")
        if not token:
            frappe.throw(_("Token not found: {0}").format(response.text))

        if device_name:
            frappe.db.set_value("ZKTeco Device", row.name, "token", token)
        else:
            frappe.db.set_single_value("ZKTeco Config", "token", token)
        frappe.db.commit()

        return {"success": True, "message": "Token registered successfully", "token": token}

    except requests.exceptions.RequestException as e:
        frappe.throw(_("Connection error: {0}").format(str(e)))


def _find_child_device(cfg, device_name):
    for row in (getattr(cfg, "devices", None) or []):
        if (row.device_name or "") == device_name:
            return row
    return None


@frappe.whitelist()
def test_connection(device_name=None):
    """Test connection for a device (or legacy single) and preview today's punches."""
    cfg = frappe.get_single("ZKTeco Config")
    devices = _get_devices(cfg)
    if device_name:
        devices = [d for d in devices if d.name == device_name]
    if not devices:
        return {"ok": False, "error": _("No usable device/token configured.")}

    device = devices[0]
    day = today()
    start_dt = get_datetime(f"{day} 00:00:00")
    end_dt = get_datetime(f"{day} 23:59:59")

    try:
        transactions = _fetch(device, start_dt, end_dt, cfg)
        preview = []
        for t in transactions[:5]:
            emp_code = t.get("emp_code")
            employee = find_employee_by_code(emp_code)
            punch_dt = get_datetime(t.get("punch_time")) if t.get("punch_time") else None
            preview.append({
                "id": t.get("id"),
                "employee_code": emp_code,
                "erpnext_employee": employee,
                "punch_time": t.get("punch_time"),
                "log_type": resolve_log_type(t.get("punch_state"), t.get("punch_state_display"), employee, punch_dt),
                "device_id": t.get("terminal_alias") or t.get("terminal_sn"),
            })
        return {
            "ok": True,
            "device": device.name,
            "total_transactions": len(transactions),
            "transactions_preview": preview,
            "message": f"[{device.name}] Found {len(transactions)} transactions for {day}",
        }
    except Exception as e:
        return {"ok": False, "error": f"[{device.name}] {str(e)}"}


# ===========================================================================
# Fetch (paginated)  [#1 scale-safe]
# ===========================================================================

def _fetch(device, start_time, end_time, cfg):
    """Fetch ALL transactions for a device+window, following pagination."""
    page_size = _cfg_int(cfg, "page_size", DEFAULT_PAGE_SIZE)
    base_url = f"http://{device.server_ip}:{device.server_port}/iclock/api/transactions/"
    headers = {"Content-Type": "application/json", "Authorization": f"Token {device.token}"}

    all_rows = []
    next_url = base_url
    params = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "page_size": page_size,
        "page": 1,
    }
    pages = 0
    use_next_url = False

    while next_url and pages < MAX_PAGES:
        pages += 1
        if use_next_url:
            resp = requests.get(next_url, headers=headers, timeout=HTTP_TIMEOUT)
        else:
            resp = requests.get(next_url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            rows = data.get("data")
            if rows is None:
                rows = data.get("results")
            if rows is None:
                rows = []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []

        all_rows.extend(rows)

        server_next = data.get("next") if isinstance(data, dict) else None
        if server_next:
            next_url = server_next
            use_next_url = True
            continue
        if isinstance(data, dict) and not use_next_url:
            if len(rows) < page_size:
                break
            params["page"] += 1
            next_url = base_url
            continue
        break

    if pages >= MAX_PAGES:
        frappe.log_error(
            f"[{device.name}] fetch hit MAX_PAGES; results may be truncated.",
            "ZKTeco Pagination Cap",
        )
    return all_rows


# ===========================================================================
# Sync orchestration  [#1, #2, #5]
# ===========================================================================

def sync_zkteco_transactions():
    """Scheduled/main sync across ALL enabled devices, each with its own cursor + log."""
    cfg = frappe.get_single("ZKTeco Config")
    if not cfg.enable_sync:
        return

    devices = _get_devices(cfg)
    if not devices:
        frappe.log_error("No ZKTeco device/token configured", "ZKTeco Sync")
        return

    overlap = _cfg_int(cfg, "overlap_hours", DEFAULT_OVERLAP_HOURS)
    current_time = now_datetime()

    for device in devices:
        _sync_one_device(cfg, device, current_time, overlap, sync_type="Scheduled")


def _sync_one_device(cfg, device, current_time, overlap, sync_type):
    started = time.monotonic()

    if not device.last_sync:
        window_start = current_time - timedelta(days=1)
    else:
        ls = get_datetime(device.last_sync)
        window_start = (current_time - timedelta(days=1)) if ls.year < 2000 else (ls - timedelta(hours=overlap))

    status = "Success"
    detail = ""
    counts = {"created": 0, "skipped": 0, "errors": 0, "reviewed": 0, "error_samples": []}
    fetched = 0

    try:
        transactions = _fetch(device, window_start, current_time, cfg)
        fetched = len(transactions)
        counts = _process_transactions(transactions, device)
        _set_device_cursor(cfg, device, current_time, counts["created"])
        frappe.db.commit()
        if counts["errors"]:
            status = "Completed with Errors"
            detail = f"Error emp_codes: {counts['error_samples']}"
    except Exception as e:
        status = "Failed"
        detail = str(e)
        frappe.log_error(f"[{device.name}] sync failed: {str(e)}", "ZKTeco Sync Fatal Error")

    duration = round(time.monotonic() - started, 2)
    _write_sync_log(sync_type, device.name, status, window_start, current_time,
                    duration, fetched, counts, detail)


def _write_sync_log(sync_type, device_name, status, window_start, window_end,
                    duration, fetched, counts, detail):
    try:
        frappe.get_doc({
            "doctype": "ZKTeco Sync Log",
            "sync_type": sync_type,
            "device_name": device_name,
            "status": status,
            "window_start": window_start,
            "window_end": window_end,
            "duration_seconds": duration,
            "fetched": fetched,
            "created": counts.get("created", 0),
            "skipped": counts.get("skipped", 0),
            "errors": counts.get("errors", 0),
            "routed_to_review": counts.get("reviewed", 0),
            "detail": detail[:2000] if detail else "",
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        # Never let logging failure break the sync.
        frappe.log_error(f"ZKTeco Sync Log write failed: {str(e)}", "ZKTeco Sync Log")


def _process_transactions(transactions, device):
    created = skipped = errors = reviewed = 0
    error_samples = []

    for t in (transactions or []):
        try:
            result = create_employee_checkin(t, device, commit=False)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
            elif result == "review":
                reviewed += 1
            else:
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append(str(t.get("emp_code")))
        except Exception as e:
            errors += 1
            if len(error_samples) < 5:
                error_samples.append(f"{t.get('emp_code')}: {e}")
            frappe.log_error(f"[{device.name}] checkin error {t}: {str(e)}", "ZKTeco Sync Error")

    frappe.db.commit()
    return {"created": created, "skipped": skipped, "errors": errors,
            "reviewed": reviewed, "error_samples": error_samples}


@frappe.whitelist()
def backfill_range(from_datetime, to_datetime, device_name=None):
    """Re-pull buffered logs for a window across one or all devices. Does not touch cursors."""
    cfg = frappe.get_single("ZKTeco Config")
    devices = _get_devices(cfg)
    if device_name:
        devices = [d for d in devices if d.name == device_name]
    if not devices:
        frappe.throw(_("No usable device/token configured."))

    start = get_datetime(from_datetime)
    end = get_datetime(to_datetime)
    if start >= end:
        frappe.throw(_("'From' must be earlier than 'To'."))

    grand = {"fetched": 0, "created": 0, "skipped": 0, "errors": 0, "reviewed": 0}
    per_device = []

    for device in devices:
        started = time.monotonic()
        try:
            transactions = _fetch(device, start, end, cfg)
            counts = _process_transactions(transactions, device)
            duration = round(time.monotonic() - started, 2)
            _write_sync_log("Backfill", device.name,
                            "Completed with Errors" if counts["errors"] else "Success",
                            start, end, duration, len(transactions), counts,
                            f"Error emp_codes: {counts['error_samples']}" if counts["errors"] else "")
            grand["fetched"] += len(transactions)
            for k in ("created", "skipped", "errors", "reviewed"):
                grand[k] += counts[k]
            per_device.append(f"{device.name}: {counts['created']} created, {counts['skipped']} present, {counts['reviewed']} review, {counts['errors']} err")
        except Exception as e:
            frappe.log_error(f"[{device.name}] backfill failed: {str(e)}", "ZKTeco Backfill Error")
            per_device.append(f"{device.name}: FAILED ({e})")

    return {
        "success": True,
        "message": f"Backfill {from_datetime} → {to_datetime}\n" + "\n".join(per_device),
        **grand,
    }


# ===========================================================================
# Log type resolution (stateless)  [#7 feeds review]
# ===========================================================================

def resolve_log_type(punch_state, punch_state_display, employee, punch_datetime):
    display = str(punch_state_display).strip().lower() if punch_state_display else ""
    state = str(punch_state).strip() if punch_state is not None else ""
    if state == "0" or display in ("check in", "in", "checkin"):
        return "IN"
    if state == "1" or display in ("check out", "out", "checkout"):
        return "OUT"
    return _log_type_from_shift(employee, punch_datetime)


def _is_ambiguous_state(punch_state, punch_state_display):
    """True when the device gave no usable IN/OUT signal (e.g. 255/blank)."""
    display = str(punch_state_display).strip().lower() if punch_state_display else ""
    state = str(punch_state).strip() if punch_state is not None else ""
    if state in ("0", "1"):
        return False
    if display in ("check in", "in", "checkin", "check out", "out", "checkout"):
        return False
    return True


def _log_type_from_shift(employee, punch_datetime):
    if not employee or not punch_datetime:
        return _noon_split(punch_datetime)
    try:
        shift_type = _get_employee_shift_type(employee)
        if shift_type:
            st = frappe.db.get_value("Shift Type", shift_type, ["start_time", "end_time"], as_dict=True)
            if st and st.start_time is not None and st.end_time is not None:
                start_sec = _timedelta_to_seconds(st.start_time)
                end_sec = _timedelta_to_seconds(st.end_time)
                overnight = end_sec <= start_sec
                if overnight:
                    end_sec += 24 * 3600
                punch_sec = punch_datetime.hour * 3600 + punch_datetime.minute * 60 + punch_datetime.second
                if overnight and punch_sec < start_sec:
                    punch_sec += 24 * 3600
                midpoint = (start_sec + end_sec) / 2.0
                return "IN" if punch_sec <= midpoint else "OUT"
    except Exception as e:
        frappe.log_error(f"Shift-based log_type failed for {employee}: {str(e)}", "ZKTeco Log Type")
    return _noon_split(punch_datetime)


def _noon_split(punch_datetime):
    try:
        return "IN" if punch_datetime.hour < 12 else "OUT"
    except Exception:
        return "IN"


def _timedelta_to_seconds(value):
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    parts = str(value).split(":")
    parts = [int(float(p)) for p in parts] + [0, 0, 0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _get_employee_shift_type(employee):
    shift = frappe.db.sql(
        """
        SELECT shift_type FROM `tabShift Assignment`
        WHERE employee = %s AND docstatus = 1
        ORDER BY creation DESC LIMIT 1
        """,
        (employee,), as_dict=True,
    )
    if shift and shift[0].shift_type:
        return shift[0].shift_type
    return frappe.db.get_value("Employee", employee, "default_shift")


# ===========================================================================
# Check-in creation  [#3 unmapped, #6 raw retention, #7 review routing]
# ===========================================================================

def _review_route_enabled(cfg):
    """Whether ambiguous punches go to review (default) or are auto-guessed."""
    val = getattr(cfg, "route_ambiguous_to_review", None)
    # Default ON when the field is absent.
    return True if val is None else bool(val)


def create_employee_checkin(transaction, device=None, commit=True):
    """
    Returns: "created" | "skipped" | "review" | False
    """
    try:
        emp_code = transaction.get("emp_code")
        punch_time = transaction.get("punch_time")
        punch_state = transaction.get("punch_state")
        punch_state_display = transaction.get("punch_state_display")
        device_id = transaction.get("terminal_alias") or transaction.get("terminal_sn")
        transaction_id = transaction.get("id")
        device_name = device.name if device else (device_id or "ZKTeco Device")

        if not emp_code or not punch_time:
            frappe.log_error(f"Missing required fields: {transaction}", "ZKTeco Transaction Error")
            return False

        punch_datetime = get_datetime(punch_time) if isinstance(punch_time, str) else punch_time

        # [#6] Primary dedup key: device transaction id (stable, unique per punch).
        if transaction_id and frappe.db.exists("Employee Checkin", {"custom_device_transaction_id": str(transaction_id)}):
            return "skipped"
        # Also skip if already parked in the review queue.
        if transaction_id and frappe.db.exists("ZKTeco Punch Review", {"device_transaction_id": str(transaction_id)}):
            return "skipped"

        # [#3] Unmapped employee -> route to review, do not drop.
        employee = find_employee_by_code(emp_code)
        if not employee:
            _route_to_review(transaction, device_name, reason="Unmapped Employee",
                             employee=None, punch_datetime=punch_datetime)
            if commit:
                frappe.db.commit()
            return "review"

        # Fallback dedup on employee+time (covers rows without a transaction id).
        if frappe.db.exists("Employee Checkin", {"employee": employee, "time": punch_datetime}):
            return "skipped"
        near = frappe.db.get_value("Employee Checkin", {
            "employee": employee,
            "time": ["between", [punch_datetime - timedelta(seconds=5), punch_datetime + timedelta(seconds=5)]],
        }, "name")
        if near:
            return "skipped"

        cfg = frappe.get_cached_doc("ZKTeco Config")

        # [#7] Ambiguous punch -> review queue instead of guessing (if enabled).
        if _is_ambiguous_state(punch_state, punch_state_display) and _review_route_enabled(cfg):
            suggested = _log_type_from_shift(employee, punch_datetime)
            _route_to_review(transaction, device_name, reason="Unknown Punch State",
                             employee=employee, punch_datetime=punch_datetime,
                             suggested=suggested)
            if commit:
                frappe.db.commit()
            return "review"

        log_type = resolve_log_type(punch_state, punch_state_display, employee, punch_datetime)
        shift_type = _get_employee_shift_type(employee)

        checkin = frappe.get_doc({
            "doctype": "Employee Checkin",
            "employee": employee,
            "time": punch_datetime,
            "log_type": log_type,
            "shift": shift_type,
            "device_id": f"{device_id} (ZKTeco-{transaction_id})" if transaction_id else (device_id or "ZKTeco Device"),
            "skip_auto_attendance": 0,
        })
        # [#6] Store the raw device transaction id for stable dedup/audit.
        if frappe.db.has_column("Employee Checkin", "custom_device_transaction_id") and transaction_id:
            checkin.custom_device_transaction_id = str(transaction_id)

        checkin.insert(ignore_permissions=True)
        if commit:
            frappe.db.commit()
        return "created"

    except Exception as e:
        frappe.log_error(f"Error creating Employee Checkin: {str(e)}", "ZKTeco Checkin Creation")
        return False


def _route_to_review(transaction, device_name, reason, employee=None,
                     punch_datetime=None, suggested=None):
    """[#7] Park a punch in ZKTeco Punch Review (Pending)."""
    transaction_id = transaction.get("id")
    if transaction_id and frappe.db.exists("ZKTeco Punch Review", {"device_transaction_id": str(transaction_id)}):
        return
    emp_name = ""
    if employee:
        emp_name = frappe.db.get_value("Employee", employee, "employee_name") or ""
    frappe.get_doc({
        "doctype": "ZKTeco Punch Review",
        "status": "Pending",
        "reason": reason,
        "device_name": device_name,
        "device_transaction_id": str(transaction_id) if transaction_id else None,
        "emp_code": transaction.get("emp_code"),
        "employee": employee,
        "employee_name": emp_name,
        "punch_time": punch_datetime,
        "punch_state": str(transaction.get("punch_state")),
        "punch_state_display": transaction.get("punch_state_display"),
        "suggested_log_type": suggested,
        "raw_json": frappe.as_json(transaction),
    }).insert(ignore_permissions=True)


@frappe.whitelist()
def approve_review(review_name, log_type=None):
    """Turn an approved review row into a real Employee Checkin."""
    rev = frappe.get_doc("ZKTeco Punch Review", review_name)
    if rev.status == "Approved":
        return {"success": False, "message": "Already approved."}

    employee = rev.employee
    if not employee and rev.emp_code:
        employee = find_employee_by_code(rev.emp_code)
    if not employee:
        frappe.throw(_("No Employee resolved for this punch — map the employee first."))

    final_log_type = log_type or rev.resolved_log_type or rev.suggested_log_type
    if not final_log_type:
        final_log_type = _log_type_from_shift(employee, get_datetime(rev.punch_time))

    if frappe.db.exists("Employee Checkin", {"employee": employee, "time": rev.punch_time}):
        rev.status = "Approved"
        rev.resolved_log_type = final_log_type
        rev.save(ignore_permissions=True)
        return {"success": True, "message": "Checkin already existed; marked approved."}

    shift_type = _get_employee_shift_type(employee)
    checkin = frappe.get_doc({
        "doctype": "Employee Checkin",
        "employee": employee,
        "time": rev.punch_time,
        "log_type": final_log_type,
        "shift": shift_type,
        "device_id": f"{rev.device_name} (ZKTeco-{rev.device_transaction_id})" if rev.device_transaction_id else rev.device_name,
        "skip_auto_attendance": 0,
    })
    if frappe.db.has_column("Employee Checkin", "custom_device_transaction_id") and rev.device_transaction_id:
        checkin.custom_device_transaction_id = str(rev.device_transaction_id)
    checkin.insert(ignore_permissions=True)

    rev.status = "Approved"
    rev.employee = employee
    rev.resolved_log_type = final_log_type
    rev.save(ignore_permissions=True)
    frappe.db.commit()
    return {"success": True, "message": f"Checkin created ({final_log_type})."}


# ===========================================================================
# Two-way sync: push employee enrollment to device  [#4]
# ===========================================================================

@frappe.whitelist()
def push_employee_to_device(employee, device_name):
    """
    Push an Employee's enrollment (emp_code + name) to a ZKTeco device.

    NOTE: The exact endpoint/payload depends on your device firmware /
    BioTime-EasyTime API version. This implements the common BioTime v8
    'personnel/api/employees/' shape. Confirm against your server and adjust
    the URL/payload if your API differs (see test_connection raw sample).
    """
    cfg = frappe.get_single("ZKTeco Config")
    device = None
    for d in _get_devices(cfg):
        if d.name == device_name:
            device = d
            break
    if not device:
        frappe.throw(_("Device '{0}' not found.").format(device_name))

    emp = frappe.db.get_value(
        "Employee", employee,
        ["name", "employee_name", "first_name", "attendance_device_id"],
        as_dict=True,
    )
    if not emp:
        frappe.throw(_("Employee '{0}' not found.").format(employee))

    emp_code = emp.attendance_device_id or emp.name
    url = f"http://{device.server_ip}:{device.server_port}/personnel/api/employees/"
    headers = {"Content-Type": "application/json", "Authorization": f"Token {device.token}"}
    payload = {
        "emp_code": emp_code,
        "first_name": emp.first_name or emp.employee_name,
        "last_name": "",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        ok = resp.status_code in (200, 201)
        return {
            "success": ok,
            "status_code": resp.status_code,
            "message": f"[{device.name}] push {'OK' if ok else 'FAILED'} for {emp_code}",
            "response": resp.text[:500],
        }
    except requests.RequestException as e:
        return {"success": False, "message": f"[{device.name}] connection error: {str(e)}"}


# ===========================================================================
# Employee mapping
# ===========================================================================

def find_employee_by_code(emp_code):
    """
    Matches ONLY on Attendance Device ID. Employee ID and User ID
    are intentionally NOT used as fallbacks.
    """
    emp_code = str(emp_code).strip()
    if not frappe.db.has_column("Employee", "attendance_device_id"):
        frappe.log_error(
            "Employee.attendance_device_id column is missing on this site. "
            "Mapping cannot proceed since only Attendance Device ID is used.",
            "ZKTeco Employee Mapping"
        )
        return None
    e = frappe.db.get_value("Employee", {"attendance_device_id": emp_code}, "name")
    if e:
        return e
    return None


# ===========================================================================
# Manual / scheduled / status / health
# ===========================================================================

@frappe.whitelist()
def manual_sync(device_name=None):
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if not cfg.enable_sync:
            return {"success": False, "message": "Sync is disabled."}
        devices = _get_devices(cfg)
        if device_name:
            devices = [d for d in devices if d.name == device_name]
        if not devices:
            return {"success": False, "message": "No usable device configured."}
        overlap = _cfg_int(cfg, "overlap_hours", DEFAULT_OVERLAP_HOURS)
        current_time = now_datetime()
        for d in devices:
            _sync_one_device(cfg, d, current_time, overlap, sync_type="Manual")
        return {"success": True, "message": "Sync completed."}
    except Exception as e:
        frappe.log_error(f"Manual sync failed: {str(e)}", "ZKTeco Manual Sync")
        return {"success": False, "message": f"Sync failed: {str(e)}"}


def scheduled_sync():
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if not cfg.enable_sync:
            return
        sync_seconds = int(cfg.seconds or 300)
        if sync_seconds < 60:
            last_run = frappe.cache().get_value("zkteco_last_sync_run")
            current_time = now_datetime()
            if last_run:
                if (current_time - get_datetime(last_run)).total_seconds() < sync_seconds:
                    return
            frappe.cache().set_value("zkteco_last_sync_run", current_time)
        sync_zkteco_transactions()
    except Exception as e:
        frappe.log_error(f"Scheduled ZKTeco sync failed: {str(e)}", "ZKTeco Scheduled Sync Error")


def sync_health_check():
    """
    Daily guard: if any enabled device hasn't synced within 2x its interval,
    notify System Managers. Turns a silent outage into an alert.
    """
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if not cfg.enable_sync:
            return
        interval = int(cfg.seconds or 300)
        threshold = now_datetime() - timedelta(seconds=max(interval * 2, 3600))

        stale = []
        for device in _get_devices(cfg):
            ls = get_datetime(device.last_sync) if device.last_sync else None
            if ls is None or ls < threshold:
                stale.append(f"{device.name} (last sync: {device.last_sync or 'never'})")

        if stale:
            recipients = _system_manager_emails()
            if recipients:
                frappe.sendmail(
                    recipients=recipients,
                    subject="⚠️ ZKTeco sync appears stale",
                    message=(
                        "The following ZKTeco device(s) have not synced recently:<br><br>"
                        + "<br>".join(stale)
                        + "<br><br>Check the device/server connectivity and the ZKTeco Sync Log."
                    ),
                )
            frappe.log_error("ZKTeco stale devices: " + "; ".join(stale), "ZKTeco Health Check")
    except Exception as e:
        frappe.log_error(f"ZKTeco health check failed: {str(e)}", "ZKTeco Health Check")


def _system_manager_emails():
    users = frappe.get_all(
        "Has Role", filters={"role": "System Manager", "parenttype": "User"},
        fields=["parent"], distinct=True,
    )
    emails = []
    for u in users:
        if u.parent not in ("Administrator", "Guest"):
            email = frappe.db.get_value("User", u.parent, "email")
            if email:
                emails.append(email)
    return emails


def cleanup_scheduler_check():
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if cfg.enable_sync:
            frappe.logger().info("ZKTeco scheduler check: Active")
    except Exception as e:
        frappe.log_error(f"ZKTeco scheduler check failed: {str(e)}", "ZKTeco Scheduler Check")


@frappe.whitelist()
def get_sync_status():
    try:
        cfg = frappe.get_single("ZKTeco Config")
        devices = _get_devices(cfg)
        device_status = []
        for d in devices:
            device_status.append({
                "device": d.name,
                "last_sync": d.last_sync,
                "server": f"{d.server_ip}:{d.server_port}",
                "token_configured": bool(d.token),
            })
        pending_review = frappe.db.count("ZKTeco Punch Review", {"status": "Pending"})
        recent_checkins = frappe.db.count("Employee Checkin", {
            "device_id": ["like", "%ZKTeco%"],
            "creation": [">=", frappe.utils.add_days(today(), -1)],
        })
        return {
            "enabled": cfg.enable_sync,
            "sync_frequency": cfg.seconds,
            "devices": device_status,
            "pending_review": pending_review,
            "recent_checkins_24h": recent_checkins,
        }
    except Exception as e:
        return {"error": str(e)}
