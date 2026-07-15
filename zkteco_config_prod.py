# Copyright (c) 2025, osama.ahmed@deliverydevs.com
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _
import requests
from frappe.utils import today, now_datetime, get_datetime
from datetime import datetime, timedelta
import json


# ---------------------------------------------------------------------------
# Tunables (all overridable via ZKTeco Config fields if the fields exist)
# ---------------------------------------------------------------------------

# How far back (hours) each sync re-scans before last_sync, so punches the
# device buffered during an outage and delivered late are re-imported.
DEFAULT_OVERLAP_HOURS = 24

# Page size requested from the device API. BioTime/EasyTime DRF paginates;
# we still follow pagination regardless of this value.
DEFAULT_PAGE_SIZE = 100

# Hard cap on pages fetched in one call, as a runaway guard.
MAX_PAGES = 500

# HTTP timeout (seconds) per page request.
HTTP_TIMEOUT = 60


def _cfg_int(cfg, fieldname, default):
    """Read an int config field if present and truthy, else return default."""
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
# Token / connection
# ===========================================================================

@frappe.whitelist()
def register_api_token():
    """Register DRF API token from ZKTeco server."""
    cfg = frappe.get_single("ZKTeco Config")

    server_ip = cfg.server_ip
    server_port = cfg.server_port
    username = (cfg.username or "").strip()
    password = cfg.get_password("password")

    if not all([server_ip, server_port, username, password]):
        frappe.throw(_("Please configure server IP, port, username, and password."))

    login_url = f"http://{server_ip}:{server_port}/api-token-auth/"

    try:
        response = requests.post(
            login_url,
            json={"username": username, "password": password},
            timeout=15,
        )

        frappe.logger().info(
            f"ZKTeco Login URL: {login_url}\n"
            f"Response: {response.status_code} - {response.text}"
        )

        if response.status_code != 200:
            frappe.throw(_("Login Failed: {0}").format(response.text))

        data = response.json()
        token = data.get("token")

        if not token:
            frappe.throw(_("Token not found: {0}").format(response.text))

        frappe.db.set_single_value("ZKTeco Config", "token", token)
        frappe.db.commit()

        return {"success": True, "message": "Token registered successfully", "token": token}

    except requests.exceptions.RequestException as e:
        frappe.throw(_("Connection error: {0}").format(str(e)))


@frappe.whitelist()
def test_connection():
    """Test connection and preview the latest transactions for today."""
    cfg = frappe.get_single("ZKTeco Config")
    token = (cfg.token or "").strip()
    server_ip = frappe.db.get_single_value("ZKTeco Config", "server_ip")
    server_port = frappe.db.get_single_value("ZKTeco Config", "server_port")

    if not token:
        return {"ok": False, "error": _("Token not set in ZKTeco Config. Please register/save a token first.")}

    day = today()
    start_dt = get_datetime(f"{day} 00:00:00")
    end_dt = get_datetime(f"{day} 23:59:59")

    try:
        transactions = fetch_zkteco_transactions(cfg, start_dt, end_dt)
        transaction_count = len(transactions)

        formatted_transactions = []
        for transaction in transactions[:5]:
            try:
                emp_code = transaction.get('emp_code')
                punch_time = transaction.get('punch_time')
                device_id = transaction.get('terminal_alias') or transaction.get('terminal_sn')
                first_name = transaction.get('first_name', '')
                last_name = transaction.get('last_name', '') or ''
                verify_type_display = transaction.get('verify_type_display')

                zkteco_name = f"{first_name} {last_name}".strip()

                employee_name = zkteco_name
                erpnext_employee = None
                if emp_code:
                    # Match ONLY on Attendance Device ID (same rule as the real sync)
                    employee = None
                    if frappe.db.has_column("Employee", "attendance_device_id"):
                        employee = frappe.db.get_value("Employee", {"attendance_device_id": emp_code}, ["name", "employee_name"])
                    if employee:
                        erpnext_employee = employee[0] if isinstance(employee, tuple) else employee
                        employee_name = f"{employee[1]} (ERPNext)" if isinstance(employee, tuple) else f"{employee} (ERPNext)"

                punch_dt_preview = get_datetime(punch_time) if punch_time else None
                log_type = resolve_log_type(
                    transaction.get('punch_state'),
                    transaction.get('punch_state_display'),
                    erpnext_employee,
                    punch_dt_preview,
                )

                formatted_transactions.append({
                    "id": transaction.get('id'),
                    "employee_code": emp_code,
                    "employee_name": employee_name,
                    "erpnext_employee": erpnext_employee,
                    "punch_time": punch_time,
                    "log_type": log_type,
                    "punch_state_display": transaction.get('punch_state_display'),
                    "device_id": device_id,
                    "verify_method": verify_type_display,
                    "zkteco_name": zkteco_name,
                    "department": transaction.get('department'),
                    "raw_data": transaction,
                })
            except Exception as e:
                frappe.log_error(f"Error processing transaction: {e}", "ZKTeco Transaction Processing")
                continue

        return {
            "ok": True,
            "status_code": 200,
            "url": f"http://{server_ip}:{server_port}/iclock/api/transactions/",
            "total_transactions": transaction_count,
            "transactions_preview": formatted_transactions,
            "raw_sample": transactions[:2] if transactions else [],
            "message": f"Found {transaction_count} transactions for {day}",
        }

    except requests.RequestException as e:
        return {"ok": False, "error": f"Connection error: {str(e)}"}
    except Exception as e:
        return {"ok": False, "error": f"{str(e)}"}


