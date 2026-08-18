import base64
import os

import frappe
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding


def load_certificate_path(cert_url: str, sandbox: bool) -> str:
    """
    Try loading certificate from File doctype.
    If missing, fall back to local /public_certs folder.
    Returns the absolute file path of the certificate.
    """
    try:
        if cert_url and frappe.db.exists("File", {"file_url": cert_url}):
            file_doc = frappe.get_doc("File", {"file_url": cert_url})
            return file_doc.get_full_path()
    except Exception:
        pass

    base_dir = os.path.dirname(__file__)
    filename = "SandboxCertificate.cer" if sandbox else "ProductionCertificate.cer"
    local_path = os.path.join(base_dir, "public_certs", filename)

    if os.path.exists(local_path):
        return local_path

    raise FileNotFoundError(f"M-Pesa certificate not found: {cert_url}")


def generate_security_credential(
    initiator_password: str, certificate_path: str, sandbox: bool
) -> str:
    """
    Encrypts the initiator password using the uploaded M-Pesa public key certificate
    following M-Pesa's security credential generation requirements.
    """
    try:
        full_path = load_certificate_path(certificate_path, sandbox)

        with open(full_path, "rb") as cert_file:
            # Load the X.509 certificate (supports both PEM & DER formats)
            cert_data = cert_file.read()

            if b"BEGIN CERTIFICATE" in cert_data:
                cert = x509.load_pem_x509_certificate(cert_data)
            else:
                cert = x509.load_der_x509_certificate(cert_data)

            # Extract the RSA public key from the certificate
            public_key = cert.public_key()

        # Encrypt the Base64-encoded password using RSA (PKCS#1 v1.5)
        encrypted_data = public_key.encrypt(
            initiator_password.encode("utf-8"),
            padding.PKCS1v15(),  # Use PKCS#1.5 padding (not OAEP)
        )

        # Convert encrypted bytes to a Base64 string
        security_credential = base64.b64encode(encrypted_data).decode("utf-8")

        return security_credential

    except Exception as e:
        frappe.log_error(title="Security Credential Generation Error", message=str(e))
        raise frappe.ValidationError(f"Error generating security credential: {str(e)}")
