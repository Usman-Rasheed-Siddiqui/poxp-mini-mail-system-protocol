"""
server_node.py
Continuous Multi-Threaded Raw TCP Socket Server for POXP.
Features:
- Windows system hosts file resolution (C:\\Windows\\System32\\drivers\\etc\\hosts)
- Priority-ordered mailbox delivery (important > moderate > low, FIFO
  tie-breaking) applied at fetch time in storage_manager.py — no dispatch
  queue or worker-thread pool; each accepted connection is handled on its
  own thread as before.
- 10-second offline recipient/relay retry window with descriptive status codes
- Server queue auto-purge upon client fetch
- Full command prompt packet inspection and logging
- Clean shutdown on 'End' key or 'end'/'exit' command
"""
import socket
import threading
import time
import uuid
import sys
import os
import json
from poxp_protocol import (
    parse_poxp_message, build_ack, build_poxp_message, POXP_VERSION,
    STATUS_OK, STATUS_REGISTERED, STATUS_BAD_REQUEST,
    STATUS_CLIENT_NOT_FOUND, STATUS_CLIENT_UNAVAILABLE,
    STATUS_RELAY_ERROR, STATUS_SERVER_ERROR, get_iso_timestamp
)
from storage_manager import StorageManager

class ServerNode:
    def __init__(self, server_id, host="0.0.0.0", port=5025):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.storage = StorageManager(f"{server_id}_mailstore.json")
        self.dns_lock = threading.Lock()
        self.dns_table = {}
        self.is_running = True
        self.listen_socket = None

        # Load system hosts file
        self._load_hosts_file()

    def _load_hosts_file(self):
        """Loads hostname-to-IP mappings directly from Windows system hosts file."""
        candidates = [
            r"C:\Windows\System32\drivers\etc\hosts",
            os.path.join(os.getcwd(), "hosts"),
            "/etc/hosts"
        ]
        loaded_path = None
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            clean = line.strip()
                            if not clean or clean.startswith("#"):
                                continue
                            parts = clean.split()
                            if len(parts) >= 2:
                                ip = parts[0]
                                for host in parts[1:]:
                                    self.dns_table[host.lower()] = {
                                        "ip": ip,
                                        "server": "server_a" if "server_a" in host or "client_a" in host else "server_b",
                                        "port": 5025
                                    }
                    loaded_path = path
                    break
                except Exception as e:
                    print(f"[{self.server_id}] [DNS WARNING] Failed reading '{path}': {e}")

        if loaded_path:
            print(f"[{self.server_id}] [DNS] Successfully loaded DNS mappings from '{loaded_path}' ({len(self.dns_table)} entries).")
        else:
            print(f"[{self.server_id}] [DNS NOTE] No system hosts file found. Initialized with empty dynamic DNS directory.")

    def update_dns(self, name, record):
        with self.dns_lock:
            self.dns_table[name.lower()] = record
            print(f"[{self.server_id}] [DNS] Record updated for '{name}': IP={record.get('ip')} Server={record.get('server')}")

    def lookup_dns(self, name):
        with self.dns_lock:
            return self.dns_table.get(name.lower())

    def start(self):
        """Starts socket server and keyboard shutdown listener."""
        self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listen_socket.bind((self.host, self.port))
        self.listen_socket.listen()

        print("=" * 60)
        print(f"[{self.server_id}] POXP Server Online on {self.host}:{self.port} (Raw TCP)")
        print(f"[{self.server_id}] Press 'End' key or type 'end'/'exit' + Enter to shutdown.")
        print("=" * 60)

        # Launch keyboard shutdown thread
        shutdown_thread = threading.Thread(target=self._shutdown_listener, daemon=True)
        shutdown_thread.start()

        while self.is_running:
            try:
                conn, addr = self.listen_socket.accept()
                t = threading.Thread(target=self.handle_connection, args=(conn, addr), daemon=True)
                t.start()
            except OSError:
                break

        print(f"[{self.server_id}] Server listener closed. Goodbye.")

    def _shutdown_listener(self):
        """Listens for the physical 'End' key (Windows) or text input for graceful shutdown."""
        try:
            import msvcrt
            while self.is_running:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    # Extended key on Windows starts with \x00 or \xe0
                    if ch in (b'\x00', b'\xe0'):
                        key = msvcrt.getch()
                        # Key code 79 is 'End' key
                        if key == b'O' or key == b'\x4f' or key == b'1':
                            print(f"\n[{self.server_id}] 'End' key pressed! Shutting down server gracefully...")
                            self.shutdown()
                            break
                time.sleep(0.05)
        except ImportError:
            while self.is_running:
                try:
                    line = sys.stdin.readline().strip().lower()
                    if line in ("end", "exit", "quit", "q"):
                        print(f"\n[{self.server_id}] Shutdown command received! Closing...")
                        self.shutdown()
                        break
                except Exception:
                    break

    def shutdown(self):
        self.is_running = False
        if self.listen_socket:
            try:
                self.listen_socket.close()
            except Exception:
                pass

    def handle_connection(self, conn, addr):
        with conn:
            conn.settimeout(30.0)
            try:
                # Read until the peer half-closes its write side (recv() returns
                # b""). The client always calls shutdown(SHUT_WR) right after
                # sendall(), so this is a reliable end-of-message signal for
                # messages of any size, including multi-megabyte base64
                # attachments spread across many 4096-byte chunks.
                #
                # (Previously this loop broke out as soon as it saw the
                # header/body "\n\n" separator in whatever had arrived so far,
                # which truncated any message whose body didn't fit in the
                # first chunk or two — this is what corrupted attachment
                # transfers. Do not reintroduce an early-break heuristic here.)
                raw_bytes = bytearray()
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw_bytes.extend(chunk)

                raw_text = raw_bytes.decode("utf-8", errors="replace")
                parsed = parse_poxp_message(raw_text)

                if not parsed:
                    conn.sendall(build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], str(uuid.uuid4()), self.server_id).encode("utf-8"))
                    return

                # Every POXP request must carry the protocol token (e.g. 'SUBMIT POXP/1.0').
                if parsed.get('protocol_or_status') != POXP_VERSION:
                    conn.sendall(build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], str(uuid.uuid4()), self.server_id, f"Missing or unsupported protocol token; expected '{POXP_VERSION}'").encode("utf-8"))
                    return

                # Packet logging to command prompt
                headers = parsed.get("headers", {})
                method = parsed.get("method_or_code")
                msg_id = headers.get("Identifier", "N/A")
                sender = headers.get("Sender email") or headers.get("Client email")
                recipient = headers.get("Receiver email")
                prio = headers.get("Priority", "normal")
                att_info = f"Attachment: {headers.get('Attachment-Name')} ({headers.get('Attachment-Length', '0')} bytes)" if "Attachment-Name" in headers else "No attachment"

                print(f"\n--- [TCP PACKET RECEIVED from {addr[0]}:{addr[1]}] ---")
                print(f"Method: {method} POXP/1.0 | Identifier: {msg_id}")
                print(f"From: {sender} -> To: {recipient}")
                if method in ("SUBMIT", "RELAY"):
                    print(f"Priority: {prio} | Content-Type: {headers.get('Content-Type', 'text')}")
                    print(att_info)

                response_str = self.dispatch_request(parsed, addr)
                conn.sendall(response_str.encode("utf-8"))

                # Log the outgoing ACK
                resp_parsed = parse_poxp_message(response_str)
                if resp_parsed:
                    print(f"--- [ACK SENT to {addr[0]}:{addr[1]}] -> {resp_parsed.get('method_or_code')} {resp_parsed.get('protocol_or_status')} ---\n")

            except Exception as e:
                print(f"[{self.server_id}] [ERROR] Exception processing connection: {e}")
                err_ack = build_ack(STATUS_SERVER_ERROR[0], STATUS_SERVER_ERROR[1], str(uuid.uuid4()), self.server_id, str(e))
                try:
                    conn.sendall(err_ack.encode("utf-8"))
                except Exception:
                    pass

    def _hold_for_pickup(self, recipient, msg_id, hold_seconds=10.0):
        """Keeps a stored email for up to `hold_seconds` waiting for the recipient
        to fetch it. Returns True if fetched. Otherwise deletes that one email
        from the server (so a client waking up later never sees it) and returns False."""
        deadline = time.time() + hold_seconds
        while time.time() < deadline:
            if not self.storage.has_email(recipient, msg_id):
                return True
            time.sleep(0.25)
        # remove_email() is False only if the client fetched it at the last instant.
        if self.storage.remove_email(recipient, msg_id):
            print(f"[{self.server_id}] [TIMEOUT] Recipient '{recipient}' did not fetch within {hold_seconds:.0f}s. Email {msg_id} deleted from server.")
            return False
        return True

    def dispatch_request(self, parsed, addr):
        method = parsed["method_or_code"]
        headers = parsed["headers"]
        body = parsed["body"]
        raw_body = parsed.get("raw_body", body)
        attachment_b64 = parsed.get("attachment_b64")

        # 400: malformed request (a SUBMIT/RELAY must name both sender and receiver)
        if method in ("SUBMIT", "RELAY"):
            missing = [h for h in ("Sender email", "Receiver email") if not headers.get(h)]
            if missing:
                return build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], headers.get("Identifier", str(uuid.uuid4())), self.server_id, f"Malformed {method}: missing header(s): {', '.join(missing)}")

        if method == "REGISTER":
            client_email = headers.get("Client email") or headers.get("Sender email")
            ip = headers.get("IP", addr[0])
            subnet = headers.get("Subnet Mask", "")
            gateway = headers.get("Gate", "")
            dns_server = headers.get("DNS", self.server_id)
            record = {
                "ip": ip,
                "subnet": subnet,
                "gateway": gateway,
                "dns": dns_server,
                "server": self.server_id,
                "registered_at": time.time()
            }
            if client_email:
                self.update_dns(client_email, record)
            return build_ack(STATUS_REGISTERED[0], STATUS_REGISTERED[1], headers.get("Identifier", str(uuid.uuid4())), self.server_id)

        elif method == "FETCH":
            client_email = headers.get("Receiver email")
            if not client_email:
                return build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], str(uuid.uuid4()), self.server_id, "Missing Receiver email")

            rec = self.lookup_dns(client_email)
            if rec:
                rec["last_seen"] = time.time()

            emails = self.storage.fetch_inbox(client_email, clear_fetched=True)
            print(f"[{self.server_id}] [FETCH] Client '{client_email}' retrieved {len(emails)} emails. Purged from server.")
            return build_ack(STATUS_OK[0], STATUS_OK[1], headers.get("Identifier", str(uuid.uuid4())), self.server_id, json.dumps(emails))

        elif method == "SUBMIT":
            recipient = headers.get("Receiver email")
            msg_id = headers.get("Identifier", str(uuid.uuid4()))
            priority_val = headers.get("Priority", "moderate")

            rec_info = self.lookup_dns(recipient)
            if not rec_info:
                return build_ack(STATUS_CLIENT_NOT_FOUND[0], STATUS_CLIENT_NOT_FOUND[1], msg_id, self.server_id, f"Destination {recipient} not found in DNS")

            dest_server = rec_info.get("server")

            # Local delivery on the same server
            if dest_server == self.server_id:
                record = {
                    "headers": headers,
                    "body": body,
                    "raw_body": raw_body,
                    "attachment_b64": attachment_b64,
                    "received_at": get_iso_timestamp(),
                    "enqueued_time": time.time()
                }
                self.storage.save_incoming_email(recipient, record)

                if self._hold_for_pickup(recipient, msg_id):
                    return build_ack(STATUS_OK[0], STATUS_OK[1], msg_id, "None", "Email delivered and retrieved by recipient client.")
                return build_ack(STATUS_CLIENT_UNAVAILABLE[0], STATUS_CLIENT_UNAVAILABLE[1], msg_id, self.server_id, f"Recipient {recipient} is offline (no fetch within 10s timeout).")

            # Cross-server relay
            else:
                relay_server_entry = self.lookup_dns(dest_server)
                if not relay_server_entry:
                    return build_ack(STATUS_RELAY_ERROR[0], STATUS_RELAY_ERROR[1], msg_id, self.server_id, f"Relay target server '{dest_server}' unknown in DNS")

                remote_ip = relay_server_entry.get("ip")
                remote_port = int(relay_server_entry.get("port", 5025))

                relay_headers = dict(headers)
                relay_headers["Server id"] = self.server_id
                relay_msg = build_poxp_message("RELAY", relay_headers, raw_body)

                # 10-second retry loop if target server is down
                start_relay_time = time.time()
                last_err = ""
                while time.time() - start_relay_time < 10.0:
                    try:
                        print(f"[{self.server_id}] [RELAY ATTEMPT] Attempting cross-server connection to {dest_server} at {remote_ip}:{remote_port}...")
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as r_sock:
                            # Remote server may hold the mail up to 10s waiting
                            # for the recipient to fetch, so allow for that.
                            r_sock.settimeout(15.0)
                            r_sock.connect((remote_ip, remote_port))
                            r_sock.sendall(relay_msg.encode("utf-8"))
                            r_sock.shutdown(socket.SHUT_WR)

                            resp = bytearray()
                            while True:
                                chunk = r_sock.recv(4096)
                                if not chunk:
                                    break
                                resp.extend(chunk)

                        parsed_ack = parse_poxp_message(resp.decode("utf-8", errors="replace"))
                        if parsed_ack and parsed_ack.get("method_or_code") == str(STATUS_OK[0]):
                            print(f"[{self.server_id}] [RELAY SUCCESS] Remote {dest_server} accepted relay for {recipient}.")
                            return build_ack(STATUS_OK[0], STATUS_OK[1], msg_id, "None", "Relayed and acknowledged by remote server.")
                        elif parsed_ack and parsed_ack.get("method_or_code") == str(STATUS_CLIENT_UNAVAILABLE[0]):
                            # Remote server held the mail 10s, recipient never
                            # fetched it, and the remote server already deleted it.
                            # Tell client A now; do NOT keep retrying (would duplicate).
                            print(f"[{self.server_id}] [RELAY] Recipient '{recipient}' unavailable on {dest_server}. Reporting back to sender.")
                            return build_ack(STATUS_CLIENT_UNAVAILABLE[0], STATUS_CLIENT_UNAVAILABLE[1], msg_id, dest_server, parsed_ack.get("body") or f"Recipient {recipient} is offline (no fetch within 10s timeout).")
                        else:
                            last_err = resp.decode("utf-8", errors="replace")
                            time.sleep(1.0)
                    except Exception as e:
                        last_err = str(e)
                        time.sleep(1.0)

                print(f"[{self.server_id}] [RELAY TIMEOUT] Target server {dest_server} ({remote_ip}) was offline for 10 seconds: {last_err}")
                # Relay could not be completed because the destination server is down/unreachable.
                return build_ack(STATUS_RELAY_ERROR[0], STATUS_RELAY_ERROR[1], msg_id, dest_server, f"Relay failed: destination server '{dest_server}' is down or unreachable (10s timeout exceeded): {last_err}")

        elif method == "RELAY":
            recipient = headers.get("Receiver email")
            msg_id = headers.get("Identifier", str(uuid.uuid4()))
            record = {
                "headers": headers,
                "body": body,
                "raw_body": raw_body,
                "attachment_b64": attachment_b64,
                "relayed_by": headers.get("Server id"),
                "received_at": get_iso_timestamp(),
                "enqueued_time": time.time()
            }
            self.storage.save_incoming_email(recipient, record)
            print(f"[{self.server_id}] [RELAY STORED] Relayed message from {headers.get('Server id')} held for client '{recipient}' (up to 10s).")
            if self._hold_for_pickup(recipient, msg_id):
                return build_ack(STATUS_OK[0], STATUS_OK[1], msg_id, "None", "Email delivered and retrieved by recipient client.")
            return build_ack(STATUS_CLIENT_UNAVAILABLE[0], STATUS_CLIENT_UNAVAILABLE[1], msg_id, self.server_id, f"Recipient {recipient} is offline (no fetch within 10s timeout).")

        return build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], str(uuid.uuid4()), self.server_id, f"Unknown method {method}")

if __name__ == "__main__":
    s_id = sys.argv[1] if len(sys.argv) > 1 else "server_a"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5025
    node = ServerNode(server_id=s_id, port=port)
    node.start()