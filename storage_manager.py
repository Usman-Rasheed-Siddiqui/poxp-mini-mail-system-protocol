"""
storage_manager.py
Thread-safe mailbox storage manager for server queues and client persistent stores.
Supports auto-purge upon retrieval and client-side unread/read tracking and deletion.
"""
import os
import json
import threading

from poxp_protocol import PRIORITY_WEIGHT

class StorageManager:
    def __init__(self, storage_file="mailbox_store.json"):
        self.storage_file = storage_file
        self.lock = threading.Lock()
        self._init_store()

    def _init_store(self):
        with self.lock:
            if not os.path.exists(self.storage_file):
                with open(self.storage_file, "w", encoding="utf-8") as f:
                    json.dump({"inboxes": {}, "sent": {}}, f, indent=2)

    def _read_data(self):
        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"inboxes": {}, "sent": {}}

    def _write_data(self, data):
        with open(self.storage_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # Server-Side Methods
    def save_incoming_email(self, recipient_email, email_record):
        with self.lock:
            data = self._read_data()
            inbox = data["inboxes"].setdefault(recipient_email, [])
            inbox.append(email_record)
            self._write_data(data)

    def fetch_inbox(self, recipient_email, clear_fetched=True):
        """Retrieves and automatically purges retrieved messages from server store.

        Messages are returned ordered by Priority header (important > moderate >
        low), with original arrival order preserved among messages of equal
        priority (Python's sort is stable, so this gives FIFO tie-breaking).
        This is the entire "priority" mechanism: no queue, no worker threads —
        just an ordering step applied at read time.
        """
        with self.lock:
            data = self._read_data()
            emails = data["inboxes"].get(recipient_email, [])
            emails = sorted(
                emails,
                key=lambda e: PRIORITY_WEIGHT.get(
                    e.get("headers", {}).get("Priority", "moderate"), 2
                ),
            )
            if clear_fetched and emails:
                data["inboxes"][recipient_email] = []
                self._write_data(data)
            return emails

    def has_email(self, recipient_email, identifier):
        """True if the message with this Identifier is still waiting on the server."""
        with self.lock:
            data = self._read_data()
            return any(
                e.get("headers", {}).get("Identifier") == identifier
                for e in data["inboxes"].get(recipient_email, [])
            )

    def remove_email(self, recipient_email, identifier):
        """Deletes ONLY the message with this Identifier (other mail is untouched).
        Returns True if it was still there (i.e. the recipient never fetched it)."""
        with self.lock:
            data = self._read_data()
            inbox = data["inboxes"].get(recipient_email, [])
            kept = [e for e in inbox if e.get("headers", {}).get("Identifier") != identifier]
            if len(kept) == len(inbox):
                return False
            data["inboxes"][recipient_email] = kept
            self._write_data(data)
            return True

    # Client-Side Persistent Store Methods
    def client_save_email(self, email_record):
        """Saves a fetched email into client-side store with unread status."""
        with self.lock:
            data = self._read_data()
            inbox = data["inboxes"].setdefault("local", [])
            # Avoid duplicate insertion by Identifier
            new_id = email_record.get("headers", {}).get("Identifier")
            exists = any(e.get("headers", {}).get("Identifier") == new_id for e in inbox if new_id)
            if not exists:
                email_record["is_read"] = False
                inbox.append(email_record)
                self._write_data(data)

    def client_get_inbox(self):
        with self.lock:
            data = self._read_data()
            return data["inboxes"].get("local", [])

    def client_mark_as_read(self, identifier):
        with self.lock:
            data = self._read_data()
            inbox = data["inboxes"].get("local", [])
            for item in inbox:
                if item.get("headers", {}).get("Identifier") == identifier:
                    item["is_read"] = True
                    break
            self._write_data(data)

    def client_delete_email(self, identifier):
        with self.lock:
            data = self._read_data()
            inbox = data["inboxes"].get("local", [])
            data["inboxes"]["local"] = [
                item for item in inbox
                if item.get("headers", {}).get("Identifier") != identifier
            ]
            self._write_data(data)