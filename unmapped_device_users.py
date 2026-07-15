# Copyright (c) 2026, Smurik Solutions
# Unmapped Device Users — device emp_codes seen punching but not mapped to any Employee.

import frappe


def execute(filters=None):
    filters = filters or {}

    columns = [
        {"label": "Device Emp Code", "fieldname": "emp_code", "fieldtype": "Data", "width": 140},
        {"label": "Punches", "fieldname": "punches", "fieldtype": "Int", "width": 90},
        {"label": "First Seen", "fieldname": "first_seen", "fieldtype": "Datetime", "width": 170},
        {"label": "Last Seen", "fieldname": "last_seen", "fieldtype": "Datetime", "width": 170},
        {"label": "Device(s)", "fieldname": "devices", "fieldtype": "Data", "width": 180},
        {"label": "Sample Name (from device)", "fieldname": "sample_name", "fieldtype": "Data", "width": 200},
    ]

    conditions = "status = 'Pending' AND reason = 'Unmapped Employee'"
    values = {}

    if filters.get("from_date"):
        conditions += " AND punch_time >= %(from_date)s"
        values["from_date"] = filters["from_date"]
    if filters.get("to_date"):
        conditions += " AND punch_time <= %(to_date)s"
        values["to_date"] = filters["to_date"]

    rows = frappe.db.sql(
        f"""
        SELECT
            emp_code,
            COUNT(*) AS punches,
            MIN(punch_time) AS first_seen,
            MAX(punch_time) AS last_seen,
            GROUP_CONCAT(DISTINCT device_name) AS devices
        FROM `tabZKTeco Punch Review`
        WHERE {conditions}
        GROUP BY emp_code
        ORDER BY punches DESC
        """,
        values,
        as_dict=True,
    )

    # Attach a sample device-supplied name from the raw json, if present.
    for r in rows:
        r["sample_name"] = _sample_name(r["emp_code"])

    return columns, rows


def _sample_name(emp_code):
    row = frappe.db.get_value(
        "ZKTeco Punch Review",
        {"emp_code": emp_code, "reason": "Unmapped Employee"},
        "raw_json",
    )
    if not row:
        return ""
    try:
        data = frappe.parse_json(row)
        first = data.get("first_name", "") or ""
        last = data.get("last_name", "") or ""
        return f"{first} {last}".strip()
    except Exception:
        return ""
