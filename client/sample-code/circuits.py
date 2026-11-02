from qiskit import QuantumCircuit


def bell_circuit(num_qubits: int=2) -> QuantumCircuit:
    """
    Create an n-qubit Bell (GHZ-like) circuit.

    Args:
        num_qubits: number of qubits (≥2)

    Returns:
        QuantumCircuit object preparing
        (|0…0> + |1…1>)/√2 on num_qubits qubits.
    """
    if num_qubits < 2:
        raise ValueError("Need at least 2 qubits for a Bell/GHZ circuit")

    qc = QuantumCircuit(num_qubits)

    # 1. Create superposition on qubit 0
    qc.h(0)

    # 2. Entangle each subsequent qubit with the previous one
    for i in range(num_qubits - 1):
        qc.cx(i, i + 1)

    return qc
