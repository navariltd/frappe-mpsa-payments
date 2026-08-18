let submitting = false;
let statusCheckInterval = null;

function getRequestId() {
	return document.getElementById("request_id")?.value;
}

function showOverlay(text = "Processing...") {
	const el = document.getElementById("overlay");
	if (el) {
		el.style.display = "flex";
		el.querySelector(".mp-overlay-text").innerText = text;
	}
}

function hideOverlay() {
	const el = document.getElementById("overlay");
	if (el) el.style.display = "none";
}

window.addEventListener("pageshow", function (event) {
	if (event.persisted) {
		window.location.reload();
	}
});

async function submit_payment() {
	if (submitting) return;
	submitting = true;
	const id = getRequestId();
	const phone = document.getElementById("phone_number")?.value;
	if (!id) {
		frappe.msgprint("Missing request ID");
		submitting = false;
		return;
	}
	showOverlay("Sending STK Push...");
	try {
		await frappe.call({
			method: "frappe_mpsa_payments.www.mpesa.stkpush.initiate_stk_push",
			args: {
				request_id: id,
				phone_number: phone,
			},
		});
		location.reload(true);
	} catch (e) {
		hideOverlay();
		submitting = false;
	}
}

async function retry_stk() {
	const id = getRequestId();
	if (!id) return;
	showOverlay("Retrying payment...");
	try {
		await frappe.call({
			method: "frappe_mpsa_payments.www.mpesa.stkpush.retry_stkpush",
			args: {
				request_id: id,
				phone_number: document.getElementById("phone_number")?.value,
			},
		});
		location.reload(true);
	} catch (e) {
		hideOverlay();
	}
}

async function checkPaymentStatus() {
	const id = getRequestId();
	if (!id) return;

	try {
		const response = await frappe.call({
			method: "frappe_mpsa_payments.www.mpesa.stkpush.check_payment_status",
			args: { request_id: id },
		});

		const data = response.message;

		if (data.status === "Completed" && data.docstatus === 1) {
			if (statusCheckInterval) {
				clearInterval(statusCheckInterval);
				statusCheckInterval = null;
			}

			const redirectTo = data.redirect_to;

			if (redirectTo) {
				showOverlay("Payment successful! Redirecting...");
				setTimeout(() => {
					window.location.href = redirectTo;
				}, 1500);
			} else {
				location.reload(true);
			}
		} else if (data.status === "Failed") {
			if (statusCheckInterval) {
				clearInterval(statusCheckInterval);
				statusCheckInterval = null;
			}
			location.reload(true);
		}
	} catch (e) {
		console.error("Error checking payment status:", e);
	}
}

document.addEventListener("DOMContentLoaded", function () {
	const id = getRequestId();
	const docstatus = document.getElementById("docstatus")?.value;
	const status = document.getElementById("status")?.value;

	if (
		id &&
		docstatus === "1" &&
		(status === "Pending" || status === "Initiated" || status === "In Progress")
	) {
		if (statusCheckInterval) {
			clearInterval(statusCheckInterval);
		}
		statusCheckInterval = setInterval(checkPaymentStatus, 3000);
	}
});
