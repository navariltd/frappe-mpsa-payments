from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    custom_fields = {
        "Sales Invoice": [
            {
                "fieldname": "mpesa_payments",
                "label": "Fetch Mpesa Payments",
                "fieldtype": "Button",
                "insert_after": "payments_section",
            }
        ]
    }

    create_custom_fields(custom_fields)