# ===========================================================================
# Fetch (with pagination)
# ===========================================================================

def fetch_zkteco_transactions(cfg, start_time, end_time):
    """
    Fetch ALL transactions from the ZKTeco device for a window, following
    pagination so no records are silently dropped on busy days.

    Handles three response shapes:
      - DRF style: {"count", "next", "previous", "data"/"results": [...]}
      - bare list: [...]
    Follows the "next" URL when present; otherwise falls back to page-number
    pagination until a short/empty page is returned.
    """
    server_ip = cfg.server_ip
    server_port = cfg.server_port
    token = cfg.token
    page_size = _cfg_int(cfg, "page_size", DEFAULT_PAGE_SIZE)

    base_url = f"http://{server_ip}:{server_port}/iclock/api/transactions/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {token}",
    }

    all_rows = []
    next_url = base_url
    params = {
        "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "page_size": page_size,
        "page": 1,
    }

    pages = 0
    use_next_url = False  # once we have a server-provided next link, trust it

    try:
        while next_url and pages < MAX_PAGES:
            pages += 1

            if use_next_url:
                # "next" already carries all query params.
                resp = requests.get(next_url, headers=headers, timeout=HTTP_TIMEOUT)
            else:
                resp = requests.get(next_url, headers=headers, params=params, timeout=HTTP_TIMEOUT)

            resp.raise_for_status()
            data = resp.json()

            # Extract this page's rows.
            if isinstance(data, dict):
                rows = data.get('data')
                if rows is None:
                    rows = data.get('results')
                if rows is None:
                    rows = []
            elif isinstance(data, list):
                rows = data
            else:
                rows = []

            all_rows.extend(rows)

            # Decide how to advance.
            server_next = data.get('next') if isinstance(data, dict) else None

            if server_next:
                next_url = server_next
                use_next_url = True
                continue

            # No server "next". If the API is dict-shaped with a count, or the
            # page came back full, try the next page number; otherwise stop.
            if isinstance(data, dict) and not use_next_url:
                if len(rows) < page_size:
                    break  # last page
                params["page"] += 1
                next_url = base_url
                continue

            # Bare-list response has no pagination -> single page.
            break

        if pages >= MAX_PAGES:
            frappe.log_error(
                f"ZKTeco fetch hit MAX_PAGES ({MAX_PAGES}); results may be truncated. "
                f"Window {params['start_time']}..{params['end_time']}",
                "ZKTeco Pagination Cap",
            )

        return all_rows

    except Exception as e:
        frappe.log_error(
            f"Failed to fetch ZKTeco transactions (page {pages}): {str(e)}",
            "ZKTeco API Error",
        )
        # Return whatever we got before the failure rather than nothing.
        return all_rows


