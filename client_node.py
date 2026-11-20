"""
client_node.py
Raw TCP Socket Client Engine for POXP.
Features:
- Physical LAN IP auto-detection
- Automatic 5-second mailbox fetch daemon
- Client persistent storage with unread/read state
- 10MB binary attachment support
- Live console activity logging buffer
- Offline retry / cancel interactive state machine
- Full command prompt packet logging
"""
import socket
import uuid
import json
import time
import threading
from poxp_protocol import (
    build_poxp_message, build_ack, parse_poxp_message, get_iso_timestamp,
    STATUS_OK, STATUS_CLIENT_UNAVAILABLE, STATUS_RELAY_ERROR, STATUS_SERVER_UNAVAILABLE
)
from storage_manager import StorageManager

class ClientNode:
    def __init__(self, client_email, server_ip, server_port=5025, local_ip="127.0.0.1", subnet="255.255.255.0", gateway="192.168.1.1"):
        self.client_email = client_email
        self.server_ip = server_ip
        self.server_port = server_port
        self.local_ip = self._detect_lan_ip(local_ip)
        self.subnet = subnet
        self.gateway = gateway
        self.storage = StorageManager(f"{client_email}_client_store.json")
        self.is_running = True

        # In-memory console log stream for terminal + GUI display
        self.console_logs = []
        self.log_lock = threading.Lock()

        # Offline retry state
        self.pending_retry = None  # Stores dict of last failed message
        # Guards retry_last_failed() so a double-click (or a slow request that
        # the user retries before it returns) can never send two copies of
        # the same retry at once -- only one retry attempt is ever in flight.
        self.retry_busy = threading.Lock()

        # Start 5-second automatic fetch daemon
        self.fetch_thread = threading.Thread(target=self._auto_fetch_loop, daemon=True)
        self.fetch_thread.start()

    def log(self, text):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{self.client_email}] [{timestamp}] {text}"
        print(entry)
        with self.log_lock:
            self.console_logs.append(entry)
            if len(self.console_logs) > 100:
                self.console_logs.pop(0)

    def _detect_lan_ip(self, fallback):
        """Auto-detects the machine's real physical IP on the local subnet."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((self.server_ip, self.server_port))
                return s.getsockname()[0]
        except Exception:
            return fallback

    def _send_raw_request(self, payload_str):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(30.0)  # server may hold mail ~10s (+ relay hop) before replying
            client.connect((self.server_ip, self.server_port))
            client.sendall(payload_str.encode("utf-8"))
            client.shutdown(socket.SHUT_WR)

            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)

            return parse_poxp_message(response.decode("utf-8", errors="replace"))

    def register_dns(self):
        """Dispatches REGISTER POXP/1.0 with network interface parameters."""
        headers = {
            "Client email": self.client_email,
            "DNS": self.server_ip,
            "Subnet Mask": self.subnet,
            "Gate": self.gateway,
            "IP": self.local_ip,
            "Identifier": str(uuid.uuid4())
        }
        msg = build_poxp_message("REGISTER", headers)
        self.log(f"[OUTGOING] REGISTER POXP/1.0 -> Server {self.server_ip}:{self.server_port} (Local IP: {self.local_ip})")
        try:
            reply = self._send_raw_request(msg)
        except OSError as e:
            reply = self._server_unavailable_ack(e)
            self.log(f"[REGISTER FAILED] {reply['method_or_code']} {reply['protocol_or_status']}: {reply['body']}")
            return reply
        if reply:
            self.log(f"[ACK RECEIVED] {reply.get('method_or_code')} {reply.get('protocol_or_status')} from {reply.get('headers', {}).get('Error at', 'server')}")
        return reply

    def _server_unavailable_ack(self, err):
        """503 SERVER_UNAVAILABLE: the client's OWN server can't be reached. A dead
        server can't send an ack, so the client raises this code locally."""
        # Built with the same serializer/parser as a real server ack so it carries
        # the same "503 SERVER_UNAVAILABLE POXP/1.0" status line.
        raw = build_ack(STATUS_SERVER_UNAVAILABLE[0], STATUS_SERVER_UNAVAILABLE[1], str(uuid.uuid4()), self.server_ip,
                        f"Server {self.server_ip}:{self.server_port} is unreachable ({err})")
        return parse_poxp_message(raw)

    def send_email(self, receiver_email, subject, body, priority="moderate", attachment_filename=None, attachment_bytes=None):
        """Dispatches SUBMIT POXP/1.0 with email headers, body, and optional 10MB attachment."""
        msg_id = str(uuid.uuid4())
        headers = {
            "Sender email": self.client_email,
            "Receiver email": receiver_email,
            "Date": get_iso_timestamp(),
            "Content-Type": "text",
            "Priority": priority,
            "Identifier": msg_id,
            "Subject": subject
        }
        msg = build_poxp_message("SUBMIT", headers, body, attachment_filename, attachment_bytes)
        att_str = f"Attachment: {attachment_filename} ({len(attachment_bytes)} bytes)" if attachment_bytes else "No attachment"
        self.log(f"[OUTGOING] SUBMIT POXP/1.0 -> To: {receiver_email} | Priority: {priority} | {att_str}")

        try:
            ack = self._send_raw_request(msg)
            code = ack.get("method_or_code") if ack else "None"
            status = ack.get("protocol_or_status") if ack else "NO_RESPONSE"

            # Check if timed out or failed due to offline recipient / server
            if code in tuple(str(s[0]) for s in (STATUS_CLIENT_UNAVAILABLE, STATUS_SERVER_UNAVAILABLE, STATUS_RELAY_ERROR)):
                self.log(f"[DELIVERY PAUSED] Server reported: {code} {status}. Reason: {ack.get('body', '')}")
                self.pending_retry = {
                    "receiver": receiver_email,
                    "subject": subject,
                    "body": body,
                    "priority": priority,
                    "att_name": attachment_filename,
                    "att_bytes": attachment_bytes,
                    "error_msg": ack.get("body", "")
                }
            elif code == str(STATUS_OK[0]):
                self.log(f"[DELIVERY SUCCESS] 200 OK — Email delivered and acknowledged. ID: {msg_id}")
                self.pending_retry = None
            else:
                self.log(f"[DELIVERY FAILED] Code {code} {status}: {ack.get('body', '')}")

            return ack
        except OSError as e:
            ack = self._server_unavailable_ack(e)
            self.log(f"[DELIVERY PAUSED] {ack['method_or_code']} {ack['protocol_or_status']}: {ack['body']}")
            self.pending_retry = {
                "receiver": receiver_email,
                "subject": subject,
                "body": body,
                "priority": priority,
                "att_name": attachment_filename,
                "att_bytes": attachment_bytes,
                "error_msg": ack["body"]
            }
            return ack
        except Exception as e:
            self.log(f"[SOCKET ERROR] Failed communicating with server: {e}")
            self.pending_retry = {
                "receiver": receiver_email,
                "subject": subject,
                "body": body,
                "priority": priority,
                "att_name": attachment_filename,
                "att_bytes": attachment_bytes,
                "error_msg": str(e)
            }
            return None

    def retry_last_failed(self):
        """Retries sending the last failed email.

        One-shot by design: if a retry is already in flight (e.g. the user
        clicked Retry twice before the first click's request came back), this
        call does nothing instead of dispatching a second copy. A fresh
        failure after a completed retry still repopulates pending_retry
        normally, so the user can retry again -- this only blocks *concurrent*
        duplicate clicks, not legitimate repeated attempts.
        """
        if not self.pending_retry:
            return None
        if not self.retry_busy.acquire(blocking=False):
            self.log("[RETRY IGNORED] A retry is already in progress for this message; duplicate click ignored.")
            return None
        try:
            data = self.pending_retry
            self.log(f"[RETRYING] Re-dispatching email to {data['receiver']}...")
            return self.send_email(
                data["receiver"], data["subject"], data["body"],
                data["priority"], data["att_name"], data["att_bytes"]
            )
        finally:
            self.retry_busy.release()

    def cancel_last_failed(self):
        """Cancels the pending retry."""
        if self.pending_retry:
            self.log(f"[CANCELLED] Cancelled pending transmission to {self.pending_retry['receiver']}.")
            self.pending_retry = None

    def fetch_inbox(self):
        """Queries FETCH POXP/1.0 and stores retrieved emails into client-side storage."""
        headers = {
            "Receiver email": self.client_email,
            "Identifier": str(uuid.uuid4()),
            "Date": get_iso_timestamp()
        }
        msg = build_poxp_message("FETCH", headers)
        try:
            reply = self._send_raw_request(msg)
            if reply and reply.get("body"):
                emails = json.loads(reply["body"])
                if emails:
                    for em in emails:
                        self.storage.client_save_email(em)
                    self.log(f"[AUTO-FETCH] Retrieved {len(emails)} new email(s) from server queue. Stored in client mailbox.")
                return self.storage.client_get_inbox()
        except Exception:
            pass
        return self.storage.client_get_inbox()

    def _auto_fetch_loop(self):
        """Background thread polling server every 5 seconds."""
        time.sleep(2.0)
        while self.is_running:
            try:
                self.fetch_inbox()
            except Exception:
                pass
            time.sleep(5.0)

    def shutdown(self):
        self.is_running = False
        self.log("[CLIENT SHUTDOWN] Shutting down client node...")