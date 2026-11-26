# poxp-mini-mail-system-protocol

**POXP (Post Office Xchange Protocol)** — a custom mail system protocol over raw TCP sockets, with
two mail servers that relay to each other, a browser-based client GUI, a status-code demo, a
UDP comparison experiment, and a Wireshark dissector.

---

## Files at a glance

| File | What it is | How you run it |
|---|---|---|
| `server_node.py` | POXP mail server (raw TCP) | `python server_node.py <server_id> <port>` |
| `gui_app.py` | Client node + web GUI (opens in your browser) | `python gui_app.py <email> <server_ip> <server_port> <web_port>` |
| `demo_errors.py` | Demonstrates every POXP status code locally | `python demo_errors.py` |
| `udp_receiver.py` | Receives a file over raw UDP (no reliability) | `python udp_receiver.py <port> <output_file> [idle_timeout]` |
| `udp_sender.py` | Sends a file over raw UDP (no reliability) | `python udp_sender.py <receiver_ip> <port> <file> [chunk_size]` |
| `poxp.lua` | Wireshark dissector for POXP | Copied into Wireshark's plugins folder (see below) |
| `client_node.py` | Client engine used by `gui_app.py` | Not run directly |
| `poxp_protocol.py` | Message builder/parser, status codes, timeouts | Not run directly |
| `storage_manager.py` | JSON mailbox storage for servers and clients | Not run directly |

---

## 1. Setup (once per machine)

Requires **Python 3.11+**.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On Linux/macOS activate with `source .venv/bin/activate` and use `python3` in the commands below.

**Firewall:** allow Python through Windows Defender Firewall (accept the prompt the first time a
server starts), or open **TCP 5025** (POXP) and **UDP 6000** (UDP experiment) for inbound traffic.

---

## 2. Running the mail system

### 2.1 Start a server

```powershell
python server_node.py server_a 5025
```

| Argument | Meaning | Default |
|---|---|---|
| `server_id` | `server_a` or `server_b` | `server_a` |
| `port` | TCP port the server listens on | `5025` |

The server prints every packet it receives and every acknowledgement it sends.
**To stop it:** press the **End** key (Windows), or type `end` / `exit` and press Enter (Linux/macOS).

The server keeps queued mail in `<server_id>_mailstore.json` in the current folder.

### 2.2 Start a client (with its GUI)

```powershell
python gui_app.py client_a@poxpmail.com 192.168.137.1 5025 8000
```

| Argument | Meaning | Default |
|---|---|---|
| `email` | This client's address | `client_a@poxpmail.com` |
| `server_ip` | IP of the server this client belongs to | `127.0.0.1` |
| `server_port` | That server's POXP port | `5025` |
| `web_port` | Local port for the GUI in your browser | `8000` |

Then open **http://localhost:8000** in a browser. The client registers with its server on start-up
and checks for new mail every 5 seconds. Its own mailbox is stored in
`<email>_client_store.json`.

> **`web_port` must be different from `server_port` if the client runs on the same machine as
> its server** — see the [FAQ](#faq-what-if-both-client-ports-are-5025).
> To run several clients on one machine, give each its own `web_port` (8000, 8001, …).

### 2.3 Full setup: four separate machines (recommended)

To see how servers and clients really behave, run **each node on its own machine**, all on the same
network (e.g. the same Wi-Fi or hotspot):

| Machine | Runs | Example IP |
|---|---|---|
| **Machine 1 — Server A** | `server_node.py server_a` | `192.168.137.1` |
| **Machine 2 — Client A** | `gui_app.py client_a@poxpmail.com` | `192.168.137.10` |
| **Machine 3 — Server B** | `server_node.py server_b` | `192.168.137.178` |
| **Machine 4 — Client B** | `gui_app.py client_b@poxpmail.com` | `192.168.137.20` |

The example IPs are placeholders: find each machine's real IP with `ipconfig` (Windows, look for
*IPv4 Address*) or `ip addr` (Linux) and use those everywhere below.

```
 Client A ──SUBMIT──▶ Server A ══RELAY══▶ Server B ◀──FETCH── Client B
 (Machine 2)         (Machine 1)         (Machine 3)         (Machine 4)
```

A mail from Client A to Client B travels Client A → Server A → (relay) → Server B → Client B, and
the acknowledgement travels back the same way. Each client only ever talks to its **own** server.

#### What each machine needs