# ===========================================================================
# Sync
# ===========================================================================

def _process_transactions(transactions):
    """
    Insert check-ins for a list of transactions.
    Commits ONCE at the end (not per record). Returns counts dict.
    """
    created = 0
    skipped = 0
    errors = 0
    error_samples = []

    for transaction in (transactions or []):
        try:
            result = create_employee_checkin(transaction, commit=False)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
            else:
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append(str(transaction.get("emp_code")))
        except Exception as e:
            errors += 1
            if len(error_samples) < 5:
                error_samples.append(f"{transaction.get('emp_code')}: {e}")
            frappe.log_error(
                f"Error creating checkin for transaction {transaction}: {str(e)}",
                "ZKTeco Sync Error",
            )

    frappe.db.commit()

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "error_samples": error_samples,
    }


def sync_zkteco_transactions():
    """
    Scheduled/main sync. Resilient window: fetch from (last_sync - overlap)
    to now, so buffered/late punches self-heal. Idempotent via dedup guard.
    Commits once per run. Logs a summary (and flags error runs).
    """
    cfg = frappe.get_single("ZKTeco Config")
    if not cfg.enable_sync:
        return

    if not cfg.token:
        frappe.log_error("ZKTeco token not configured", "ZKTeco Sync")
        return

    try:
        last_sync = frappe.db.get_single_value("ZKTeco Config", "last_sync")
        current_time = now_datetime()

        if not last_sync:
            window_start = current_time - timedelta(days=1)
        else:
            last_sync = get_datetime(last_sync)
            if last_sync.year < 2000:
                window_start = current_time - timedelta(days=1)
            else:
                overlap = _cfg_int(cfg, "overlap_hours", DEFAULT_OVERLAP_HOURS)
                window_start = last_sync - timedelta(hours=overlap)

        transactions = fetch_zkteco_transactions(cfg, window_start, current_time)

        counts = _process_transactions(transactions)

        # Advance cursor after a completed pass (even if 0 rows).
        total_synced = frappe.db.get_single_value("ZKTeco Config", "total_synced_records") or 0
        frappe.db.set_single_value("ZKTeco Config", "last_sync", current_time)
        frappe.db.set_single_value("ZKTeco Config", "total_synced_records", total_synced + counts["created"])
        frappe.db.commit()

        frappe.logger().info(
            f"ZKTeco Sync: fetched={len(transactions or [])} "
            f"created={counts['created']} already-present={counts['skipped']} "
            f"errors={counts['errors']}"
        )

        # Surface error runs so failures are noticed, not discovered late.
        if counts["errors"]:
            frappe.log_error(
                f"ZKTeco sync completed with {counts['errors']} error(s). "
                f"Window {window_start} .. {current_time}. "
                f"Sample emp_codes: {counts['error_samples']}",
                "ZKTeco Sync Errors Summary",
            )

    except Exception as e:
        frappe.log_error(f"ZKTeco sync failed: {str(e)}", "ZKTeco Sync Fatal Error")


@frappe.whitelist()
def backfill_range(from_datetime, to_datetime):
    """
    Re-pull the device's buffered logs for an explicit window (outage recovery).
    Does NOT touch last_sync. Idempotent. Commits once at the end.
    """
    cfg = frappe.get_single("ZKTeco Config")
    if not cfg.token:
        frappe.throw(_("ZKTeco token not configured."))

    start = get_datetime(from_datetime)
    end = get_datetime(to_datetime)

    if start >= end:
        frappe.throw(_("'From' must be earlier than 'To'."))

    transactions = fetch_zkteco_transactions(cfg, start, end)
    counts = _process_transactions(transactions)

    if counts["errors"]:
        frappe.log_error(
            f"ZKTeco backfill {from_datetime}..{to_datetime} had {counts['errors']} error(s). "
            f"Sample: {counts['error_samples']}",
            "ZKTeco Backfill Errors",
        )

    return {
        "success": True,
        "fetched": len(transactions or []),
        "created": counts["created"],
        "already_present": counts["skipped"],
        "errors": counts["errors"],
        "message": f"Backfill {from_datetime} → {to_datetime}: "
                   f"{counts['created']} created, {counts['skipped']} already present, "
                   f"{counts['errors']} errors.",
    }


