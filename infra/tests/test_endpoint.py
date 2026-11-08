#!/usr/bin/env python3
"""Тестовый endpoint для acceptance M2c (raw-socket: http.server на macOS+vz кидает hang-up
на loopback-подключения; raw-сокет доказанно работает). Принимает POST, сохраняет payload."""
import contextlib
import socket

LAST = "/tmp/m2c-last-payload.json"


def handle(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1].strip())
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    with open(LAST, "wb") as f:
        f.write(rest)
    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
    conn.close()


def main():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 8999))
    s.listen(10)
    while True:
        conn, _ = s.accept()
        with contextlib.suppress(Exception):
            conn.settimeout(10)  # защитное: зависшее соединение не блокирует accept-цикл
            handle(conn)
            conn.close()


if __name__ == "__main__":
    main()
