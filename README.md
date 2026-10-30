# Custom MITM proxy

A small Python HTTP/1.1 debugging proxy. Requests and responses for exact hostnames
in `TARGET_DOMAINS` in `main.py` are printed with headers and bodies. Other HTTP
traffic is forwarded without traffic logs; other HTTPS traffic uses an opaque
CONNECT tunnel. Host matching ignores case and trailing dots; add subdomains
explicitly.

## Run

With Python 3.14 and uv installed:

```sh
uv sync
uv run python main.py
```

The default listener is `127.0.0.1:8888`. Override targets or write a log file:

```sh
uv run python main.py --target example.com --target api.example.com --log-file traffic.log
```

Alternatively install the dependencies in `pyproject.toml` into your environment
and run `python main.py`. The implementation uses the standard library for proxy
networking and `cryptography` for certificates; it does not use mitmproxy.

## Interactive traffic inspector

Run in Windows Terminal, PowerShell, Command Prompt, or a Linux/macOS terminal:

```sh
uv sync
```

On Windows, this installs `windows-curses` automatically. Linux and macOS use
Python's built-in `curses` module. Start the inspector in an interactive terminal:

```sh
uv run python main.py --interactive --target example.com --log-file traffic.log
```

The live list pairs each request with its response, showing method, URL, status,
response size, and elapsed time. Pending requests and failures appear in the list.
Use Up/Down to select a flow and Enter to inspect request and response headers,
query arguments, URL-encoded form fields, and decoded bodies. JSON is indented.
Repeated arguments and headers are preserved. Use Up/Down or Page Up/Page Down to
scroll, Left/Right to pan long lines, Esc or `b` to return, and `q` to stop the proxy.

Only configured target domains are captured. The latest 500 flows are retained in
memory for this session. `--log-file` keeps the full plaintext traffic log while
the terminal shows the inspector. Without `--interactive`, logging works as before.
Multipart form bodies are shown as text rather than parsed fields.

## Trust the proxy CA

On first startup the proxy creates `.proxy-certs/ca.crt` and its private key.
Configure your HTTP client to trust that certificate. Older CA files without key
identifier extensions are upgraded automatically on startup; reimport the updated
`ca.crt` if you previously installed it in a browser or system trust store. The CA persists across
restarts, and leaf certificates are generated separately per hostname.

For curl (run after starting the proxy):

```sh
curl --proxy http://127.0.0.1:8888 --cacert .proxy-certs/ca.crt https://example.com/
curl --proxy http://127.0.0.1:8888 http://example.com/
```

For requests:

```python
import requests

response = requests.get(
    "https://example.com/",
    proxies={"http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888"},
    verify=".proxy-certs/ca.crt",
    timeout=30,
)
print(response.status_code)
```

A browser needs the CA imported into its trusted authorities and its HTTP/HTTPS
proxy set to the listener. Certificate-pinned clients may reject interception.
For a mix of intercepted and pass-through destinations, the client's trust store
must include both this CA and the usual public roots. Upstream server certificates
are always verified against platform roots plus Requests' public CA bundle.
If a private server or your network uses a corporate certificate issuer, supply
its trusted PEM CA bundle explicitly:

```sh
uv run python main.py --upstream-ca corporate-ca.pem
```

This option configures the proxy-to-server connection. `.proxy-certs/ca.crt`
configures trust for the separate client-to-proxy connection.

Keep the CA private key private. Logs contain full headers and bodies, including
credentials when present. Only trust this CA on clients you intend to debug.
`--cert-dir` selects another certificate directory; do not run multiple proxy
processes against the same directory during initial CA creation.

## Behavior and limits

- Supports HTTP and HTTP/1.1 over HTTPS, common methods, keep-alive, request
  bodies, chunked transfer framing, and concurrent connections.
- Logs complete messages atomically. Gzip and deflate bodies are decompressed
  for display, then decoded using the declared charset (UTF-8 by default).
  Forwarded body content stays unchanged; chunked messages are reframed with a
  Content-Length. Binary data may display replacement characters.
- Advertises gzip/deflate upstream to keep textual responses readable.
- Buffers complete requests/responses in memory. Large downloads consume memory;
  indefinite streams such as SSE are unsuitable. Idle/network timeout is 30 seconds.
- HTTP/2, HTTP/3, WebSockets, protocol upgrades, and transparent OS interception
  are not implemented. Clients must explicitly use the proxy.
- Default binding is local. There is no proxy authentication.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use local HTTP/TLS origins and verify plaintext logging, domain filtering,
chunked bodies, persistent HTTPS connections, CA persistence, opaque TLS tunnels,
and upstream certificate verification. No external sites are required.
