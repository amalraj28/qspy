from qiskit import QuantumCircuit


def generate_random_circuit(num_qubits: int = 2, qubits_to_measure: list[int] | str | None = "all") -> QuantumCircuit:
    qc = QuantumCircuit(num_qubits, 0)
    qc.h(0)
    qc.cx(0,1)
    qc.measure_all()
    
    return qc


    
