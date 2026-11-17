"""
gui_app.py
Embedded FastAPI web interface running in the same Python process as ClientNode.
Features:
- Modern responsive layout with sidebar navigation (Inbox, Compose, Console, Exit)
- Expandable email viewer with attachment downloads (PDFs, images, docs up to 10MB)
- Safe template rendering using token replacements (immune to CSS/JS curly brace KeyError)
- Live activity console streaming socket events, TCP packets, and ACKs
- 10-second offline timeout alert with interactive Retry and Cancel prompts
- Client-side storage deletion and unread badge tracking
"""
import uvicorn
import os
import sys
import base64
from fastapi import FastAPI, Request, Form, UploadFile, File
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
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --bg-main: #f8fafc;
            --surface: #ffffff;
            --sidebar-bg: #0f172a;
            --sidebar-text: #94a3b8;
            --sidebar-active: #ffffff;
            --border: #e2e8f0;
            --text-dark: #0f172a;
            --text-muted: #64748b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg-main);
            color: var(--text-dark);
            height: 100vh;
            display: flex;
            overflow: hidden;
        }
        /* Sidebar */
        .sidebar {
            width: 260px;
            background: var(--sidebar-bg);
            color: var(--sidebar-text);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 24px 16px;
            flex-shrink: 0;
        }
        .sidebar-brand {
            font-size: 18px;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 28px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .nav-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 16px;
            border-radius: 8px;
            color: var(--sidebar-text);
            text-decoration: none;
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 6px;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        .nav-item:hover, .nav-item.active {
            background: #1e293b;
            color: var(--sidebar-active);
        }
        .badge {
            background: var(--primary);
            color: white;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 12px;
        }
        .sidebar-footer {
            border-top: 1px solid #1e293b;
            padding-top: 16px;
        }
        .exit-btn {
            width: 100%;
            background: #ef4444;
            color: white;
            border: none;
            padding: 10px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: block;
        }
        .exit-btn:hover { background: #dc2626; }

        /* Main Content */
        .main {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
        }
        .topbar {
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            padding: 16px 32px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .node-chip {
            background: #f1f5f9;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 13px;
            color: var(--text-muted);
        }
        .content-area {
            padding: 28px 32px;
            max-width: 1100px;
            margin: 0 auto;
            width: 100%;
        }

        /* Banner & Prompts */
        .banner {
            padding: 14px 18px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .banner-success { background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }
        .banner-error { background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
        .retry-prompt {
            background: #fffbeb;
            border: 1px solid #fde68a;
            color: #92400e;
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .btn-retry {
            background: #d97706;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 13px;
            cursor: pointer;
            margin-right: 8px;
        }
        .btn-cancel {
            background: #64748b;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 13px;
            cursor: pointer;
        }

        /* Two Column View */
        .grid-layout {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        h2 { font-size: 17px; font-weight: 700; margin-bottom: 16px; color: var(--text-dark); }
        label { display: block; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-top: 14px; margin-bottom: 4px; }
        input, select, textarea {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 14px;
            background: #f8fafc;
        }
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: var(--primary);
            background: white;
        }
        .submit-btn {
            background: var(--primary);
            color: white;
            border: none;
            padding: 12px 20px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 18px;
            width: 100%;
        }
        .submit-btn:hover { background: var(--primary-hover); }

        /* Inbox List & Expandable */
        .email-item {
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 12px;
            background: white;
            transition: all 0.15s;
            overflow: hidden;
        }
        .email-header {
            padding: 14px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: #f8fafc;
        }
        .email-header:hover { background: #f1f5f9; }
        .unread-indicator {
            width: 8px;
            height: 8px;
            background: var(--primary);
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
        }
        .email-body {
            padding: 16px;
            border-top: 1px solid var(--border);
            font-size: 14px;
            line-height: 1.5;
            color: #334155;
            display: none;
        }
        .email-body.open { display: block; }
        .attachment-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #e0e7ff;
            color: #3730a3;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            text-decoration: none;
            margin-top: 12px;
        }
        .attachment-chip:hover { background: #c7d2fe; }
        .delete-btn {
            background: transparent;
            color: #ef4444;
            border: 1px solid #fecaca;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            cursor: pointer;
            float: right;
            margin-top: 12px;
        }
        .delete-btn:hover { background: #fee2e2; }

        /* Console Section */
        .console-box {
            background: #0f172a;
            color: #38bdf8;
            font-family: monospace;
            font-size: 12px;
            padding: 16px;
            border-radius: 8px;
            height: 220px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.4;
            margin-top: 24px;
        }
    </style>
</head>
<body>

    <!-- Sidebar Navigation -->
    <div class="sidebar">
        <div>
            <div class="sidebar-brand">
                <span>📬</span> POXP Mail Node
            </div>
            <a class="nav-item active" href="/">
                <span>📥 Inbox</span>
                <span class="badge">__UNREAD_COUNT__</span>
            </a>
            <a class="nav-item" href="#compose-box">
                <span>✍️ Compose</span>
            </a>
            <a class="nav-item" href="#console-box">
                <span>🖥️ Socket Console</span>
            </a>
        </div>
        <div class="sidebar-footer">
            <form action="/shutdown" method="post">
                <button type="submit" class="exit-btn">🚪 Exit Client GUI</button>
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
                Relay Server: <strong>__SERVER_IP__:__SERVER_PORT__</strong> | Auto-Poll: <strong>5s</strong>
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
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                        <h2>Client Inbox</h2>
                        <a href="/refresh" style="font-size:12px; color:var(--primary); text-decoration:none; font-weight:600;">🔄 Refresh Now</a>
                    </div>
                    <div style="max-height: 440px; overflow-y: auto;">
                        __INBOX_HTML__
                    </div>
                </div>
            </div>

            <!-- Socket Activity Console -->
            <div class="card" id="console-box" style="margin-top:24px;">
                <h2>Live Socket Activity Console (Raw TCP & Status Codes)</h2>
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
            inbox_html = "<p style='color:#94a3b8; font-style:italic; padding:12px;'>No emails in client storage. Background daemon auto-polls every 5s.</p>"
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
                    att_html = f"<br><a href='/download/{msg_id}' class='attachment-chip'>📎 Download {att_name}</a>"

                inbox_html += f"""
                <div class="email-item">
                    <div class="email-header" onclick="toggleEmail('{msg_id}')">
                        <div>
                            {indicator}<strong>{sender}</strong>
                            <div style="font-size:13px; color:#334155; margin-top:2px;">{subject}</div>
                        </div>
                        <div style="text-align:right;">
                            <span style="font-size:11px; background:#e2e8f0; padding:2px 6px; border-radius:4px;">{priority}</span>
                            <div style="font-size:11px; color:#94a3b8; margin-top:4px;">{date[:16]}</div>
                        </div>
                    </div>
                    <div class="email-body" id="body-{msg_id}">
                        <div style="white-space: pre-wrap;">{body}</div>
                        {att_html}
                        <form action="/delete/{msg_id}" method="post">
                            <button type="submit" class="delete-btn">🗑 Delete from Storage</button>
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
                    <strong>⚠️ Transmission Paused:</strong> Target {rec} was offline for >10s ({err}).
                    <br><small>Would you like to retry sending now or cancel?</small>
                </div>
                <div>
                    <form action="/retry" method="post" style="display:inline;">
                        <button type="submit" class="btn-retry">🔄 Retry Sending</button>
                    </form>
                    <form action="/cancel" method="post" style="display:inline;">
                        <button type="submit" class="btn-cancel">✖ Cancel</button>
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
