import requests

proxies = {
    "http": "http://127.0.0.1:8888",
    "https": "http://127.0.0.1:8888",
}

print("Testing pass through API request")

try:
    response = requests.get(
        "https://httpbin.org",
        proxies=proxies,
        verify=False,
    )
    print(f"Pass-through Status: {response.status_code}")

except Exception as e:
    print(f"Pass-through failed: {e}")


print("Testing intercepted API request")

try:
    response = requests.get(
        "https://example.com",
        proxies=proxies,
        verify=False,
    )
    print(f"Pass-through Status: {response.status_code}")

except Exception as e:
    print(f"Pass-through failed: {e}")
