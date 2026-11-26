"""
server_node.py
Continuous Multi-Threaded Raw TCP Socket Server for POXP.
Features:
- Windows system hosts file resolution (C:\\Windows\\System32\\drivers\\etc\\hosts,
  or /etc/hosts on Linux/macOS). The machine's hosts file IS the DNS and is
  never modified by clients. A client may REGISTER only if its email is listed
  there AND it connects from the IP listed for it; FETCH/SUBMIT additionally
  require a prior REGISTER on this server. Anything else gets 404 CLIENT_NOT_FOUND.
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
    STATUS_RELAY_ERROR, STATUS_SERVER_ERROR, get_iso_timestamp,
    SERVER_CONN_TIMEOUT, RELAY_SOCKET_TIMEOUT, CLIENT_ACTIVE_WINDOW
)
from storage_manager import StorageManager

class ServerNode:
    def __init__(self, server_id, host="0.0.0.0", port=5025):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.storage = StorageManager(f"{server_id}_mailstore.json")
        self.dns_lock = threading.Lock()
        self.dns_table = {}  # exactly what the system hosts file says; clients never change it
        # Clients currently registered on THIS server: email -> session info.
        # Kept apart from dns_table so a REGISTER can never rewrite DNS.
        self.registered = {}
        self.is_running = True
        self.listen_socket = None

        # Load system hosts file
        self._load_hosts_file()

    def _load_hosts_file(self):
        """Loads hostname-to-IP mappings from the machine's system hosts file.

        Only the system DNS file is used (Windows path first, then /etc/hosts).
        There is deliberately no project-local DNS file: the laptop's own hosts
        file is the single source of truth for which servers and clients exist.
        """
        candidates = [
            r"C:\Windows\System32\drivers\etc\hosts",
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
            print(f"[{self.server_id}] [DNS NOTE] No system hosts file found. DNS table is empty, so every client REGISTER will be refused (404).")

    def update_dns(self, name, record):
        # Not called by request handling (clients can't change DNS); used only to
        # pre-load entries, e.g. by demo_errors.py standing in for a hosts file.
        with self.dns_lock:
            self.dns_table[name.lower()] = record
            print(f"[{self.server_id}] [DNS] Record updated for '{name}': IP={record.get('ip')} Server={record.get('server')}")

    def lookup_dns(self, name):
        with self.dns_lock:
            return self.dns_table.get(name.lower())

    def is_registered(self, email):
        with self.dns_lock:
            return email.lower() in self.registered

    def is_active(self, email):
        """Registered here AND still polling (REGISTER/FETCH within CLIENT_ACTIVE_WINDOW).
        A client that closed never says so, so this is how a stale registration expires."""
        with self.dns_lock:
            session = self.registered.get(email.lower())
        if not session:
            return False
        if session.get("fetching", 0) > 0:
            return True
        last = session.get("last_seen", session["registered_at"])
        return time.time() - last <= CLIENT_ACTIVE_WINDOW

    def other_servers(self):
        """Server ids (other than this one) listed in the DNS hosts file."""
        me = self.server_id.lower()
        with self.dns_lock:
            return sorted(k for k, v in self.dns_table.items() if v.get("server") == k and k != me)

    def _identity_error(self, email, addr):
        """Checks that `email` is listed in the DNS hosts file AND that this
        connection really comes from the IP listed for it. The IP used is the
        TCP peer address (addr[0]), not anything the client typed in a header,
        so it can't simply be claimed. Returns an error message, or None if OK."""
        rec = self.lookup_dns(email)
        if not rec:
            print(f"[{self.server_id}] [IDENTITY REFUSED] '{email}' is not listed in the DNS hosts file.")
            return f"Client {email} is not listed in {self.server_id}'s DNS hosts file."
        if str(rec.get("ip", "")).strip() != addr[0]:
            print(f"[{self.server_id}] [IDENTITY REFUSED] '{email}' connected from {addr[0]}, but the DNS hosts file lists {rec.get('ip')}.")
            return f"Client {email} is not listed in {self.server_id}'s DNS hosts file at address {addr[0]}."
        return None

    def _session_error(self, email, addr):
        """For FETCH/SUBMIT: the identity check above, plus the client must have
        REGISTERed on this server (so skipping REGISTER doesn't get around it)."""
        err = self._identity_error(email, addr)
        if err:
            return err
        if not self.is_registered(email):
            print(f"[{self.server_id}] [SESSION REFUSED] '{email}' has not registered on {self.server_id}.")
            return f"Client {email} has not registered on {self.server_id}."
        return None

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
            conn.settimeout(SERVER_CONN_TIMEOUT)
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
                # A FETCH reply carrying big attachments can take a while on a slow
                # link; the client counts as connected for that whole time, and
                # its "last seen" moves to when the reply finished.
                session = None
                if method == "FETCH":
                    with self.dns_lock:
                        session = self.registered.get((headers.get("Receiver email") or "").lower())
                    if session:
                        session["fetching"] = session.get("fetching", 0) + 1
                try:
                    conn.sendall(response_str.encode("utf-8"))
                finally:
                    if session:
                        session["fetching"] -= 1
                        session["last_seen"] = time.time()

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
            reg_id = headers.get("Identifier", str(uuid.uuid4()))
            if not client_email:
                return build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], reg_id, self.server_id, "Malformed REGISTER: missing header: Client email")
            # Only a client listed in this machine's DNS (system hosts file), and
            # connecting from the IP listed there, may join.
            err = self._identity_error(client_email, addr)
            if err:
                return build_ack(STATUS_CLIENT_NOT_FOUND[0], STATUS_CLIENT_NOT_FOUND[1], reg_id, self.server_id,
                                 f"{err} Registration refused.")
            # Record the session separately; the DNS table itself is never changed.
            with self.dns_lock:
                self.registered[client_email.lower()] = {
                    "ip": addr[0],
                    "reported_ip": headers.get("IP", ""),
                    "subnet": headers.get("Subnet Mask", ""),
                    "gateway": headers.get("Gate", ""),
                    "dns": headers.get("DNS", ""),
                    "registered_at": time.time(),
                }
            print(f"[{self.server_id}] [REGISTERED] '{client_email}' from {addr[0]} (matches DNS hosts file).")
            return build_ack(STATUS_REGISTERED[0], STATUS_REGISTERED[1], reg_id, self.server_id)

        elif method == "FETCH":
            client_email = headers.get("Receiver email")
            if not client_email:
                return build_ack(STATUS_BAD_REQUEST[0], STATUS_BAD_REQUEST[1], str(uuid.uuid4()), self.server_id, "Missing Receiver email")

            fetch_id = headers.get("Identifier", str(uuid.uuid4()))
            refusal = self._session_error(client_email, addr)
            if refusal:
                return build_ack(STATUS_CLIENT_NOT_FOUND[0], STATUS_CLIENT_NOT_FOUND[1], fetch_id, self.server_id, f"{refusal} Fetch refused.")
            with self.dns_lock:
                self.registered[client_email.lower()]["last_seen"] = time.time()

            emails = self.storage.fetch_inbox(client_email, clear_fetched=True)
            print(f"[{self.server_id}] [FETCH] Client '{client_email}' retrieved {len(emails)} emails. Purged from server.")
            return build_ack(STATUS_OK[0], STATUS_OK[1], headers.get("Identifier", str(uuid.uuid4())), self.server_id, json.dumps(emails))

        elif method == "SUBMIT":
            recipient = headers.get("Receiver email")
            msg_id = headers.get("Identifier", str(uuid.uuid4()))
            priority_val = headers.get("Priority", "moderate")

            # The sender must be the listed client at its listed IP, registered here.
            refusal = self._session_error(headers.get("Sender email"), addr)
            if refusal:
                return build_ack(STATUS_CLIENT_NOT_FOUND[0], STATUS_CLIENT_NOT_FOUND[1], msg_id, self.server_id, f"{refusal} Submit refused.")

            rec_info = self.lookup_dns(recipient)
            if not rec_info:
                return build_ack(STATUS_CLIENT_NOT_FOUND[0], STATUS_CLIENT_NOT_FOUND[1], msg_id, self.server_id, f"Destination {recipient} not found in DNS")

            # Any client may join any server, and the hosts file only says WHO a
            # client is (email + IP), not WHERE it joined. So: a recipient that is
            # connected to this server is delivered here; otherwise the mail is
            # relayed to the other server (which holds it for pickup, or reports
            # 480 if the recipient isn't connected there either). With no other
            # server in DNS (single-server setup) it is held here.
            if self.is_active(recipient):
                dest_server = self.server_id
            else:
                others = self.other_servers()
                dest_server = others[0] if others else self.server_id
                if others:
                    print(f"[{self.server_id}] [ROUTING] '{recipient}' is not connected to {self.server_id}; relaying to {dest_server}.")

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
                            # for the recipient to fetch, and a large attachment
                            # takes time to transfer, so allow for both.
                            r_sock.settimeout(RELAY_SOCKET_TIMEOUT)
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
                            return build_ack(STATUS_OK[0], STATUS_OK[1], msg_id, "None", parsed_ack.get("body") or "Relayed and acknowledged by remote server.")
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