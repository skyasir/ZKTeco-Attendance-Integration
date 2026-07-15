# ZKTeco Checkin Sync - Installation & Setup Guide

## Overview
This Frappe/ERPNext app automatically synchronizes employee check-in/check-out transactions from ZKTeco biometric devices with ERPNext Employee Checkin records.

## Features
- ✅ **Real-time Sync**: Configurable sync frequency (10 seconds to 1 hour)
- ✅ **Smart Mapping**: Automatically maps ZKTeco employee codes to ERPNext employees
- ✅ **Duplicate Prevention**: Prevents duplicate check-in records
- ✅ **Test Connection**: Preview transactions before enabling sync
- ✅ **Dynamic Scheduler**: Adjusts cron jobs based on sync frequency
- ✅ **Comprehensive Logging**: Detailed error tracking and sync statistics

## Prerequisites

### ZKTeco Device Requirements
- ZKBio Time software installed and running
- Device connected to network
- API access enabled
- Superuser credentials available

### ERPNext Requirements
- ERPNext v13+ or Frappe v13+
- HR module enabled
- Employee records with matching employee codes

## Installation

### 1. Install the App
```bash
# Navigate to your bench directory
cd /path/to/your/bench

# Get the app
bench get-app https://github.com/your-username/zkteco_checkins_sync

# Install on your site
bench --site your-site.com install-app zkteco_checkins_sync

# Migrate the database
bench --site your-site.com migrate
```

### 2. Setup ZKTeco Device

#### Configure ZKBio Time
1. Open ZKBio Time software
2. Go to **System Settings** → **Communication**
3. Note down the **Server IP** and **Port** (usually port 80)
4. Ensure API access is enabled
5. Create or note superuser credentials

#### Test Device Connectivity
```bash
# Test if device is accessible
curl -X GET "http://[DEVICE_IP]:[PORT]/iclock/api/transactions/"
```

## Configuration

### 1. Access ZKTeco Config
1. Login to ERPNext
2. Go to: **Setup** → **ZKTeco Checkin Sync** → **ZKTeco Config**
3. Enable sync: Check **"Enable Sync"**

### 2. Configure Connection Settings

#### ZKTeco Superuser Credentials
- **Username**: Your ZKBio Time superuser username
- **Password**: Your ZKBio Time superuser password

#### ZKTeco API Credentials
- **Server IP**: IP address of the ZKBio Time server (e.g., 192.168.1.100)
- **Server Port**: Port number (usually 80)

### 3. Register API Token
1. Fill in Server IP and Port
2. Click **"Register API token"**
3. Token will be automatically generated and saved

### 4. Test Connection
1. Click **"Test Connection"** button
2. Review the transaction preview
3. Verify employee mappings

### 5. Configure Sync Settings
- **Sync Frequency**: Choose from 10 seconds to 1 hour
  - **10-30 seconds**: For real-time monitoring
  - **5-15 minutes**: Recommended for most setups
  - **30+ minutes**: For less frequent updates

## Employee Mapping

### Automatic Mapping
The system maps ZKTeco employees to ERPNext using **only one field**:

- **Attendance Device ID** field in ERPNext must match `emp_code` from ZKTeco.
  - This is a **standard core field** on the ERPNext Employee doctype (under "Attendance and Leave Details"). You do **not** need to create it as a Custom Field — it already exists on any standard ERPNext install with the HR module enabled.