| | Server A / Server B | Client A / Client B |
|---|---|---|
| Python + this repo + `pip install -r requirements.txt` | yes | yes |
| Hosts file entries (DNS) | **yes** — see below | no |
| Inbound firewall rule | **TCP 5025** must be allowed | none (clients only connect out) |
| Port | listens on **5025** | GUI on any free local port, e.g. 8000 |

#### Step 1 — DNS: the hosts file (server machines only)

The server machine's **system hosts file is POXP's DNS table**: it is the only place a server learns
which servers and clients exist. At start-up the server reads:

- `C:\Windows\System32\drivers\etc\hosts` on Windows, or
- `/etc/hosts` on Linux/macOS.

There is no project-local DNS file: a `hosts` file placed next to the code is **not** used.

How the server reads each line (`<IP> <name> <name> ...`):
- Blank lines and lines starting with `#` are ignored.
- The names `server_a` and `server_b` tell each server where the servers are. Relays to a server
  always use **port 5025**, so both servers must listen on 5025.
- Every other name is a client email, listed with **its own laptop's IP**. The hosts file says *who*
  a client is, not which server it joins: any listed client can join either server.

**How mail finds its recipient:** if the recipient is connected to the sender's server, it is
delivered there. Otherwise that server relays it to the other server, which delivers it if the
recipient is connected there, or holds it 10 s and reports **480** if not. "Connected" means the
client is still polling (it fetched within the last 15 s), so a client that has closed stops
counting even though it never said goodbye. In a one-server setup, leave the other server out of
the hosts file; otherwise mail to an offline client would wait on the missing server and end in
**502**.

**Every client must be listed in the hosts file, with its own laptop's IP, before it joins.** The
hosts file is trusted exactly as written; clients can never change it. A server:

- accepts a `REGISTER` only if the **email is listed** *and* the connection **really comes from the
  IP listed for that email** (the server checks the network connection's source address, not the IP
  the client reports about itself);
- accepts `FETCH` and `SUBMIT` only from a client that passes the same check **and** has registered
  on this server, so nobody can skip `REGISTER` to read or send mail as someone else.

Anything else is refused with **404 CLIENT_NOT_FOUND**, e.g. *"Client client_a@poxpmail.com is not
listed in server_a's DNS hosts file at address 192.168.137.55. Registration refused."* A refused
client stays **idle** (it can't send or receive, and its GUI says why). Mail to a recipient that
isn't listed is also answered with 404.

Add the servers and **all** clients, each with its **real IP**, to the hosts file on **both** server
machines (Machine 1 and Machine 3):

```
192.168.137.1     server_a
192.168.137.178   server_b
192.168.137.10    client_a@poxpmail.com
192.168.137.20    client_b@poxpmail.com
```

Rules for the IPs:
- Use each laptop's **LAN IP** as shown by `ipconfig` (e.g. `192.168.137.x` on a Windows mobile
  hotspot), not `127.0.0.1`. A client running on the same laptop as its server must also connect to
  the server's LAN IP.
- Hotspot/DHCP addresses can change when a PC reconnects. If a client's IP changes, it will be
  refused until both servers' hosts files are updated and the servers restarted, so check
  `ipconfig` on every PC before a session.
- Matching the IP stops other PCs from posing as a client, but it is an address check, not a
  password: a PC that is manually given the same IP while the real one is offline would pass.

**Editing it on Windows:** run Notepad as Administrator (Start → type *Notepad* → right-click →
*Run as administrator*) → File → Open → `C:\Windows\System32\drivers\etc\hosts` (set the file type
to *All Files*) → add the lines at the end → Save.
**On Linux:** `sudo nano /etc/hosts`.

The server reads the file only when it starts, so **restart a server after editing its hosts
file**. On start-up the server prints `[DNS] Successfully loaded DNS mappings from '<path>' (N
entries)`, which shows which file it used.

#### Step 2 — Start the servers first

```powershell
# Machine 1 (Server A)
python server_node.py server_a 5025
```

```powershell
# Machine 3 (Server B)
python server_node.py server_b 5025
```

#### Step 3 — Then start the clients, each pointing at its own server's IP

```powershell
# Machine 2 (Client A) -> Server A's IP
python gui_app.py client_a@poxpmail.com 192.168.137.1 5025 8000
```

```powershell
# Machine 4 (Client B) -> Server B's IP
python gui_app.py client_b@poxpmail.com 192.168.137.178 5025 8000
```

