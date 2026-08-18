from typing import Callable, Literal, Optional, Union

from ..connectors.connectors import MpesaConnector


def process_request(
    endpoint: str,
    settings_name: str,
    method: Literal["GET", "POST", "PATCH", "PUT"] = "POST",
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    success_callback: Optional[Callable] = None,
    error_callback: Optional[Callable] = None,
    request_description: Optional[str] = "Mpesa API Request",
    doctype: Optional[str] = None,
    document_name: Optional[str] = None,
    retrying: bool = False,
    reuse_existing_request: bool = False,
) -> Optional[Union[dict, str, bytes]]:
    """
    Initializes an MpesaConnector and makes a remote call with the provided parameters.

    Args:
        endpoint (str): The API endpoint.
        settings_name (str): The name of the Mpesa Settings document.
        method (str): HTTP method, default is "POST".
        payload (dict, optional): Request payload.
        headers (dict, optional): Request headers.
        success_callback (Callable, optional): Function called on success.
        error_callback (Callable, optional): Function called on error.
        request_description (str, optional): Description of the request for logging.
        doctype (str, optional): Reference DocType for logging.
        document_name (str, optional): Reference document name for logging.
        retrying (bool, optional): Indicates if this is a retry attempt.

    Returns:
        dict | str | bytes | None: Response data or None on failure.
    """
    # Initialize the MpesaConnector with the settings name
    builder = (
        MpesaConnector(settings_name=settings_name)
        .set_endpoint(endpoint)  # Set the API endpoint
        .set_method(method)  # Set the HTTP method
        .set_payload(payload or {})  # Set the payload (default to empty dict)
        .set_headers(headers or {})  # Set headers (default to JSON content type)
        .describe(request_description)  # Set the request description
        .reuse_existing_request(reuse_existing_request)
    )

    # Set success and error callbacks if provided
    if success_callback:
        builder.on_success(success_callback)
    if error_callback:
        builder.on_error(error_callback)

    # Make the remote API call
    return builder.make_remote_call(
        doctype=doctype, document_name=document_name, retrying=retrying
    )
