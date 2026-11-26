"""
poxp_protocol.py
Post Office Xchange Protocol (POXP) Serializer and Parser.
Handles message framing, headers, timestamps, status codes, and 10MB attachments.
"""

import base64
from datetime import datetime, timezone, timedelta

# Protocol name/version carried on the first line of EVERY POXP message
# (requests, relays and acknowledgements alike).
POXP_VERSION = "POXP/1.0"

# Standard & Custom Status Codes
STATUS_OK = (200, "OK")
STATUS_REGISTERED = (201, "REGISTERED")
STATUS_BAD_REQUEST = (400, "BAD_REQUEST")
STATUS_CLIENT_NOT_FOUND = (404, "CLIENT_NOT_FOUND")
STATUS_CLIENT_UNAVAILABLE = (480, "CLIENT_UNAVAILABLE")
STATUS_SERVER_ERROR = (500, "SERVER_ERROR")
STATUS_RELAY_ERROR = (502, "RELAY_ERROR")
STATUS_SERVER_UNAVAILABLE = (503, "SERVER_UNAVAILABLE")

# Priority header -> sort weight (lower = delivered first). Used only to order
# messages when a mailbox is fetched; it is metadata, not a scheduling queue.
PRIORITY_WEIGHT = {"important": 1, "moderate": 2, "low": 3}

# Socket timeouts in seconds. Each one is an idle timeout per socket operation
# (connect / one send / one recv), not a deadline for the whole exchange. A 10 MB
# attachment travels as ~13.5 MB of base64, which needs this headroom on slower
# LAN / Wi-Fi links. These do NOT change the protocol's 10-second windows
# (recipient-offline hold and destination-server relay retry) in server_node.py.
REGISTER_TIMEOUT = 20.0      # client -> own server, REGISTER
FETCH_TIMEOUT = 20.0         # client -> own server, FETCH (inbox may carry attachments)
SUBMIT_TIMEOUT = 180.0       # client -> own server, SUBMIT (waits while the server
                             # relays and holds the mail up to 10s for pickup)
SERVER_CONN_TIMEOUT = 180.0  # server <- any incoming connection
RELAY_SOCKET_TIMEOUT = 120.0 # server -> remote server, RELAY

# A registered client counts as "connected here" only while it keeps polling
# (clients FETCH every 5s). After this long without a REGISTER/FETCH, the server
# stops assuming the recipient is local and relays to the other server instead.
CLIENT_ACTIVE_WINDOW = 15.0

ATTACHMENT_START_TAG = "---POXP_ATTACHMENT_BEGIN---"
ATTACHMENT_END_TAG = "---POXP_ATTACHMENT_END---"

def get_iso_timestamp():
    """Returns ISO-8601 timestamp with +05:00 timezone offset."""
    tz = timezone(timedelta(hours=5))
    return datetime.now(tz).isoformat()

def build_poxp_message(method, headers, body="", attachment_filename=None, attachment_bytes=None):
    """
    Serializes a POXP request message.
    Encodes optional binary attachment (up to 10MB) in base64 inside envelope.
    """
    out_headers = dict(headers)
    final_body = body if body else ""

    if attachment_bytes and attachment_filename:
        out_headers["Content-Type"] = "multipart/mixed"
        out_headers["Attachment-Name"] = attachment_filename
        out_headers["Attachment-Length"] = str(len(attachment_bytes))
        b64_payload = base64.b64encode(attachment_bytes).decode("ascii")
        final_body = f"{final_body}\n\n{ATTACHMENT_START_TAG}\n{b64_payload}\n{ATTACHMENT_END_TAG}"

    out_headers["Length"] = str(len(final_body))

    lines = [f"{method} {POXP_VERSION}"]
    for k, v in out_headers.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append(final_body)
    return "\n".join(lines) + "\n"

def build_ack(code, short_name, identifier, error_at="None", body=""):
    """
    Serializes a POXP acknowledgement message.
    Status line carries the protocol/version token (e.g. "200 OK POXP/1.0"),
    matching the request-side lines ("SUBMIT POXP/1.0", etc.) so every POXP message --
    request or acknowledgement -- is self-identifying on the wire.
    """
    lines = [
        f"{code} {short_name} {POXP_VERSION}",
        f"Identifier: {identifier}",
        f"Time of Acknowledgement: {get_iso_timestamp()}",
        f"Error at: {error_at}",
        "",
        body if body else ""
    ]
    return "\n".join(lines) + "\n"

def parse_poxp_message(raw_text):
    """
    Parses a raw POXP string into a structured dictionary.
    Separates attachment payload from plain text body if multipart request.
    """
    if not raw_text or not raw_text.strip():
        return None
    
    parts = raw_text.split("\n\n", 1)
    header_block = parts[0]
    raw_body = parts[1] if len(parts) > 1 else ""
    
    lines = header_block.splitlines()
    if not lines:
        return None
    
    first_line = lines[0].strip()
    first_tokens = first_line.split(" ", 1)
    
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip()] = val.strip()
            
    is_ack = first_tokens[0].isdigit()

    attachment_b64 = None
    clean_body = raw_body

    # Only parse attachment envelope for non-ACK request messages (SUBMIT / RELAY)
    if not is_ack and ATTACHMENT_START_TAG in raw_body and ATTACHMENT_END_TAG in raw_body:
        pre_att, post_start = raw_body.split(ATTACHMENT_START_TAG, 1)
        b64_str, _ = post_start.split(ATTACHMENT_END_TAG, 1)
        clean_body = pre_att.strip()
        attachment_b64 = b64_str.strip()

    return {
        "type": "ACK" if is_ack else "REQUEST",
        "method_or_code": first_tokens[0],
        "protocol_or_status": first_tokens[1] if len(first_tokens) > 1 else "",
        "headers": headers,
        "body": clean_body.strip(),
        "raw_body": raw_body.strip(),
        "attachment_b64": attachment_b64
    }