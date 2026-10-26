import socket


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

    hostname = url.split(":")[0]  # hostname

    if hostname in TARGET_DOMAINS:
        print("Found target")

    else:
        print("Pass through")
        

    # Client expects this response irrespective of whether request is intercepted or passed through
    client_socket.send(
        b"HTTP/1.1 200 Connection Established\r\n\r\n",
    )
    client_socket.close()
