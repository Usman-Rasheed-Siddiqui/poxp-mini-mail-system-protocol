"""
udp_receiver.py
Receives a file sent by udp_sender.py over raw UDP -- no acks sent back,
no requests for retransmission. Whatever arrives, arrives; whatever is
lost, is simply gone. Reassembles what it received (in sequence order,
skipping any missing chunk numbers) and reports exactly how much was
actually lost, so you can compare the reconstructed file against the
original.

Usage:
    python3 udp_receiver.py <port> <output_file> [idle_timeout_seconds]

Example:
    python3 udp_receiver.py 6000 received_1mb.pdf 5
"""
import socket
import sys
import struct

HEADER_SEQ = 0xFFFFFFFF


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 udp_receiver.py <port> <output_file> [idle_timeout_seconds]")
        sys.exit(1)

    port = int(sys.argv[1])
    output_file = sys.argv[2]
    idle_timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(idle_timeout)

    print(f"[LISTENING] UDP port {port} (will stop after {idle_timeout}s of silence)")

    chunks = {}
    total_chunks = None
    total_size = None

    while True:
        try:
            packet, addr = sock.recvfrom(65535)
        except socket.timeout:
            print(f"[TIMEOUT] No packets for {idle_timeout}s -- assuming transfer finished (or sender gave up, "
                  f"which it never does, since this sender has no concept of giving up).")
            break

        seq, count = struct.unpack("!II", packet[:8])
        if seq == HEADER_SEQ:
            total_chunks = count
            total_size = struct.unpack("!I", packet[8:12])[0]
            print(f"[HEADER] Expecting {total_chunks} chunks, {total_size} bytes total")
            continue

        if seq not in chunks:
            chunks[seq] = packet[8:]
        if total_chunks is None:
            total_chunks = count

    received = len(chunks)
    expected = total_chunks if total_chunks is not None else max(chunks.keys(), default=0) + 1
    lost = expected - received
    loss_pct = (lost / expected * 100) if expected else 0.0

    print(f"\n[RESULT] Received {received}/{expected} chunks ({loss_pct:.2f}% lost, never retransmitted)")

    with open(output_file, "wb") as f:
        for i in range(expected):
            if i in chunks:
                f.write(chunks[i])
            # missing chunks are simply skipped -- the file comes out
            # shorter than the original and corrupted from that point
            # structurally, exactly like a real lost-attachment-byte scenario

    import os
    out_size = os.path.getsize(output_file)
    print(f"[SAVED] {output_file}: {out_size} bytes")
    if total_size is not None:
        print(f"[COMPARE] Original was {total_size} bytes -> "
              f"{'MATCH (no loss occurred)' if out_size == total_size else f'MISMATCH: missing {total_size - out_size} bytes -- file is corrupted/truncated'}")


if __name__ == "__main__":
    main()
