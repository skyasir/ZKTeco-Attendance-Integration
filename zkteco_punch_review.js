// Copyright (c) 2026, Smurik Solutions
// Client Script for ZKTeco Punch Review

frappe.ui.form.on("ZKTeco Punch Review", {
    refresh(frm) {
        if (frm.doc.status === "Pending") {
            frm.add_custom_button(__("Approve → Create Checkin"), function () {
                const d = new frappe.ui.Dialog({
                    title: __("Approve Punch"),
                    fields: [
                        {
                            fieldtype: "Link",
                            fieldname: "employee",
                            label: __("Employee"),
                            options: "Employee",
                            default: frm.doc.employee,
                            reqd: 1,
                            description: frm.doc.employee ? "" : __("This punch is unmapped — pick the employee.")
                        },
                        {
                            fieldtype: "Select",
                            fieldname: "log_type",
                            label: __("Log Type"),
                            options: "\nIN\nOUT",
                            default: frm.doc.resolved_log_type || frm.doc.suggested_log_type || ""
                        }
                    ],
                    primary_action_label: __("Create Checkin"),
                    primary_action(values) {
                        // Persist chosen employee/log_type first, then approve.
                        frm.set_value("employee", values.employee);
                        if (values.log_type) frm.set_value("resolved_log_type", values.log_type);
                        frm.save().then(() => {
                            frappe.call({
                                method: "zkteco_checkins_sync.zkteco_checkin_sync.doctype.zkteco_config.zkteco_config.approve_review",
                                args: { review_name: frm.doc.name, log_type: values.log_type || null },
                                freeze: true,
                                freeze_message: __("Creating checkin...")
                            }).then((r) => {
                                const m = r.message || {};
                                frappe.show_alert({
                                    message: m.message || __("Done"),
                                    indicator: m.success ? "green" : "red"
                                });
                                d.hide();
                                frm.reload_doc();
                            });
                        });
                    }
                });
                d.show();
            }).addClass("btn-primary");

            frm.add_custom_button(__("Reject"), function () {
                frm.set_value("status", "Rejected");
                frm.save();
            });
        }
    }
});
