"""Thread-safe traffic capture and a standard-library terminal inspector."""
from collections import deque
from datetime import datetime
import json
import threading
import time
from urllib.parse import parse_qsl, urlsplit


def decode_body(headers, body):
    import gzip
    import zlib

    try:
        for encoding in reversed(headers.get("Content-Encoding", "").lower().split(",")):
            encoding = encoding.strip()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            elif encoding not in ("", "identity"):
                raise ValueError(f"unsupported content encoding: {encoding}")
        return body.decode(headers.get_content_charset() or "utf-8", errors="replace")
    except (ValueError, OSError, EOFError, LookupError, zlib.error) as exc:
        return f"[body could not be decoded: {exc}; {len(body)} bytes]"


def message_details(first_line, headers, body):
    content = decode_body(headers, body)
    media_type = headers.get_content_type()
    fields = parse_qsl(content, keep_blank_values=True) if media_type == "application/x-www-form-urlencoded" else []
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return {"first_line": first_line, "headers": list(headers.items()),
            "body": content, "bytes": len(body), "form": fields}


class FlowStore:
    def __init__(self, limit=500):
        self.flows = deque(maxlen=limit)
        self.lock = threading.Lock()
        self.next_id = 1

    def start(self, method, url, version, headers, body):
        request = message_details(f"{method} {url} {version}", headers, body)
        with self.lock:
            flow = {"id": self.next_id, "time": datetime.now().strftime("%H:%M:%S"),
                    "started": time.monotonic(), "method": method, "url": url,
                    "query": parse_qsl(urlsplit(url).query, keep_blank_values=True),
                    "request": request, "response": None, "error": None,
                    "status": None, "duration": None}
            self.next_id += 1
            self.flows.append(flow)
            return flow

    def finish(self, flow, response=None, body=b"", error=None):
        details = message_details(f"HTTP/1.1 {response.status} {response.reason}", response.headers, body) if response else None
        with self.lock:
            flow.update(response=details, status=response.status if response else None,
                        error=str(error) if error else None,
                        duration=(time.monotonic() - flow["started"]) * 1000)

    def snapshot(self):
        with self.lock:
            return [dict(flow) for flow in self.flows]


def detail_lines(flow):
    lines = [f'Flow #{flow["id"]}  {flow["time"]}', flow["url"], "", "Query arguments"]
    lines += [f"  {key}: {value}" for key, value in flow["query"]] or ["  (none)"]
    for title, message in (("Request", flow["request"]), ("Response", flow["response"])):
        lines += ["", title]
        if message is None:
            lines += [flow["error"] or "Waiting for response…"]
            continue
        lines += [message["first_line"], "", "Headers"]
        lines += [f"  {key}: {value}" for key, value in message["headers"]]
        if message["form"]:
            lines += ["", "Form arguments"] + [f"  {key}: {value}" for key, value in message["form"]]
        lines += ["", f'Body ({message["bytes"]} bytes on wire)', message["body"] or "(empty)"]
    if flow["error"] and flow["response"]:
        lines += ["", "Error: " + flow["error"]]
    return "\n".join(lines).splitlines()


def run_viewer(store, address):
    """Run on the main thread; proxy handlers continue on background threads."""
    import curses
    import locale

    locale.setlocale(locale.LC_ALL, "")

    def draw(screen):
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
        selected = 0
        opened = None
        offset = 0
        horizontal = 0
        while True:
            flows = store.snapshot()
            selected = max(0, min(selected, len(flows) - 1))
            height, width = screen.getmaxyx()
            screen.erase()

            def put(y, value, attr=0):
                if 0 <= y < height and width > 1:
                    # Render control characters as text, never as terminal commands.
                    value = "".join(c if c.isprintable() else " " for c in value)
                    try:
                        screen.addnstr(y, 0, value, width - 1, attr)
                    except curses.error:
                        pass

            put(0, f"Traffic · {address[0]}:{address[1]} · {len(flows)} flows (latest {store.flows.maxlen})", curses.A_BOLD)
            if opened is None:
                put(1, "     TIME      METHOD   STATUS       SIZE     TIME  URL", curses.A_BOLD)
                available = max(1, height - 3)
                start = max(0, selected - available + 1)
                for index in range(start, min(len(flows), start + available)):
                    flow = flows[index]
                    status = str(flow["status"] or ("ERROR" if flow["error"] else "pending"))
                    size = str(flow["response"]["bytes"]) if flow["response"] else "-"
                    duration = f'{flow["duration"]:.0f}ms' if flow["duration"] is not None else "-"
                    put(index - start + 2, f'{flow["id"]:4} {flow["time"]}  {flow["method"]:7} {status:7} {size:>8} {duration:>8}  {flow["url"]}', curses.A_REVERSE if index == selected else 0)
                if not flows:
                    put(3, "Waiting for traffic to configured target domains…")
                put(height - 1, "↑/↓ select · Enter inspect · q quit")
            else:
                current = next((f for f in flows if f["id"] == opened["id"]), opened)
                lines = detail_lines(current)
                offset = min(offset, max(0, len(lines) - max(1, height - 2)))
                for y, line in enumerate(lines[offset:offset + max(0, height - 2)], 1):
                    put(y, line[horizontal:])
                put(height - 1, "↑/↓ scroll · PgUp/PgDn · ←/→ pan · Esc/b back · q quit")
            screen.refresh()
            key = screen.getch()
            if key == ord("q"):
                return
            if opened is None:
                if key in (curses.KEY_DOWN, ord("j")):
                    selected = min(len(flows) - 1, selected + 1)
                elif key in (curses.KEY_UP, ord("k")):
                    selected = max(0, selected - 1)
                elif key in (10, 13, curses.KEY_ENTER) and flows:
                    opened, offset, horizontal = flows[selected], 0, 0
            elif key in (27, ord("b")):
                opened = None
            elif key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset += max(1, height - 3)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - max(1, height - 3))
            elif key == curses.KEY_RIGHT:
                horizontal += 20
            elif key == curses.KEY_LEFT:
                horizontal = max(0, horizontal - 20)
    curses.wrapper(draw)