On each client machine open **http://localhost:8000**. Each client registers with its server only
**once, at start-up**, so its server must already be running; if you start a client too early (or
restart its server), restart the client too: registrations live in the server's memory, so after a
server restart every client must register again. Its console should show `201 REGISTERED`; if it
shows `404 CLIENT_NOT_FOUND`, the client's email or IP doesn't match that server's hosts file
(Step 1).

#### Step 4 — Send mail

From Client A's GUI send to `client_b@poxpmail.com` (or from Client B to `client_a@poxpmail.com`).
Watch the server consoles: Server A logs the `SUBMIT` and the `RELAY ATTEMPT`, Server B logs the
`RELAY STORED` and the `FETCH` by Client B, and the `200 OK` comes back to Client A.

To see the error cases: close Client B and send again (**480** after ~10 s); stop Server B and send
(**502** after ~10 s); stop Server A and send from Client A (**503**).

### 2.4 Adding more clients

There is no fixed pairing between clients and servers: **any listed client can join any server**,
simply by passing that server's IP when it starts. **Register each client on one server only**:
servers don't tell each other who registered where, so a client registered on both would get some of
its mail on one connection and some on the other. You are free to add as many clients as you like, with any
email address, on their own machines (or several on one machine with different GUI ports: 8000,
8001, …). For example, all of these can join Server A:

```powershell
# each on its own machine (on one machine, give each a different last number: 8000, 8001, 8002)
python gui_app.py client_a@poxpmail.com 192.168.137.1 5025 8000
python gui_app.py client_b@poxpmail.com 192.168.137.1 5025 8000
python gui_app.py client_c@poxpmail.com 192.168.137.1 5025 8000
```

and the same addresses could equally join Server B by passing `192.168.137.178` instead.

**To add a new client, the server side goes first:**

1. **Add the client to the DNS hosts file** on the server machine(s) — on **both** servers in a
   two-server setup (Step 1), e.g.
   ```
   192.168.137.30    client_c@poxpmail.com
   ```
2. **Restart the server(s)** so they load the new entry (check the `[DNS] Successfully loaded …`
   line).
3. **Only then start the client** from its own laptop, pointing at the server it wants to join:
   ```powershell
   python gui_app.py client_c@poxpmail.com 192.168.137.1 5025 8000
   ```

A client that is not in the hosts file is refused when it tries to join (`REGISTER` →
**404 CLIENT_NOT_FOUND**) and stays idle, and mail sent to it also gets **404 CLIENT_NOT_FOUND**.
The same happens if a listed email connects from a different IP than the one in the hosts file.

### 2.5 Quick test on one machine (single server)

Not a substitute for the four-machine setup, but handy for checking that everything starts. As
in 2.4, add the clients to the hosts file first; since everything runs on one PC and connects to
`127.0.0.1`, list the clients with IP `127.0.0.1`:

```powershell
python server_node.py server_a 5025
python gui_app.py client_a@poxpmail.com 127.0.0.1 5025 8000
python gui_app.py client_c@poxpmail.com 127.0.0.1 5025 8001
```

Run each in its own terminal, then use http://localhost:8000 and http://localhost:8001.
Both clients are on server_a, so mail is delivered locally (no relay).

---

## 3. Status codes

| Code | Name | When |
|---|---|---|
| 200 | OK | Mail delivered and fetched by the recipient |
| 201 | REGISTERED | Client registered with its server |
| 400 | BAD_REQUEST | Malformed request / missing `POXP/1.0` token |
| 404 | CLIENT_NOT_FOUND | Recipient not in the server's DNS (hosts file); or a client's email/IP doesn't match the hosts file (`REGISTER`, `FETCH`, `SUBMIT`); or it sent `FETCH`/`SUBMIT` without registering on that server |
| 480 | CLIENT_UNAVAILABLE | Recipient isn't connected to either server; the server that held the mail 10 s deleted it (the GUI names that server) |
| 500 | SERVER_ERROR | Unexpected error inside a server |
| 502 | RELAY_ERROR | Destination server is down/unreachable (10 s retry window) |
| 503 | SERVER_UNAVAILABLE | Raised by the client itself when its own server can't be reached or doesn't answer in time |

On 480, 502 and 503 the GUI shows **Retry Sending** / **Cancel**.

### Timeouts

