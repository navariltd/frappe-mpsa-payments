# Frappe Mpesa Payments

<img width="1408" height="768" alt="Frappe+Daraja" src="https://github.com/user-attachments/assets/805e320d-16df-4b16-8663-cf42ee00c20e" />


A custom Frappe application that integrates with [Safaricom's Daraja API](https://developer.safaricom.co.ke/) 
to extend [ERPNext](https://frappe.io/erpnext) with M-Pesa payment capabilities. It supports:

- **Mpesa Express (STK Push)** — Trigger payment prompts on a customer's phone from Sales Invoices, Orders, and POS
- **C2B (Customer to Business)** — Receive and reconcile incoming M-Pesa payments in real time
- **B2C (Business to Customer)** — Disburse salaries, supplier payments, and loans to M-Pesa accounts
- **Transaction Status** — Query the status of any M-Pesa transaction by receipt number

## Installation

**Frappe Cloud:**
Search for **Frappe Mpesa Payments** in the [Marketplace](https://frappecloud.com/marketplace/search), 
or add from GitHub: https://github.com/navariltd/frappe-mpsa-payments.git

**Self-hosted:**
```bash
bench get-app https://github.com/navariltd/frappe-mpsa-payments.git
bench --site [your-site-name] install-app frappe_mpsa_payments
bench restart
```

## Dependencies

- [Frappe Framework](https://frappe.io/framework)
- [ERPNext](https://frappe.io/erpnext)
- [Frappe HR](https://frappe.io/hr)
- [Frappe Lending](https://frappe.io/lending)
- Valid [Daraja API credentials](https://developer.safaricom.co.ke/)
- A publicly accessible HTTPS domain for webhook callbacks

## Documentation

Full installation, configuration, and usage documentation is available at:

**[docs.navari.co.ke/frappe-mpesa-payments](https://docs.navari.co.ke/frappe-mpesa-payments/introduction/home)**

## License

MIT
