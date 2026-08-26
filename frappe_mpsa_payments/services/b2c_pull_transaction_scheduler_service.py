import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

HOURLY_PULL_JOB_ID = "mpesa_hourly_pull_transactions"
HOURLY_PULL_WINDOW_HOURS = -2


def run_hourly_pull_transactions():
    """Pull every enabled shortcode in one job.

    This used to enqueue one job per Mpesa Settings doc. With dozens of
    shortcodes that meant dozens of jobs an hour, each writing its own Error
    Log when Safaricom answered 1001 - hundreds of entries a day, almost all just
    "this branch took no payments in the last two hours". One job means one
    summary entry per run.
    """
    settings_list = frappe.get_all(
        "Mpesa Settings",
        filters={"enable_hourly_pull_transactions": 1},
        fields=["name"],
    )
    if not settings_list:
        return

    # A slow run must not stack on top of itself - dozens of shortcodes with
    # retries can outlast the hour.
    if is_job_enqueued(HOURLY_PULL_JOB_ID):
        frappe.logger().info("Hourly Mpesa pull still running, skipping this cycle")
        return

    end_date = now_datetime()
    start_date = add_to_date(end_date, hours=HOURLY_PULL_WINDOW_HOURS)

    try:
        frappe.enqueue(
            "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api"
            ".execute_bulk_pull_transactions",
            queue="long",
            timeout=3600,
            job_id=HOURLY_PULL_JOB_ID,
            settings_names=[row.name for row in settings_list],
            start_date=start_date.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end_date.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "[Hourly Pull Transaction Error] could not queue bulk pull",
        )