# ===========================================================================
# Log type resolution (stateless)
# ===========================================================================

def resolve_log_type(punch_state, punch_state_display, employee, punch_datetime):
    """
    Determine IN / OUT for a punch WITHOUT stateful alternation.

    1. Trust unambiguous device signal (0 = Check In, 1 = Check Out).
    2. Otherwise (255 / blank / unknown) derive from the employee's shift
       window: first half = IN, second half = OUT (overnight-aware).
    3. If no shift resolvable, split the day at noon.

    Stateless: each punch is classified independently, so a missing or late
    punch can never shift another punch's IN/OUT.
    """
    display = str(punch_state_display).strip().lower() if punch_state_display else ""
    state = str(punch_state).strip() if punch_state is not None else ""

    if state == "0" or display in ("check in", "in", "checkin"):
        return "IN"
    if state == "1" or display in ("check out", "out", "checkout"):
        return "OUT"

    return _log_type_from_shift(employee, punch_datetime)


def _log_type_from_shift(employee, punch_datetime):
    """Classify by position within the employee's shift window; noon-split fallback."""
    if not employee or not punch_datetime:
        return _noon_split(punch_datetime)

    try:
        shift_type = _get_employee_shift_type(employee)
        if shift_type:
            st = frappe.db.get_value(
                "Shift Type", shift_type, ["start_time", "end_time"], as_dict=True
            )
            if st and st.start_time is not None and st.end_time is not None:
                start_sec = _timedelta_to_seconds(st.start_time)
                end_sec = _timedelta_to_seconds(st.end_time)

                overnight = end_sec <= start_sec
                if overnight:
                    end_sec += 24 * 3600  # unwrap across midnight

                punch_sec = (
                    punch_datetime.hour * 3600
                    + punch_datetime.minute * 60
                    + punch_datetime.second
                )

                # For overnight shifts, a punch in the small hours (before the
                # start time) belongs to the *previous* day's shift -> shift it
                # into the unwrapped range so the midpoint test is correct.
                if overnight and punch_sec < start_sec:
                    punch_sec += 24 * 3600

                midpoint = (start_sec + end_sec) / 2.0
                return "IN" if punch_sec <= midpoint else "OUT"
    except Exception as e:
        frappe.log_error(
            f"Shift-based log_type failed for {employee}: {str(e)}",
            "ZKTeco Log Type",
        )

    return _noon_split(punch_datetime)


def _noon_split(punch_datetime):
    """Last-resort: before noon = IN, noon or later = OUT."""
    try:
        return "IN" if punch_datetime.hour < 12 else "OUT"
    except Exception:
        return "IN"


