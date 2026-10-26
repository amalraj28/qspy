import socket
import threading


def forward_bytes(
    source: socket.socket,
    destination: socket.socket,
):
    try:
        while True:
            data = source.recv(4096)
            if not data:
                break

            destination.sendall(data)
    except:
        pass


TARGET_DOMAINS = {
    "example.com",
}

server_socket = socket.socket(
    socket.AF_INET,  # IPv4 addressing
    socket.SOCK_STREAM,  # TCP Protocol
)

server_socket.bind(("127.0.0.1", 8888))

server_socket.listen()

while True:
    client_socket, client_addr = server_socket.accept()

    data = client_socket.recv(4096)
    decoded_data = data.decode()
    print(decoded_data)

    # First line is in the form --> CONNECT hostname:port HTTP/1.1\r\n
    first_line = decoded_data.split("\n")[0]
    url = first_line.split(" ")[1]  # hostname:port

    hostname, port = url.split(":")  # (hostname, port)

    if hostname in TARGET_DOMAINS:
        print("Found target")
        client_socket.send(
            b"HTTP/1.1 200 Connection Established\r\n\r\n",
        )

    else:
        print("Pass through")
        destination_socket = socket.create_connection((hostname, port))

        client_socket.send(
            b"HTTP/1.1 200 Connection Established\r\n\r\n",
        )

        # Spin up and start two threads for bidirectional communication
        request_thread = threading.Thread(
            target=forward_bytes,
            args=(
                client_socket,
                destination_socket,
            ),
            daemon=True,  # daemon thread, tied to the main script
        )

        response_thread = threading.Thread(
            target=forward_bytes,
            args=(
                destination_socket,
                client_socket,
            ),
            daemon=True,  # daemon thread, tied to the main script
        )

        request_thread.start()
        response_thread.start()