Defined in `poxp_protocol.py`. Each is an *idle* timeout per socket operation, so a slow but
steady transfer of a large attachment is not cut off.

| Constant | Value | Used for |
|---|---|---|
| `REGISTER_TIMEOUT` | 20 s | Client → server REGISTER |
| `FETCH_TIMEOUT` | 20 s | Client → server FETCH |
| `SUBMIT_TIMEOUT` | 180 s | Client → server SUBMIT (covers relay + 10 s hold) |
| `SERVER_CONN_TIMEOUT` | 180 s | Server reading an incoming connection |
| `RELAY_SOCKET_TIMEOUT` | 120 s | Server → server RELAY |

The 10-second recipient-offline hold and the 10-second relay retry window are protocol behaviour
and are separate from these.

Attachments are limited to **10 MB** (enforced by the GUI).

---

## 4. Status-code demo — `demo_errors.py`

```powershell
python demo_errors.py
```

Starts two throw-away servers on **ports 5201 and 5202** (mailstores go in a temporary folder, not
the repo) and triggers, in order: 201, 404 (unlisted client's REGISTER refused), 404 (impostor
using a listed email from the wrong IP), 404 (FETCH without REGISTER), 400 (missing
header), 400 (no protocol token), 404 (unknown recipient),
480 (recipient not connected anywhere), 502 and 503. Takes about 30 seconds and exits by itself.
No arguments, no other programs needed; ports 5201/5202 must be free.

---

## 5. UDP experiment — `udp_receiver.py` / `udp_sender.py`

Sends a file over raw UDP with **no** acknowledgements or retransmission, to compare against
POXP's TCP transport. Start the **receiver first**.

```powershell
# On the receiving machine
python udp_receiver.py 6000 received_1mb.pdf 5
```

| Argument | Meaning | Default |
|---|---|---|
| `port` | UDP port to listen on | required |
| `output_file` | Where to write the reassembled file | required |
| `idle_timeout` | Seconds of silence before it stops and saves | `5` |

```powershell
# On the sending machine
python udp_sender.py 192.168.137.178 6000 poxp_r6_test_1mb.pdf
```

| Argument | Meaning | Default |
|---|---|---|
| `receiver_ip` | IP of the receiving machine | required |
| `port` | Receiver's UDP port | required |
| `file_path` | File to send | required |
| `chunk_size` | Bytes of file data per packet | `1400` (stays under a 1500-byte MTU) |

The receiver reports how many chunks were lost and compares the saved file size with the
original (**MATCH** or **MISMATCH**). To try it on one machine use `127.0.0.1` as `receiver_ip`.

---

## 6. Wireshark dissector — `poxp.lua`

1. In Wireshark: **Help → About Wireshark → Folders**, note the **Personal Lua Plugins** path
   (Windows usually `%APPDATA%\Wireshark\plugins\`).
2. Copy `poxp.lua` into that folder.
3. Restart Wireshark (or **Analyze → Reload Lua Plugins**).

TCP traffic on ports **5025, 5026, 5201, 5202** then shows as **POXP**. If you run a server on a
different port, add it at the bottom of `poxp.lua` (`tcp_port_table:add(<port>, poxp_proto)`).

Notes for captures:
- TCP splits each message into segments of at most **1460 bytes** of payload on a normal
  1500-byte-MTU network; the programs themselves just hand the whole message to the OS.
- Capture on a real network interface, not loopback (loopback's MTU is ~64 KB), and preferably on
  the **receiving** machine — a sender's NIC offloading can make Wireshark show segments larger
  than 1460 bytes.

---

<a id="faq-what-if-both-client-ports-are-5025"></a>
## FAQ: what if both client ports are 5025?

E.g. `python gui_app.py client_a@poxpmail.com 192.168.137.1 5025 5025`

- **Client on the same machine as its server** (server already running on 5025): the client
  registers successfully (`201 REGISTERED`), then the GUI fails to start with
  `[Errno 10048] ... only one usage of each socket address ... is normally permitted` and the
  program exits. The server keeps running normally. Pick another `web_port` such as 8000.
- **Client started first, then the server on the same machine:** the GUI works, but the server
  crashes on start-up with `PermissionError: [WinError 10013]`, because the GUI already owns 5025.
- **Client on a different machine from its server:** it works, because the GUI's 5025 is on the
  client's machine and the server's 5025 is on the server's machine. It's still clearer to keep
  the GUI on 8000.
