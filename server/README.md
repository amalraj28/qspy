# QSpy server setup

This package contains the QSpy collection server, its web dashboard, and the
mitmproxy addon distributed to authorized clients. Its contents are intended to
be unpacked directly into the server's deployment directory. All paths and
commands in this guide treat that directory as the project root.

The server receives captured IBM Quantum traffic at `/api/log`, reconstructs
circuits with Qiskit, decodes results, and displays recent activity in the
dashboard.

Use QSpy only on systems and traffic that you own or have explicit permission
to inspect.

## Directory layout

```text
qspy-server/
├── c2_server.py                  # Flask API and server-side decoder
├── pyproject.toml                # Server project and dependency metadata
├── uv.lock                       # Reproducible server dependency lock
├── requirements.txt              # Optional pip dependency list
├── static/
│   └── web_monitor.py            # Mitmproxy addon downloaded by clients
├── templates/
│   └── quantum_dashboard.html    # Flask dashboard template
└── README.md
```

Flask serves the dashboard template at `/` and automatically makes the addon
available at `/static/web_monitor.py`.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the locked
  installation workflow, or pip as a fallback
- Network access from authorized clients to TCP port `8000`
- A virtual environment is recommended

## Unpack and install the server

Copy the server package to the target machine, unpack it, and enter the
directory containing `c2_server.py`. For example:

```bash
unzip qspy-server.zip -d qspy-server
cd qspy-server
```

If the files were transferred by another method, simply change into their
containing directory.

Create and activate a virtual environment on Linux or macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
```

Install the locked server dependencies with uv:

```bash
uv sync --locked
```

Then run commands inside the managed environment with `uv run`. Alternatively,
install the dependencies with pip:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configure the client addon

Before clients download the addon, open `static/web_monitor.py` and replace:

```python
SERVER_PORT = "SERVER_PORT"
SERVER_IP = "SERVER_IP"
```

For example:

```python
SERVER_PORT = "8000"
SERVER_IP = "192.0.2.10"
```

## Start the server

From the deployment root—the directory containing `c2_server.py`—run:

```bash
uv run python c2_server.py
```

Waitress listens on all interfaces on port `8000`. If Waitress is unavailable,
the application falls back to Flask's development server.

Verify the service locally:

```bash
curl http://127.0.0.1:8000/health
```

The available URLs are:

| URL | Purpose |
| --- | --- |
| `http://SERVER:8000/` | Dashboard |
| `http://SERVER:8000/health` | Health check |
| `http://SERVER:8000/api/log` | Client event ingestion |
| `http://SERVER:8000/static/web_monitor.py` | Client addon download |

Permit inbound TCP port `8000` in the server firewall only for the clients or
networks that need access. For an internet-facing deployment, place the
application behind an HTTPS reverse proxy and restrict access appropriately.

## Configure the client startup script

The client package is distributed separately and is not part of this server
directory. Before transferring its `start.bat` file to an authorized Windows
client, replace its placeholders with the server URL and local mitmproxy port:

```bat
SET "SCRIPT_URL=http://SERVER:8000/static/web_monitor.py"
SET "SCRIPT_NAME=web_monitor.py"
SET "MITMPROXY_PORT=8080"
```

The batch script downloads the configured addon, starts mitmproxy on the client,
and opens a command prompt with proxy environment variables set. The client must
trust its local mitmproxy CA certificate for HTTPS interception to work.

## Validation records

By default, validation records are written to `validation_records/` inside the
deployment root, provided the server is started from that directory as shown
above.

Set `QSPY_VALIDATION_DIR` to use another location:

```bash
export QSPY_VALIDATION_DIR=/var/lib/qspy/validation_records
uv run python c2_server.py
```

On Windows PowerShell:

```powershell
$env:QSPY_VALIDATION_DIR = "C:\qspy-data\validation_records"
uv run python c2_server.py
```

Validation records can include IBM job IDs, client IP addresses, timestamps,
circuit metadata, and result data. Review and anonymize them before sharing or
publishing them.

## Troubleshooting

- **Dashboard returns `TemplateNotFound`:** Confirm that
  `templates/quantum_dashboard.html` exists and start the checked-in
  `c2_server.py`.
- **Addon download returns 404:** Confirm that `static/web_monitor.py` exists,
  then open its `/static/web_monitor.py` URL from the client.
- **Client cannot reach the server:** Check `/health`, the firewall, and the
  `SERVER_IP` and `SERVER_PORT` embedded in the addon.
- **No IBM traffic is captured:** Run the workload from the command prompt
  opened by `start.bat` and ensure the client trusts the mitmproxy CA.
- **Port 8000 is already in use:** Stop the conflicting service or update both
  server startup calls and every client-facing URL to use another port.
