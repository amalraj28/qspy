"""Manual smoke test: start main.py first, then run this script."""
from pathlib import Path

import requests

PROXIES = {
    "http": "http://127.0.0.1:8888",
    "https": "http://127.0.0.1:8888",
}
CA_CERT = Path(__file__).parent / ".proxy-certs" / "ca.crt"


def main():
    if not CA_CERT.exists():
        raise SystemExit("Start main.py first to generate the proxy CA")
    for label, url, trust in (
        ("HTTP intercepted", "http://example.com", True),
        ("HTTPS intercepted", "https://example.com", str(CA_CERT)),
        ("HTTPS pass-through", "https://httpbin.org/get", True),
    ):
        try:
            response = requests.get(url, proxies=PROXIES, verify=trust, timeout=30)
            print(f"{label}: {response.status_code}")
        except requests.RequestException as exc:
            print(f"{label} failed: {exc}")


if __name__ == "__main__":
    main()
