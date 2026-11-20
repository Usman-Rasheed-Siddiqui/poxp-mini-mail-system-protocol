"""
gui_app.py
Embedded FastAPI web interface running in the same Python process as ClientNode.
Features:
- Plain, flat layout with sidebar navigation (Inbox, Compose, Console, Exit)
- Expandable email viewer with attachment downloads (PDFs, images, docs up to 10MB)
- Safe template rendering using token replacements (immune to CSS/JS curly brace KeyError)
- Live activity console streaming socket events, TCP packets, and ACKs
- 10-second offline timeout alert with interactive Retry and Cancel prompts
- Client-side storage deletion and unread badge tracking

Pure HTML/CSS (system fonts, flat colors, no blur/gradients/animation) plus the
original small vanilla-JS toggle for expanding an email -- no frameworks, no
build step, nothing beyond what FastAPI already serves.
"""
import uvicorn
import os
import sys
import base64
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from client_node import ClientNode
from poxp_protocol import ATTACHMENT_START_TAG, ATTACHMENT_END_TAG

HTML_TEMPLATE_RAW = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>POXP Mail - __CLIENT_EMAIL__</title>
    <style>
        :root {
            --bg: #f6f7f9;
            --surface: #ffffff;
            --sidebar: #1a1d23;
            --sidebar-text: #9aa1ad;
            --sidebar-text-active: #ffffff;
            --border: #e3e5e9;
            --ink: #16181d;
            --muted: #6b7280;
            --accent: #2f6feb;
            --accent-ink: #ffffff;
            --danger: #d64545;
            --danger-bg: #fdecec;
            --success-bg: #e8f5ea;
            --success-ink: #1f7a37;
            --warn-bg: #fff6e0;
            --warn-ink: #8a6200;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            background: var(--bg);
            color: var(--ink);
            display: flex;
            overflow: hidden;
        }

        /* ---------- Sidebar ---------- */
        .sidebar {
            width: 220px;
            background: var(--sidebar);
            color: var(--sidebar-text);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 20px 14px;
            flex-shrink: 0;
        }
        .sidebar-brand {
            font-size: 15px;
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 22px;
            padding: 0 6px;
        }
        .nav-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 9px 12px;
            border-radius: 6px;
            color: var(--sidebar-text);
            text-decoration: none;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 2px;
            cursor: pointer;
        }
        .nav-item:hover { background: rgba(255,255,255,.06); color: var(--sidebar-text-active); }
        .nav-item.active { background: rgba(255,255,255,.1); color: var(--sidebar-text-active); }
        .badge {
            background: var(--accent);
            color: #ffffff;
            font-size: 11px;
            font-weight: 600;
            padding: 1px 7px;
            border-radius: 9px;
        }
        .sidebar-footer {
            border-top: 1px solid rgba(255,255,255,.1);
            padding-top: 12px;
        }
        .exit-btn {
            width: 100%;
            background: transparent;
            border: 1px solid rgba(255,255,255,.18);
            color: var(--sidebar-text);
            padding: 8px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: block;
        }
        .exit-btn:hover { background: rgba(255,255,255,.08); color: #fff; }

        /* ---------- Main ---------- */
        .main {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
            min-width: 0;
        }
        .topbar {
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            padding: 14px 28px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .topbar strong { font-weight: 600; }
        .node-chip {
            background: var(--bg);
            border: 1px solid var(--border);
            padding: 5px 11px;
            border-radius: 5px;
            font-size: 12px;
            color: var(--muted);
        }
        .node-chip strong { color: var(--ink); }
        .content-area {
            padding: 24px 28px;
            max-width: 1100px;
            margin: 0 auto;
            width: 100%;
        }

        /* ---------- Banners ---------- */
        .banner {
            padding: 12px 16px;
            border-radius: 6px;
            margin-bottom: 16px;
            font-size: 13px;
            border: 1px solid var(--border);
        }
        .banner-success { background: var(--success-bg); color: var(--success-ink); border-color: #c7e7cd; }
        .banner-error { background: var(--danger-bg); color: #9b2c2c; border-color: #f6cbcb; }
        .retry-prompt {
            background: var(--warn-bg);
            border: 1px solid #f3dca0;
            color: var(--warn-ink);
            padding: 14px 16px;
            border-radius: 6px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .btn-retry {
            background: #b8860b;
            color: white;
            border: none;
            padding: 7px 14px;
            border-radius: 5px;
            font-weight: 600;
            font-size: 12px;
            cursor: pointer;
            margin-right: 8px;
        }
        .btn-cancel {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--ink);
            padding: 7px 14px;
            border-radius: 5px;
            font-weight: 600;
            font-size: 12px;
            cursor: pointer;
        }

        /* ---------- Cards ---------- */
        .grid-layout {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
        }
        h2 { font-size: 14.5px; font-weight: 600; margin-bottom: 14px; color: var(--ink); }
        label { display: block; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); margin-top: 12px; margin-bottom: 4px; }
        input, select, textarea {
            width: 100%;
            padding: 8px 10px;
            border: 1px solid var(--border);
            border-radius: 5px;
            font-size: 13px;
            background: var(--surface);
            color: var(--ink);
            font-family: inherit;
        }
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: var(--accent);
        }
        .submit-btn {
            background: var(--accent);
            color: var(--accent-ink);
            border: none;
            padding: 10px 18px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 16px;
            width: 100%;
        }
        .submit-btn:hover { background: #2559c4; }

        /* ---------- Inbox list ---------- */
        .email-item {
            border: 1px solid var(--border);
            border-radius: 6px;
            margin-bottom: 8px;
            background: var(--surface);
        }
        .email-item:hover { border-color: #c9ccd2; }
        .email-header {
            padding: 11px 12px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .unread-indicator {
            width: 6px;
            height: 6px;
            background: var(--accent);
            border-radius: 50%;
            display: inline-block;
            margin-right: 7px;
        }
        .email-body {
            padding: 0 14px 14px 14px;
            border-top: 1px solid var(--border);
            margin-top: 2px;
            font-size: 13px;
            line-height: 1.55;
            color: var(--muted);
            display: none;
        }
        .email-body.open { display: block; padding-top: 12px; }
        .attachment-chip {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--accent);
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 12px;
            font-weight: 500;
            text-decoration: none;
            margin-top: 10px;
        }
        .attachment-chip:hover { border-color: var(--accent); }
        .delete-btn {
            background: transparent;
            color: var(--danger);
            border: 1px solid #efc6c6;
            padding: 4px 10px;
            border-radius: 5px;
            font-size: 11px;
            cursor: pointer;
            float: right;
            margin-top: 10px;
        }
        .delete-btn:hover { background: var(--danger-bg); }

        /* ---------- Console ---------- */
        .console-box {
            background: #15171b;
            color: #9ad1ff;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 11.5px;
            padding: 14px;
            border-radius: 6px;
            height: 210px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.5;
            margin-top: 16px;
        }

        @media (max-width: 900px) {
            body { overflow: auto; flex-direction: column; }
            .sidebar { flex-direction: row; width: auto; overflow-x: auto; }
            .grid-layout { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>

    <!-- Sidebar Navigation -->
    <div class="sidebar">
        <div>
            <div class="sidebar-brand">POXP Mail</div>
            <a class="nav-item active" href="/">
                <span>Inbox</span>
                <span class="badge">__UNREAD_COUNT__</span>
            </a>
            <a class="nav-item" href="#compose-box">
                <span>Compose</span>
            </a>
            <a class="nav-item" href="#console-box">
                <span>Socket Console</span>
            </a>
        </div>
        <div class="sidebar-footer">
            <form action="/shutdown" method="post">
                <button type="submit" class="exit-btn">Exit Client GUI</button>
            </form>
        </div>
    </div>

    <!-- Main Workspace -->
    <div class="main">
        <div class="topbar">
            <div>
                <strong>__CLIENT_EMAIL__</strong>
            </div>
            <div class="node-chip">
                Relay Server: <strong>__SERVER_IP__:__SERVER_PORT__</strong> &nbsp;&middot;&nbsp; Auto-Poll: <strong>5s</strong>
            </div>
        </div>

        <div class="content-area">
            __STATUS_BANNER__
            __RETRY_BANNER__

            <div class="grid-layout">
                <!-- Compose Card -->
                <div class="card" id="compose-box">
                    <h2>Compose New Email</h2>
                    <form action="/send" method="post" enctype="multipart/form-data">
                        <label>Recipient Email:</label>
                        <input type="email" name="receiver" placeholder="recipient@poxpmail.com" required>

                        <label>Subject:</label>
                        <input type="text" name="subject" placeholder="Project milestone" required>

                        <label>Priority (Thread Dispatch Priority):</label>
                        <select name="priority">
                            <option value="important">Important (High Priority)</option>
                            <option value="moderate" selected>Moderate (Normal)</option>
                            <option value="low">Low (Background)</option>
                        </select>

                        <label>Message Content:</label>
                        <textarea name="body" rows="5" placeholder="Type your message here..." required></textarea>

                        <label>Attachment (PDF / Files up to 10MB):</label>
                        <input type="file" name="attachment">

                        <button type="submit" class="submit-btn">Send via Raw TCP Socket</button>
                    </form>
                </div>

                <!-- Inbox Card -->
                <div class="card">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                        <h2>Client Inbox</h2>
                        <a href="/refresh" style="font-size:12px; color:var(--accent); text-decoration:none; font-weight:600;">Refresh Now</a>
                    </div>
                    <div style="max-height: 440px; overflow-y: auto;">
                        __INBOX_HTML__
                    </div>
                </div>
            </div>

            <!-- Socket Activity Console -->
            <div class="card" id="console-box" style="margin-top:20px;">
                <h2>Live Socket Activity Console (Raw TCP &amp; Status Codes)</h2>
                <div class="console-box">__CONSOLE_LOGS__</div>
            </div>
        </div>
    </div>

    <script>
        function toggleEmail(id) {
            const body = document.getElementById('body-' + id);
            if (body) {
                body.classList.toggle('open');
                fetch('/mark_read/' + id, { method: 'POST' });
            }
        }
    </script>
</body>
</html>
"""

def create_app(client_node: ClientNode):
    app = FastAPI(title=f"POXP Mail Client - {client_node.client_email}")
    last_status = {"code": "", "text": ""}

    @app.get("/", response_class=HTMLResponse)
    def index():
        inbox_items = client_node.storage.client_get_inbox()
        unread_count = sum(1 for e in inbox_items if not e.get("is_read", False))

        inbox_html = ""
        if not inbox_items:
            inbox_html = "<p style='color:#9aa1ad; font-style:italic; padding:10px;'>No emails in client storage. Background daemon auto-polls every 5s.</p>"
        else:
            for item in reversed(inbox_items):
                headers = item.get("headers", {})
                msg_id = headers.get("Identifier", "unknown")
                sender = headers.get("Sender email", "Unknown")
                subject = headers.get("Subject", "(No Subject)")
                date = headers.get("Date", "")
                priority = headers.get("Priority", "normal")
                is_read = item.get("is_read", False)
                body = item.get("body", "")

                indicator = "" if is_read else "<span class='unread-indicator'></span>"
                att_html = ""
                if "Attachment-Name" in headers:
                    att_name = headers["Attachment-Name"]
                    att_html = f"<br><a href='/download/{msg_id}' class='attachment-chip'>Download {att_name}</a>"

                inbox_html += f"""
                <div class="email-item">
                    <div class="email-header" onclick="toggleEmail('{msg_id}')">
                        <div>
                            {indicator}<strong>{sender}</strong>
                            <div style="font-size:12px; color:#6b7280; margin-top:2px;">{subject}</div>
                        </div>
                        <div style="text-align:right;">
                            <span style="font-size:10px; background:#f6f7f9; border:1px solid #e3e5e9; padding:2px 7px; border-radius:4px; color:#6b7280;">{priority}</span>
                            <div style="font-size:10px; color:#9aa1ad; margin-top:4px;">{date[:16]}</div>
                        </div>
                    </div>
                    <div class="email-body" id="body-{msg_id}">
                        <div style="white-space: pre-wrap;">{body}</div>
                        {att_html}
                        <form action="/delete/{msg_id}" method="post">
                            <button type="submit" class="delete-btn">Delete from Storage</button>
                        </form>
                    </div>
                </div>
                """

        status_banner = ""
        if last_status["code"]:
            cls = "banner-success" if last_status["code"].startswith("200") or last_status["code"].startswith("201") else "banner-error"
            status_banner = f"<div class='banner {cls}'><strong>Action Result:</strong> {last_status['code']} — {last_status['text']}</div>"

        retry_banner = ""
        if client_node.pending_retry:
            rec = client_node.pending_retry.get("receiver")
            err = client_node.pending_retry.get("error_msg")
            retry_banner = f"""
            <div class="retry-prompt">
                <div>
                    <strong>Transmission Paused:</strong> Target {rec} was offline for &gt;10s ({err}).
                    <br><small>Would you like to retry sending now or cancel?</small>
                </div>
                <div>
                    <form action="/retry" method="post" style="display:inline;">
                        <button type="submit" class="btn-retry">Retry Sending</button>
                    </form>
                    <form action="/cancel" method="post" style="display:inline;">
                        <button type="submit" class="btn-cancel">Cancel</button>
                    </form>
                </div>
            </div>
            """

        with client_node.log_lock:
            console_text = "\n".join(client_node.console_logs[-25:])

        # Safe token replacement: ZERO KeyError possible from CSS or JS!
        html = HTML_TEMPLATE_RAW
        html = html.replace("__CLIENT_EMAIL__", client_node.client_email)
        html = html.replace("__SERVER_IP__", client_node.server_ip)
        html = html.replace("__SERVER_PORT__", str(client_node.server_port))
        html = html.replace("__UNREAD_COUNT__", str(unread_count))
        html = html.replace("__STATUS_BANNER__", status_banner)
        html = html.replace("__RETRY_BANNER__", retry_banner)
        html = html.replace("__INBOX_HTML__", inbox_html)
        html = html.replace("__CONSOLE_LOGS__", console_text)

        return html

    @app.get("/refresh")
    def refresh():
        client_node.fetch_inbox()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/send")
    async def send(receiver: str = Form(...), subject: str = Form(...), priority: str = Form("moderate"), body: str = Form(...), attachment: UploadFile = File(None)):
        att_filename = None
        att_bytes = None
        if attachment and attachment.filename:
            att_filename = attachment.filename
            att_bytes = await attachment.read()
            if len(att_bytes) > 10 * 1024 * 1024:
                last_status["code"] = "400 BAD_REQUEST"
                last_status["text"] = "Attachment exceeds maximum allowed limit of 10 MB."
                return RedirectResponse(url="/", status_code=303)

        ack = client_node.send_email(
            receiver_email=receiver,
            subject=subject,
            body=body,
            priority=priority,
            attachment_filename=att_filename,
            attachment_bytes=att_bytes
        )
        if ack:
            last_status["code"] = f"{ack.get('method_or_code')} {ack.get('protocol_or_status')}"
            last_status["text"] = f"{ack.get('body', '')}"
        else:
            last_status["code"] = "Error"
            last_status["text"] = "No response from socket server"
        return RedirectResponse(url="/", status_code=303)

    @app.post("/retry")
    def retry():
        ack = client_node.retry_last_failed()
        if ack:
            last_status["code"] = f"{ack.get('method_or_code')} {ack.get('protocol_or_status')}"
            last_status["text"] = f"{ack.get('body', '')}"
        return RedirectResponse(url="/", status_code=303)

    @app.post("/cancel")
    def cancel():
        client_node.cancel_last_failed()
        last_status["code"] = "CANCELLED"
        last_status["text"] = "Pending transmission cancelled by user."
        return RedirectResponse(url="/", status_code=303)

    @app.post("/mark_read/{identifier}")
    def mark_read(identifier: str):
        client_node.storage.client_mark_as_read(identifier)
        return {"status": "ok"}

    @app.post("/delete/{identifier}")
    def delete_email(identifier: str):
        client_node.storage.client_delete_email(identifier)
        client_node.log(f"[CLIENT STORAGE] Deleted email ID: {identifier} from local storage.")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/download/{identifier}")
    def download_attachment(identifier: str):
        inbox = client_node.storage.client_get_inbox()
        for item in inbox:
            if item.get("headers", {}).get("Identifier") == identifier:
                filename = item.get("headers", {}).get("Attachment-Name", "download.bin")
                # Direct attachment base64 field check
                if item.get("attachment_b64"):
                    file_bytes = base64.b64decode(item["attachment_b64"])
                    return Response(
                        content=file_bytes,
                        media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                    )
                # Raw body envelope check
                raw_body = item.get("raw_body") or item.get("body", "")
                if ATTACHMENT_START_TAG in raw_body:
                    _, post = raw_body.split(ATTACHMENT_START_TAG, 1)
                    b64_part, _ = post.split(ATTACHMENT_END_TAG, 1)
                    file_bytes = base64.b64decode(b64_part.strip())
                    return Response(
                        content=file_bytes,
                        media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
                    )
        return Response("Attachment not found", status_code=404)

    @app.post("/shutdown")
    def shutdown():
        client_node.shutdown()
        def _exit():
            import time
            time.sleep(0.5)
            os._exit(0)
        import threading
        threading.Thread(target=_exit).start()
        return HTMLResponse("<h3>POXP Client Node has been shut down successfully. You may close this window.</h3>")

    return app

if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "client_a@poxpmail.com"
    srv_ip = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    srv_port = int(sys.argv[3]) if len(sys.argv) > 3 else 5025
    web_port = int(sys.argv[4]) if len(sys.argv) > 4 else 8000

    node = ClientNode(client_email=email, server_ip=srv_ip, server_port=srv_port)
    try:
        reg_ack = node.register_dns()
        print(f"[{email}] Auto-Registration response: {reg_ack}")
    except Exception as e:
        print(f"[{email}] Note: Registration deferred: {e}")

    app = create_app(node)
    uvicorn.run(app, host="0.0.0.0", port=web_port)