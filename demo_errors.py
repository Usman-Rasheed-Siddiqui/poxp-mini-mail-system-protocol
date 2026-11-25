"""
demo_errors.py
Demonstrates every POXP status code against two throw-away local servers
(ports 5201/5202, separate mailstore files in a temp dir). Takes ~35 seconds.

    python demo_errors.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="poxp_demo_"))  # keep demo mailstores out of the repo

from server_node import ServerNode
from client_node import ClientNode
from poxp_protocol import build_poxp_message

PORT_A, PORT_B = 5201, 5202


def banner(title, explanation):
    print("\n" + "=" * 70)
    print(f"{title}\n  {explanation}")
    print("=" * 70)


def result(ack):
    print(f"  >>> RESULT: {ack.get('method_or_code')} {ack.get('protocol_or_status')}  |  {ack.get('body', '')}")


def raw(port, text, source_ip=None):
    """Send arbitrary text to a server, return the parsed ack. `source_ip` makes the
    connection come from another local address (e.g. 127.0.0.2) to play an impostor."""
    import socket
    from poxp_protocol import parse_poxp_message
    with socket.socket() as s:
        if source_ip:
            s.bind((source_ip, 0))
        s.settimeout(30)
        s.connect(("127.0.0.1", port))
        s.sendall(text.encode())
        s.shutdown(socket.SHUT_WR)
        data = bytearray()
        while chunk := s.recv(4096):
            data.extend(chunk)
    return parse_poxp_message(data.decode(errors="replace"))


def main():
    srv_a = ServerNode("server_a", "127.0.0.1", PORT_A)
    srv_b = ServerNode("server_b", "127.0.0.1", PORT_B)
    for s in (srv_a, srv_b):
        threading.Thread(target=s.start, daemon=True).start()
    time.sleep(1)
    for s in (srv_a, srv_b):
        s.update_dns("server_a", {"ip": "127.0.0.1", "port": PORT_A, "server": "server_a"})
        s.update_dns("server_b", {"ip": "127.0.0.1", "port": PORT_B, "server": "server_b"})

    # Alice is listed in server A's DNS (as if from its hosts file); Mallory is not.
    srv_a.update_dns("alice@poxp.com", {"ip": "127.0.0.1", "server": "server_a"})
    alice = ClientNode("alice@poxp.com", "127.0.0.1", PORT_A)

    banner("201 REGISTERED", "Alice (listed in server A's DNS hosts file) announces herself to her server.")
    result(alice.register_dns())

    mallory = ClientNode("mallory@poxp.com", "127.0.0.1", PORT_A)
    banner("404 CLIENT_NOT_FOUND (REGISTER)",
           "Mallory is NOT in server A's DNS hosts file, so her registration is refused and she stays idle.")
    result(mallory.register_dns())
    mallory.shutdown()

    banner("404 CLIENT_NOT_FOUND (impostor IP)",
           "Someone claims to be alice@poxp.com but connects from 127.0.0.2, not the IP in the DNS hosts file (127.0.0.1).")
    result(raw(PORT_A, build_poxp_message("REGISTER", {"Client email": "alice@poxp.com", "IP": "127.0.0.1", "Identifier": "demo-imp"}),
               source_ip="127.0.0.2"))

    srv_b.update_dns("dave@poxp.com", {"ip": "127.0.0.1", "server": "server_b"})
    banner("404 CLIENT_NOT_FOUND (FETCH without REGISTER)",
           "Dave is listed on server B with the right IP but never registered, so he may not read a mailbox there.")
    result(raw(PORT_B, build_poxp_message("FETCH", {"Receiver email": "dave@poxp.com", "Identifier": "demo-nofetch"})))

    banner("400 BAD_REQUEST", "A SUBMIT arrives with no 'Receiver email' header (malformed request).")
    result(raw(PORT_A, build_poxp_message("SUBMIT", {"Sender email": "alice@poxp.com", "Identifier": "demo-400"}, "hi")))

    banner("400 BAD_REQUEST (no protocol token)", "A request whose first line lacks 'POXP/1.0' is rejected.")
    result(raw(PORT_A, "SUBMIT\nSender email: alice@poxp.com\nReceiver email: bob@poxp.com\n\nhi\n"))

    banner("404 CLIENT_NOT_FOUND", "Alice mails someone who is in no server's DNS/hosts table.")
    result(alice.send_email("nobody@poxp.com", "hi", "hello"))

    # Bob is known to both servers (as if from the hosts file / REGISTER) but never fetches mail.
    srv_a.update_dns("bob@poxp.com", {"ip": "127.0.0.1", "server": "server_a"})
    banner("480 CLIENT_UNAVAILABLE (same server)",
           "Bob is known on Alice's server but is not running. Server holds the mail 10s, then deletes it.")
    result(alice.send_email("bob@poxp.com", "hi", "hello"))
    alice.cancel_last_failed()

    srv_a.update_dns("carol@poxp.com", {"ip": "127.0.0.1", "server": "server_b"})
    srv_b.update_dns("carol@poxp.com", {"ip": "127.0.0.1", "server": "server_b"})
    banner("480 CLIENT_UNAVAILABLE (other server)",
           "Carol lives on server B, which is up, but Carol is not running. B holds 10s, deletes, reports back via A.")
    result(alice.send_email("carol@poxp.com", "hi", "hello"))
    alice.cancel_last_failed()

    srv_b.shutdown()
    time.sleep(0.5)
    banner("502 RELAY_ERROR", "Carol's server (B) is DOWN, so server A cannot relay the mail.")
    result(alice.send_email("carol@poxp.com", "hi", "hello"))
    alice.cancel_last_failed()

    srv_a.shutdown()
    time.sleep(0.5)
    banner("503 SERVER_UNAVAILABLE", "Alice's OWN server (A) is down. Raised by the client itself (a dead server can't reply).")
    result(alice.register_dns())

    print("\n(500 SERVER_ERROR is only a catch-all for an unexpected crash inside the server; it has no normal trigger.)")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
