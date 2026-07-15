# ZKTeco Checkin Sync — Multi-Device Edition (v2)

This upgrade adds seven functional capabilities on top of the hardened sync:

1. **Multi-device support** — sync from any number of biometric devices.
2. **Per-device sync cursors** — each device heals independently.
3. **Unmapped user detection** — punches with no matching Employee are queued, not dropped.
4. **Two-way sync** — push employee enrollment from ERPNext to a device.
5. **Sync log / audit** — one `ZKTeco Sync Log` row per device per run.
6. **Raw transaction retention** — device transaction id stored on each checkin and used as the dedup key.
7. **Manual review queue** — ambiguous / off-hours / unmapped punches go to `ZKTeco Punch Review`.

Backwards compatible: if you don't configure the new device table, the app keeps
using the existing single-device fields, so nothing breaks before you migrate.

---

## 1. Files

| File | Destination |
|---|---|
| `zkteco_config.py` | `zkteco_checkin_sync/doctype/zkteco_config/zkteco_config.py` (replace) |
| `doctype/zkteco_device.json` | new DocType **ZKTeco Device** (child table) |
| `doctype/zkteco_sync_log.json` | new DocType **ZKTeco Sync Log** |
| `doctype/zkteco_punch_review.json` | new DocType **ZKTeco Punch Review** |
| `zkteco_punch_review.js` | client script for the review doctype |
| `report/unmapped_device_users.json` + `.py` | new Script Report **Unmapped Device Users** |

Place each new doctype under
`zkteco_checkin_sync/zkteco_checkin_sync/doctype/<snake_name>/` with matching
`__init__.py` and a controller `.py` (an empty `class <Name>(Document): pass`
is fine for Device/Sync Log/Punch Review, though the JS approve flow lives in
the config controller).

---

## 2. Custom fields to add

### On **ZKTeco Config** (add via fixtures or Customize Form)
- `devices` — Table, Options: **ZKTeco Device**, label "Devices"
- `overlap_hours` — Int, default `24`
- `page_size` — Int, default `100`
- `route_ambiguous_to_review` — Check, default `1`
  (uncheck to auto-guess ambiguous punches by shift timing instead of queuing them)

Keep the existing single-device fields (`server_ip`, `server_port`, `token`,
`username`, `password`, `last_sync`, `total_synced_records`) — they're the
legacy fallback and the migration source.

### On **Employee Checkin** (custom field — enables #6)
- `custom_device_transaction_id` — Data, label "Device Transaction ID",
  **Unique** (recommended), read-only. Add an index for fast dedup lookups.

The code guards every use with `frappe.db.has_column(...)`, so if you skip this
field the app still works — it just falls back to timestamp-based dedup.

---

## 3. Scheduler

The dynamic scheduler in `hooks.py` already calls `scheduled_sync` /
`sync_zkteco_transactions`. Add two more events:

```python
scheduler_events = {
    "daily": [
        "zkteco_checkins_sync.zkteco_checkin_sync.doctype.zkteco_config.zkteco_config.sync_health_check"
    ],
    # ... existing cron/hourly entries ...
}
```

`sync_health_check` emails System Managers if any device hasn't synced within
2× its interval — this is what would have surfaced the outage automatically.

---

## 4. Deploy

```bash
# copy files into the app, then:
bench --site <site> migrate      # installs the new doctypes
bench --site <site> build
bench --site <site> clear-cache
bench restart
```

---

## 5. Migrate from single-device to multi-device

1. Open **ZKTeco Config**.
2. In the **Devices** table, add one row for your existing machine:
   device name, server IP, port, username, password.
3. Click **Register API token** for that row (or paste the existing token).
4. Enable the row. Leave the legacy top-level fields as-is (harmless).
5. Once the device row syncs successfully, the child cursor takes over.

To add a second machine later, just add another row and register its token.

---

## 6. Feature notes

- **#4 push_employee_to_device** targets the BioTime v8
  `personnel/api/employees/` endpoint. Confirm the URL/payload against your
  firmware — run **Test Connection** and check the `raw_sample` to see the
  exact field names your server uses, then adjust `push_employee_to_device`
  if needed. Treated as best-effort until confirmed on your device.

- **#7 review queue** default-routes only genuinely ambiguous punches
  (state 255 / unknown). Set `route_ambiguous_to_review = 0` to fall back to
  automatic shift-timing classification with no queue.

- **Unmapped Device Users report** aggregates the pending `Unmapped Employee`
  review rows by emp_code so HR can see who's punching but not yet in the
  Employee master, then map + approve.

---

## 7. Operational routine (recommended)

- Daily: skim **ZKTeco Sync Log** (filter Status ≠ Success) and clear the
  **ZKTeco Punch Review** pending queue before payroll runs.
- Monthly: check **Unmapped Device Users** before processing salary so no
  new joiner's attendance is silently missing.
