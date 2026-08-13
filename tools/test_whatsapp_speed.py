import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


# ============================================================
# PROJECT CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def get_env_value(name: str) -> str:
    """Read a simple KEY=value entry from the project's .env file."""
    if not ENV_FILE.exists():
        raise RuntimeError(f".env file not found: {ENV_FILE}")

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()

            # Remove optional surrounding quotes.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]

            if not value:
                raise RuntimeError(f"{name} is empty in {ENV_FILE}")

            return value

    raise RuntimeError(f"{name} not found in {ENV_FILE}")


TOKEN = get_env_value("WHATSAPP_ACCESS_TOKEN")

PHONE_NUMBER_ID = "1147629025108848"

TEMPLATE_NAME = "new_visitor"
LANGUAGE = "en"
TEST_NAME = "Test"

API_VERSION = "v21.0"


# ============================================================
# COMMAND-LINE INPUT
# ============================================================

if len(sys.argv) != 3:
    print()
    print("Usage:")
    print("  python tools/test_whatsapp_speed.py <concurrency> <recipient>")
    print()
    print("Examples:")
    print("  python tools/test_whatsapp_speed.py 10 27832916327")
    print("  python tools/test_whatsapp_speed.py 20 27832916327")
    print()
    sys.exit(1)


try:
    CONCURRENCY = int(sys.argv[1])
except ValueError:
    print("Error: concurrency must be a whole number.")
    sys.exit(1)


TO_NUMBER = sys.argv[2]


if CONCURRENCY < 1:
    print("Error: concurrency must be at least 1.")
    sys.exit(1)


# ============================================================
# SEND ONE MESSAGE
# ============================================================

def send_message(test_number):

    url = (
        f"https://graph.facebook.com/"
        f"{API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": TO_NUMBER,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {
                "code": LANGUAGE,
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": TEST_NAME,
                        }
                    ],
                }
            ],
        },
    }

    start = time.perf_counter()

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30,
        )

        elapsed = time.perf_counter() - start

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return {
            "number": test_number,
            "elapsed": elapsed,
            "status": response.status_code,
            "ok": response.status_code == 200,
            "body": body,
        }

    except Exception as exc:
        return {
            "number": test_number,
            "elapsed": time.perf_counter() - start,
            "status": None,
            "ok": False,
            "body": str(exc),
        }


# ============================================================
# CONCURRENT TEST
# ============================================================

def concurrent_test(concurrency):

    print()
    print("=" * 60)
    print(f"{concurrency}-WAY CONCURRENT TEST")
    print("=" * 60)

    start = time.perf_counter()

    results = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:

        futures = [
            executor.submit(send_message, i + 1)
            for i in range(concurrency)
        ]

        for future in as_completed(futures):
            results.append(future.result())

    total = time.perf_counter() - start

    results.sort(key=lambda x: x["number"])

    for result in results:
        print(
            f"Request {result['number']}: "
            f"{result['elapsed']:.3f}s "
            f"(HTTP {result['status']})"
        )

    successful = sum(1 for r in results if r["ok"])

    print("-" * 60)
    print(f"Total wall time: {total:.3f}s")
    print(f"Successful:      {successful}/{concurrency}")

    if total > 0:
        print(
            f"Effective rate:  "
            f"{successful / total:.2f} msg/sec"
        )

    failed = [r for r in results if not r["ok"]]

    if failed:
        print()
        print("FAILED RESPONSES:")

        for result in failed:
            print(
                f"Request {result['number']}: "
                f"{result['body']}"
            )


# ============================================================
# MAIN
# ============================================================

print()
print("WhatsApp Graph API speed test")
print("-" * 60)
print(f"Recipient:       {TO_NUMBER}")
print(f"Template:        {TEMPLATE_NAME}")
print(f"Concurrency:     {CONCURRENCY}")
print(f"Phone number ID: {PHONE_NUMBER_ID}")
print(f"API version:     {API_VERSION}")
print(f".env:            {ENV_FILE}")
print()
print(f"This will send {CONCURRENCY} WhatsApp messages.")
print()

input("Press ENTER to start, or Ctrl+C to cancel...")

concurrent_test(CONCURRENCY)

print()
print("=" * 60)
print("TEST COMPLETE")
print("=" * 60)
