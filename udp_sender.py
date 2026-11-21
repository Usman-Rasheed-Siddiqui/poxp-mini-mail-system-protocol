"""
udp_sender.py
Sends a file over raw UDP with NO reliability layer -- no acknowledgments,
no retransmission, no ordering guarantee beyond what arrives. This is
deliberately "POXP if it had been built on UDP instead of TCP" -- the
opposite choice from R4, with nothing added to compensate for loss.

Each packet: [4-byte seq number][4-byte total chunk count][chunk bytes]
Packet seq=0xFFFFFFFF is a special "header" packet announcing total
chunk count + original file size, sent first (and repeated 3x in case
it itself gets lost, since there's no ack to know if it arrived).

Usage:
    python3 udp_sender.py <receiver_ip> <port> <file_path> [chunk_size]

Example:
    python3 udp_sender.py 192.168.137.178 6000 poxp_r6_test_1mb.pdf
"""
import socket
import sys
import struct
import os
import time

HEADER_SEQ = 0xFFFFFFFF


def main():
    if len(sys.argv) < 4:
        print("Usage: python3 udp_sender.py <receiver_ip> <port> <file_path> [chunk_size]")
        sys.exit(1)

    receiver_ip = sys.argv[1]
    port = int(sys.argv[2])
    file_path = sys.argv[3]
    chunk_size = int(sys.argv[4]) if len(sys.argv) > 4 else 1400  # stays under typical MTU

    with open(file_path, "rb") as f:
        data = f.read()

    total_size = len(data)
    chunks = [data[i:i + chunk_size] for i in range(0, total_size, chunk_size)]
    total_chunks = len(chunks)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"[FILE] {file_path}: {total_size} bytes in {total_chunks} chunks of {chunk_size} bytes")
    print(f"[SENDING] -> {receiver_ip}:{port} (no ACKs, no retransmission -- pure UDP)")

    # Send the header 3x since there's no way to confirm the receiver got it
    header_packet = struct.pack("!II", HEADER_SEQ, total_chunks) + struct.pack("!I", total_size)
    for _ in range(3):
        sock.sendto(header_packet, (receiver_ip, port))

    start = time.perf_counter()
    for i, chunk in enumerate(chunks):
        packet = struct.pack("!II", i, total_chunks) + chunk
        sock.sendto(packet, (receiver_ip, port))
    elapsed = time.perf_counter() - start

    print(f"[DONE] Sent all {total_chunks} chunks in {elapsed:.4f} s "
          f"({(total_size*8)/elapsed/1_000_000:.3f} Mbps nominal -- this is just how fast "
          f"we fired packets, NOT a guarantee any of them arrived)")
    print("[NOTE] Check udp_receiver.py's output on the other end to see how much actually got there.")


if __name__ == "__main__":
    main()
