from __future__ import annotations

# --- Validation starts --- #
import hashlib
import json
import math
from pathlib import Path

from qiskit.circuit import ParameterExpression
# --- Validation ends --- #

import os
from typing import Any

from dotenv import load_dotenv
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService
from qiskit_ibm_runtime import SamplerV2 as Sampler

from circuits import *


def draw_circuit(qc: QuantumCircuit, message: str) -> None:
    print(message)
    print(qc.draw("text"))


# --------------- Validation starts ------------------- #
# This block is needed only for experimental validation
# Actual user may not need to generate canonical hashing 
# and/or validation records

VALIDATION_DIR = Path("validation_records")


def canonicalize_parameter(value: Any) -> Any:
    """
    Convert a Qiskit operation parameter into a deterministic,
    JSON-serializable representation.

    The same function must be used on both the client and QSpy server.
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

        # repr() preserves enough precision to round-trip a Python float.
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

    # NumPy scalar-like objects often provide .item().
    if hasattr(value, "item"):
        try:
            scalar = value.item()

            if scalar is not value:
                return canonicalize_parameter(scalar)
        except (ValueError, TypeError):
            pass

    # Fallback for Qiskit-specific parameter objects.
    return {
        "type": type(value).__name__,
        "value": str(value),
    }


def canonicalize_circuit(circuit: QuantumCircuit) -> dict[str, Any]:
    """
    Convert a QuantumCircuit into a deterministic structural representation.

    This representation captures:
      - allocated qubits and classical bits,
      - register structure,
      - global phase,
      - ordered operations,
      - operation parameters,
      - qubit operands,
      - classical-bit operands.

    It intentionally excludes display formatting, Python object identity,
    timestamps, and unrelated metadata.
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
    Serialize the canonical circuit structure deterministically.
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


def extract_circuit_properties(
    circuit: QuantumCircuit,
) -> dict[str, Any]:
    """
    Extract circuit properties using the same definitions as the QSpy server.
    """

    active_qubits = set()
    active_clbits = set()
    gate_breakdown: dict[str, int] = {}

    for instruction in circuit.data:
        gate_name = instruction.operation.name

        gate_breakdown[gate_name] = gate_breakdown.get(gate_name, 0) + 1

        for qubit in instruction.qubits:
            active_qubits.add(circuit.find_bit(qubit).index)

        for clbit in instruction.clbits:
            active_clbits.add(circuit.find_bit(clbit).index)

    return {
        "active_qubits": len(active_qubits),
        "active_clbits": len(active_clbits),
        "allocated_qubits": circuit.num_qubits,
        "allocated_clbits": circuit.num_clbits,
        "depth": circuit.depth(),
        "num_gates": len(circuit.data),
        "gate_breakdown": gate_breakdown,
    }


def save_client_validation_record(
    *,
    job_id: str,
    backend_name: str,
    circuit: QuantumCircuit,
    measurement_results: dict[str, int],
    shots: int,
) -> None:
    """
    Save the client-side ground-truth record.

    This file remains on the client and should not be sent to QSpy.
    """

    VALIDATION_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    canonical_hash = canonical_circuit_sha256(circuit)

    record = {
        "job_id": job_id,
        "backend": backend_name,
        "shots": shots,
        "canonical_schema_version": 1,
        "canonical_circuit_sha256": canonical_hash,
        "circuit_properties": extract_circuit_properties(circuit),
        "measurement_results": {
            str(state): int(count)
            for state, count in sorted(measurement_results.items())
        },
    }

    output_path = VALIDATION_DIR / f"client_{job_id}.json"

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

    print("\nClient-side validation record saved to " f"{output_path}")

    print("Client canonical circuit SHA-256 = " f"{canonical_hash}")

# --------------- Validation ends ------------------- #


def get_backend_name(backend: Any) -> str:
    """
    Safely obtain the backend name.
    """

    name = getattr(backend, "name", None)

    if callable(name):
        return str(name())

    if name is not None:
        return str(name)

    return str(backend)


def main() -> None:
    shots = 1024

    # qc = bell_circuit()
    # qc = ghz_circuit(num_qubits=3)
    qc = grover_circuit(
        num_qubits=4,
        marked_state="1010",
    )
    # qc = qft_circuit(num_qubits=5)
    # qc = parameterized_ansatz_circuit(
    #     num_qubits=6,
    #     repetitions=3,
    #     seed=42,
    # )

    # qc = random_quantum_circuit(
    #     num_qubits=8,
    #     depth=20,
    #     seed=42,
    # )
    qc.measure_all()

    assert (
        qc.num_qubits == qc.num_clbits
    ), "measure_all may not be the best way to append measurements"

    draw_circuit(
        qc,
        "Printing original circuit",
    )

    load_dotenv(".env")

    api_key_environment_variable = "API_TOKEN"

    key = os.environ.get(api_key_environment_variable)

    instance = os.environ.get("QISKIT_INSTANCE_NAME")

    if key is None:
        raise KeyError(
            "API key is not available in environment "
            "variables, or is not set in the variable "
            f'"{api_key_environment_variable}"'
        )

    QiskitRuntimeService.save_account(
        token=key,
        set_as_default=True,
        overwrite=True,
        instance=instance,
    )

    service = QiskitRuntimeService()

    backend = service.least_busy(
        simulator=False,
        operational=True,
    )

    backend_name = get_backend_name(backend)

    print(f"Backend = {backend_name}")

    pm = generate_preset_pass_manager(backend=backend)

    transpiled = pm.run(qc)

    draw_circuit(
        transpiled,
        "Printing transpiled circuit",
    )

    # --- Validation starts --- #
    # -------------------------------------------------
    # GROUND-TRUTH CIRCUIT HASH
    # -------------------------------------------------
    #
    # This is the backend-ready QuantumCircuit that is
    # immediately about to be submitted to IBM Quantum.
    #
    # QSpy must independently decode the intercepted QPY
    # payload on the C2 server, canonicalize the resulting
    # QuantumCircuit with the exact same procedure, and
    # compute its own SHA-256 hash.
    #
    # The client-side and server-side hashes are joined
    # later using IBM's job_id.
    # -------------------------------------------------

    client_circuit_hash = canonical_circuit_sha256(transpiled)

    print(f"\nClient canonical circuit SHA-256 = {client_circuit_hash}")
    # --- Validation ends --- #

    sampler = Sampler(backend)

    job = sampler.run(
        [transpiled],
        shots=shots,
    )

    job_id = job.job_id()

    print(f"IBM Job ID = {job_id}")

    result = job.result()

    measurement_results = result[0].data.meas.get_counts()

    measurement_results = {
        str(state): int(count) for state, count in sorted(measurement_results.items())
    }

    print(f"Measurement results = " f"{measurement_results}")

    # --- Validation starts --- #
    save_client_validation_record(
        job_id=job_id,
        backend_name=backend_name,
        circuit=transpiled,
        measurement_results=measurement_results,
        shots=shots,
    )
    # --- Validation ends --- #


if __name__ == "__main__":
    main()