Employee ID and User ID are **not** used for mapping, intentionally, and are not checked as fallbacks. If an incoming `emp_code` doesn't match any Employee's Attendance Device ID, the punch is not mapped (it's logged, and — in the multi-device build — routed to the Punch Review / Unmapped Device Users report).

### Setup Employee Mapping

```
ERPNext Employee → Attendance Device ID = ZKTeco emp_code
Example: Attendance Device ID = "001" matches ZKTeco emp_code = "001"
```

Every active employee who punches on a ZKTeco device **must** have Attendance Device ID populated with the exact value the device reports as `emp_code`, or their punches will not be mapped to an Employee Checkin.

> ⚠️ Before enabling sync, audit your Employee list to confirm Attendance Device ID is filled in for every employee who uses the biometric device. An employee with a blank or mismatched Attendance Device ID will be silently unmapped under this configuration.

The "Test Connection" preview (`test_connection()`) has been updated to match this same rule — it now checks Attendance Device ID only, so "Employee mappings (Found/Not Found)" in the preview accurately reflects what the real sync will do.

## Testing & Verification

### 1. Test Connection
```javascript
// The test will show:
✅ Connection status
📊 Number of transactions found today
👥 Employee mappings (Found/Not Found)
🔍 Sample transaction data
```

### 2. Manual Sync
1. Go to ZKTeco Config
2. Click **Actions** → **Manual Sync**
3. Check Employee Checkin list for new records

### 3. Monitor Sync Status
1. Click **Actions** → **Sync Status**
2. Review:
   - Last sync time
   - Recent check-ins count
   - Configuration status

## Transaction Data Mapping

### ZKTeco API Response Format
```json
{
  "count": 8,
  "data": [
    {
      "id": 2,
      "emp_code": "001",
      "first_name": "John",
      "last_name": "Doe",
      "punch_time": "2025-08-15 17:54:17",
      "punch_state": "0",  // 0 = Check In, 1 = Check Out
      "punch_state_display": "Check In",
      "terminal_alias": "Main_Entrance"
    }
  ]
}
```

### ERPNext Employee Checkin Mapping
```python
ZKTeco Field           → ERPNext Field
emp_code              → employee (via mapping)
punch_time            → time
punch_state ("0"/"1") → log_type ("IN"/"OUT")
terminal_alias        → device_id
id                    → device_id (appended for uniqueness)
```

## Troubleshooting

### Common Issues

#### 1. Connection Failed
```
❌ Symptoms: Test connection fails
🔧 Solutions:
- Verify Server IP and Port
- Check network connectivity
- Ensure ZKBio Time is running
- Verify firewall settings
```

#### 2. Token Registration Failed
```
❌ Symptoms: Cannot register API token
🔧 Solutions:
- Check username/password
- Ensure superuser privileges
- Verify API endpoint accessibility
```

#### 3. Employees Not Found
```
❌ Symptoms: Transactions sync but employees not mapped
🔧 Solutions:
- Check Employee ID/User ID mapping
- Verify emp_code format consistency
- Review Employee master data
```

#### 4. Duplicate Check-ins
```
❌ Symptoms: Multiple records for same punch
🔧 Solutions:
- System automatically prevents duplicates
- Check device_id uniqueness
- Review time tolerance settings
```

### Error Logs
Monitor error logs in:
- **Error Log** doctype in ERPNext
- System logs with title "ZKTeco"

### Debug Mode
```python
# Enable detailed logging
frappe.log_error("Debug message", "ZKTeco Debug")
```

## Advanced Configuration

### Custom Scheduler Frequency
```python
# In hooks.py, modify get_scheduler_events()
# Add custom frequency mappings
```

### Custom Employee Mapping
```python
# Override find_employee_by_code() function
# Add custom mapping logic
```

### Field Customization
```python
# Modify create_employee_checkin() function
# Map additional ZKTeco fields to custom fields
```

## Performance Optimization

### Recommended Settings
- **Small Office (< 50 employees)**: 30-60 seconds
- **Medium Office (50-200 employees)**: 2-5 minutes  
- **Large Office (200+ employees)**: 5-15 minutes

### Database Optimization
```sql
-- Add indexes for better performance
ALTER TABLE `tabEmployee Checkin` 
ADD INDEX `idx_employee_time` (`employee`, `time`);

ALTER TABLE `tabEmployee Checkin` 
ADD INDEX `idx_device_time` (`device_id`, `time`);
```

## Security Considerations

### API Token Security
- Tokens are stored encrypted in ERPNext
- Regular token rotation recommended
- Limit API access to specific IPs if possible

### Network Security
- Use VPN for remote ZKTeco device access
- Implement firewall rules
- Regular security updates

## Maintenance

### Regular Tasks
1. **Monitor sync status** - Weekly
2. **Review error logs** - Daily
3. **Verify employee mappings** - When adding new employees
4. **Test connection** - After any network changes

### Backup Considerations
- Employee Checkin data is included in ERPNext backups
- ZKTeco Config settings are preserved
- API tokens need re-registration after restore

## Support & Troubleshooting

### Log Files to Check
1. **Error Log** doctype in ERPNext
2. **ZKTeco Sync** error logs
3. **ZKTeco Scheduler** logs

### Performance Monitoring
```python
# Check sync statistics
frappe.call('zkteco_checkins_sync.zkteco_checkin_sync.doctype.zkteco_config.zkteco_config.get_sync_status')
```

### Contact Support
For technical support or feature requests:
- Email: osama.ahmed@deliverydevs.com

## Changelog

### Version 0.0.1
- Initial release
- Basic sync functionality
- Dynamic scheduler
- Employee mapping
- Error handling and logging
