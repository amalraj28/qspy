# Custom MITM Proxy for Quantum Job Monitoring

A lightweight Python HTTP/1.1 man-in-the-middle (MITM) proxy for monitoring Quantum job submissions and results.

The proxy intercepts HTTPS traffic for configured target domains, extracts quantum job metadata, and forwards relevant information to a central monitoring server (C2). Traffic to non-target HTTPS domains is forwarded through opaque CONNECT tunnels without inspecting its contents.

The implementation uses Python's standard library for proxy networking, `cryptography` for TLS certificate generation, and `PyJWT` for extracting identity claims. It does not depend on mitmproxy.

## Features

- Selective HTTP/HTTPS interception based on configured domains.
- Transparent forwarding of non-target HTTPS traffic through CONNECT tunnels.
- Detection of Quantum job submission requests and responses.
- Extraction of job IDs, backend names, shot counts, and available user identity information.
- Detection and capture of Quantum job results.
- Forwarding of captured job metadata and results to a C2 monitoring server.
- Conditional gzip and deflate decompression for response inspection.
- Preservation of original response bytes when forwarding traffic to clients.
- Automatic generation of a local certificate authority and per-host TLS certificates.
- Optional plaintext traffic logging.
- Concurrent request handling using a threaded HTTP server.

## Architecture

The system consists of three main components:

1. **Client:** A quantum application or Qiskit program configured to use the proxy.
2. **Custom MITM Proxy:** Intercepts configured domains, monitors quantum jobs, and forwards traffic to its destination.
3. **C2 Monitoring Server:** Receives captured job information through an HTTP API and makes it available to the monitoring dashboard.

```text
                  Quantum Client
                        |
                        v
                 Custom MITM Proxy
                        |
            +-----------+-----------+
            |                       |
      Target Domains          Other Domains
            |                       |
            v                       v
      Inspect Traffic        Forward Traffic
            |                 Without Inspection
            |
      Extract Job Data
            |
            v
      C2 Monitoring Server
            |
            v
       Web Dashboard
```

The proxy forwards intercepted requests to the original upstream server. Monitoring is performed alongside normal request processing.

## Installation

### Requirements

- Python 3.14
- `uv` or another Python package manager
- Dependencies declared in `pyproject.toml`
- A running C2 server for centralized monitoring

Install dependencies:

```bash
uv sync
```

Alternatively, install the required Python dependencies into your environment using your preferred package manager.

## Configuration

The proxy configuration is defined near the beginning of `custom_proxy.py`.

### Target domains

The proxy includes a list of domains to monitor. Any traffic to the listed domains are intercepted, while others are passed through. Domain matching is case-insensitive and supports subdomains of configured domains.

You can override the proxy's interception targets using the `--target` argument.

### C2 server configuration

The proxy reads its monitoring server configuration from environment variables:

| Variable | Default | Description |
|---|---|---|
| `C2_SERVER_IP` | `127.0.0.1` | C2 server hostname or IP address |
| `C2_SERVER_PORT` | `5000` | C2 server port |

The monitoring endpoint is:

```text
http://<C2_SERVER_IP>:<C2_SERVER_PORT>/api/log
```

On startup, the proxy attempts to contact the C2 server's `/health` endpoint and reports whether it is reachable.

If the C2 server is unavailable, the proxy can still forward traffic, but captured monitoring data may fail to reach the dashboard.

## Running the Proxy

Start the proxy using:

```bash
uv run python custom_proxy.py
```

The default listener is:

```text
127.0.0.1:8888
```

To use a different port:

```bash
uv run python custom_proxy.py --port 8080
```

To specify interception targets:

```bash
uv run python custom_proxy.py \
  --target custom_domain \
```

To write proxy logs to a file:

```bash
uv run python custom_proxy.py --log-file traffic.log
```

The available command-line options are:

| Option | Description |
|---|---|
| `--host` | Proxy listening address |
| `--port` | Proxy listening port |
| `--target` | Domain to intercept; repeatable |
| `--cert-dir` | Directory for proxy CA certificates |
| `--upstream-ca` | Additional PEM CA bundle for upstream TLS verification |
| `--log-file` | Save plaintext traffic logs to a file |

## Trusting the Proxy Certificate

The proxy generates a local certificate authority (CA) when its certificate directory is initialized.

By default, the certificate directory is:

```text
~/.custom-proxy/
```

It contains the CA certificate and private key.

Clients must trust the generated CA certificate to establish intercepted HTTPS connections.

### Upstream certificate verification

The proxy verifies upstream TLS certificates using trusted CA roots.

For environments using a private or corporate certificate authority, supply an additional trusted CA bundle:

```bash
uv run python custom_proxy.py --upstream-ca corporate-ca.pem
```

This configures the proxy-to-server TLS connection, not the client-to-proxy connection.

## Quantum Job Monitoring

The proxy monitors Quantum traffic for two main operations.

### 1. Job submission

A submission is detected when an intercepted request satisfies:

```text
Method: POST
Path:   Ends with /jobs
```

The proxy attempts to extract:

- Username and email from JWT identity claims.
- Target quantum backend.
- Number of shots.
- Original job submission payload.
- Submission timestamp and detected public IP.

When the corresponding response arrives, the proxy parses its JSON body and extracts the job ID.

The submission metadata is then sent to the C2 server.

JWT claims are decoded for monitoring without cryptographic signature verification. They should not be treated as independently authenticated identity information.

### 2. Job results

The proxy also monitors responses associated with paths containing `/results`.

It attempts to extract:

- Job ID.
- Response status code.
- Job results payload.
- Timestamp and detected public IP.
- Original request URL.

Results are sent to the C2 server.

The C2 server is responsible for processing these events and associating them with the corresponding job submissions.

## HTTP Response Decompression

The proxy supports conditional decoding of:

- `gzip`
- `deflate`
- Uncompressed responses

The `decode_response_body()` function examines the `Content-Encoding` value and returns the decoded content for inspection.

The decoding process is centralized in the request-forwarding logic.

```text
Upstream Response
       |
       v
Read Original Body
       |
       +------------------------+
       |                        |
       v                        v
Decode for Inspection    Preserve Original Body
       |                        |
       v                        v
Parse Job Metadata       Forward to Client
       |
       v
Send to C2
```

If decompression fails or the encoding is unsupported, the proxy logs a warning and skips response inspection while retaining the original response for forwarding.

## Traffic Logging

For configured target domains, the proxy logs HTTP request and response information, including:

- Request method and URL.
- HTTP headers.
- Request bodies.
- Response status.
- Response headers.
- Decoded response content when supported.

Response bodies displayed in the logs are truncated to 2,000 characters.

Non-target HTTPS traffic is forwarded without application-layer inspection.

To save logs:

```bash
uv run python custom_proxy.py --log-file traffic.log
```

## Testing

After starting the proxy and C2 server:

1. Configure the quantum client to use the proxy and trust its CA.
2. Submit a new Quantum job.
3. Verify that job submission detection appears in the proxy logs.
4. Confirm that the job ID is extracted from the submission response.
5. Confirm that the C2 server receives the submission metadata.
6. Retrieve the job results and verify that the results event is captured.
7. Check that the monitoring dashboard associates the results with the correct job.
8. Verify that unrelated HTTPS traffic continues to work through the proxy.

Also test both compressed and uncompressed HTTP responses to confirm that conditional decompression works correctly.