def _timedelta_to_seconds(value):
    """Shift Type start_time/end_time come back as timedelta (or 'HH:MM:SS')."""
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    parts = str(value).split(":")
    parts = [int(float(p)) for p in parts] + [0, 0, 0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _get_employee_shift_type(employee):
    """Resolve current shift: active Shift Assignment, else Employee default_shift."""
    shift = frappe.db.sql(
        """
        SELECT shift_type
        FROM `tabShift Assignment`
        WHERE employee = %s
          AND docstatus = 1
        ORDER BY creation DESC
        LIMIT 1
        """,
        (employee,),
        as_dict=True,
    )
    if shift and shift[0].shift_type:
        return shift[0].shift_type

    return frappe.db.get_value("Employee", employee, "default_shift")


# ===========================================================================
# Check-in creation
# ===========================================================================

def create_employee_checkin(transaction, commit=True):
    """
    Create an Employee Checkin from a ZKTeco transaction.

    commit: when False, the caller is responsible for committing (batch mode).

    Returns:
      "created"  - a new checkin was inserted
      "skipped"  - a matching checkin already existed (idempotent)
      False      - could not be processed
    """
    try:
        emp_code = transaction.get('emp_code')
        punch_time = transaction.get('punch_time')
        punch_state = transaction.get('punch_state')
        punch_state_display = transaction.get('punch_state_display')
        device_id = transaction.get('terminal_alias') or transaction.get('terminal_sn')
        transaction_id = transaction.get('id')

        if not emp_code or not punch_time:
            frappe.log_error(f"Missing required fields in transaction: {transaction}", "ZKTeco Transaction Error")
            return False

        employee = find_employee_by_code(emp_code)
        if not employee:
            frappe.log_error(f"Employee not found for code: {emp_code}", "ZKTeco Employee Mapping")
            return False

        punch_datetime = get_datetime(punch_time) if isinstance(punch_time, str) else punch_time

        # --- Dedup guard (idempotent) --------------------------------------
        if frappe.db.exists("Employee Checkin", {"employee": employee, "time": punch_datetime}):
            return "skipped"

        near_dup = frappe.db.get_value(
            "Employee Checkin",
            {
                "employee": employee,
                "time": ["between", [
                    punch_datetime - timedelta(seconds=5),
                    punch_datetime + timedelta(seconds=5),
                ]],
            },
            "name",
        )
        if near_dup:
            return "skipped"
        # -------------------------------------------------------------------

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

        checkin.insert(ignore_permissions=True)

        if commit:
            frappe.db.commit()

        return "created"

    except Exception as e:
        frappe.log_error(f"Error creating Employee Checkin: {str(e)}", "ZKTeco Checkin Creation")
        return False


def find_employee_by_code(emp_code):
    """
    Find Employee using ZKTeco/EasyTime employee code.
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

    employee = frappe.db.get_value("Employee", {"attendance_device_id": emp_code}, "name")
    if employee:
        return employee

    frappe.logger().info(f"Employee mapping not found for emp_code (attendance_device_id): {emp_code}")
    return None


# ===========================================================================
# Manual / scheduled / status
# ===========================================================================

@frappe.whitelist()
def manual_sync():
    """Manual sync trigger (same resilient window as scheduled)."""
    try:
        sync_zkteco_transactions()
        return {"success": True, "message": "Sync completed successfully"}
    except Exception as e:
        frappe.log_error(f"Manual sync failed: {str(e)}", "ZKTeco Manual Sync")
        return {"success": False, "message": f"Sync failed: {str(e)}"}


def scheduled_sync():
    """Scheduled sync respecting the frequency setting."""
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if not cfg.enable_sync:
            return

        sync_seconds = int(cfg.seconds or 300)
        if sync_seconds < 60:
            last_run = frappe.cache().get_value("zkteco_last_sync_run")
            current_time = now_datetime()
            if last_run:
                time_diff = (current_time - get_datetime(last_run)).total_seconds()
                if time_diff < sync_seconds:
                    return
            frappe.cache().set_value("zkteco_last_sync_run", current_time)

        sync_zkteco_transactions()

    except Exception as e:
        frappe.log_error(f"Scheduled ZKTeco sync failed: {str(e)}", "ZKTeco Scheduled Sync Error")


def cleanup_scheduler_check():
    """Ensure scheduler is active (heartbeat log)."""
    try:
        cfg = frappe.get_single("ZKTeco Config")
        if cfg.enable_sync:
            frappe.logger().info("ZKTeco scheduler check: Active")
    except Exception as e:
        frappe.log_error(f"ZKTeco scheduler check failed: {str(e)}", "ZKTeco Scheduler Check")


@frappe.whitelist()
def get_sync_status():
    """Current sync status and statistics."""
    try:
        cfg = frappe.get_single("ZKTeco Config")
        last_sync = frappe.db.get_single_value("ZKTeco Config", "last_sync")

        recent_checkins = frappe.db.count("Employee Checkin", {
            "device_id": ["like", "%ZKTeco%"],
            "creation": [">=", frappe.utils.add_days(today(), -1)],
        })

        return {
            "enabled": cfg.enable_sync,
            "sync_frequency": cfg.seconds,
            "last_sync": last_sync,
            "recent_checkins_24h": recent_checkins,
            "server_configured": bool(cfg.server_ip and cfg.server_port),
            "token_configured": bool(cfg.token),
        }

    except Exception as e:
        return {"error": str(e)}
