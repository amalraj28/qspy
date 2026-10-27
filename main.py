import socket
import threading
from generate_cert import generate_cert_files
import ssl


def forward_bytes(
    source: socket.socket,
    destination: socket.socket,
    label: str | None = None,
):
    try:
        while True:
            data = source.recv(4096)
            if not data:
                break

            if label:
                print(f"[{label}] = {data.decode("utf-8", errors="ignore")}")

            destination.sendall(data)
    except:
        pass


def handle_client_connection(client_socket: socket.socket):
    is_intercept = False
    try:
        data = client_socket.recv(4096)
        if not data:
            return

        decoded_data = data.decode(
            encoding="utf-8",
            errors="ignore",
        )
        print(decoded_data)

        # First line is in the form --> CONNECT hostname:port HTTP/1.1\r\n
        first_line = decoded_data.split("\n")[0]
        url = first_line.split(" ")[1]  # hostname:port

        hostname, port = url.split(":")  # (hostname, port)
        port = int(port)

        if hostname in TARGET_DOMAINS:
            is_intercept = True
            print("Found target")
            client_socket.send(
                b"HTTP/1.1 200 Connection Established\r\n\r\n",
            )
            # Always send the above acknowledgment first, since client is
            # waiting for this after sending CONNECT request

            generate_cert_files(hostname=hostname)
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            context.load_cert_chain(
                certfile="tmp.crt",
                keyfile="tmp.key",
            )

            tls_client_socket = context.wrap_socket(
                client_socket,
                server_side=True,
            )

            remote_raw = socket.create_connection((hostname, int(port)))
            server_context = ssl._create_unverified_context()
            tls_remote_socket = server_context.wrap_socket(
                remote_raw,
                server_hostname=hostname,
            )

            t1 = threading.Thread(
                target=forward_bytes,
                args=(
                    tls_client_socket,
                    tls_remote_socket,
                    "API_REQUEST",
                ),
                daemon=True,
            )

            t2 = threading.Thread(
                target=forward_bytes,
                args=(
                    tls_remote_socket,
                    tls_client_socket,
                    "API_RESPONSE",
                ),
                daemon=True,
            )

            t1.start()
            t2.start()

            # plaintext_request = tls_client_socket.recv(4096)
            # print(f"Logged message : {plaintext_request.decode(errors="ignore")}")
            # tls_remote_socket.sendall(plaintext_request)

            # plaintext_response = tls_remote_socket.recv(4096)
            # print(f"Logged response : {plaintext_response.decode(errors="ignore")}")

            # tls_client_socket.sendall(plaintext_response)

            # tls_client_socket.close()
            # tls_remote_socket.close()

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

    except Exception as e:
        print(f"Error handling connection: {e}")

    finally:
        if is_intercept:
            try:
                client_socket.close()
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
server_socket.settimeout(1.0)
print("MITM Proxy active on 127.0.0.1:8888...")


try:
    while True:
        try:
            client_socket, client_addr = server_socket.accept()

        except socket.timeout:
            continue

        worker = threading.Thread(
            target=handle_client_connection,
            args=(client_socket,),
            daemon=True,
        )

        worker.start()


except KeyboardInterrupt:
    print("\nShutting down proxy server!")

finally:
    server_socket.close()
