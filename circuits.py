import math
import random

from qiskit import QuantumCircuit
from qiskit.circuit.random import random_circuit


def bell_circuit() -> QuantumCircuit:
    """
    Create the standard two-qubit Bell state:

        (|00> + |11>) / sqrt(2)

    Returns:
        QuantumCircuit preparing a two-qubit Bell state.
    """
    qc = QuantumCircuit(2)

    qc.h(0)
    qc.cx(0, 1)

    return qc


def ghz_circuit(num_qubits: int = 5) -> QuantumCircuit:
    """
    Create an n-qubit GHZ state:

        (|00...0> + |11...1>) / sqrt(2)

    Args:
        num_qubits:
            Number of qubits. Must be at least 3.

    Returns:
        QuantumCircuit preparing an n-qubit GHZ state.
    """
    if num_qubits < 3:
        raise ValueError(
            "GHZ circuit requires at least 3 qubits. Use bell_circuit() for the two-qubit case."
        )

    qc = QuantumCircuit(num_qubits)

    qc.h(0)

    for qubit in range(num_qubits - 1):
        qc.cx(qubit, qubit + 1)

    return qc


def qft_circuit(
    num_qubits: int = 5,
    include_swaps: bool = True,
) -> QuantumCircuit:
    """
    Create a Quantum Fourier Transform circuit.

    The implementation is written explicitly rather than relying on a
    library-level composite instruction so that the resulting circuit
    structure is transparent and reproducible.

    Args:
        num_qubits:
            Number of qubits. Must be at least 2.

        include_swaps:
            Whether to append the final qubit-order reversal swaps.

    Returns:
        QuantumCircuit implementing the QFT.
    """
    if num_qubits < 2:
        raise ValueError("QFT circuit requires at least 2 qubits.")

    qc = QuantumCircuit(num_qubits)

    for target in range(num_qubits):
        qc.h(target)

        for control in range(target + 1, num_qubits):
            angle = math.pi / (2 ** (control - target))

            qc.cp(
                angle,
                control,
                target,
            )

    if include_swaps:
        for qubit in range(num_qubits // 2):
            qc.swap(
                qubit,
                num_qubits - qubit - 1,
            )

    return qc


def grover_circuit(
    num_qubits: int = 4,
    marked_state: str = "1111",
    reps: int | None = None,
) -> QuantumCircuit:
    """
    Create an n-qubit Grover search circuit for one marked computational
    basis state.

    Args:
        num_qubits:
            Number of search qubits. Must be at least 2.

        marked_state:
            Binary string representing the state marked by the oracle.
            Its length must equal num_qubits.

            The leftmost bit corresponds to the highest-indexed qubit,
            following Qiskit's computational-basis string convention.

        reps:
            Number of Grover iterations. When None, the approximately
            optimal number of iterations is calculated automatically.

    Returns:
        QuantumCircuit implementing Grover search without measurements.
    """
    if num_qubits < 2:
        raise ValueError(
            "Grover circuit requires at least 2 qubits."
        )

    if len(marked_state) != num_qubits:
        raise ValueError(
            "marked_state length must equal num_qubits."
        )

    if any(bit not in {"0", "1"} for bit in marked_state):
        raise ValueError(
            "marked_state must contain only '0' and '1'."
        )

    if reps is None:
        search_space_size = 2**num_qubits
        theta = math.asin(1 / math.sqrt(search_space_size))

        reps = round(
            math.pi / (4 * theta) - 0.5
        )

    if reps < 1:
        raise ValueError(
            "reps must be at least 1."
        )

    qc = QuantumCircuit(num_qubits)

    # Prepare the uniform superposition.
    qc.h(range(num_qubits))
    qc.barrier()

    for iteration in range(reps):
        # -------------------------------------------------
        # Oracle
        # -------------------------------------------------
        # Map the marked state to |11...1>.
        for qubit, bit in enumerate(reversed(marked_state)):
            if bit == "0":
                qc.x(qubit)

        # Apply a phase flip to |11...1>.
        qc.h(num_qubits - 1)
        qc.mcx(
            list(range(num_qubits - 1)),
            num_qubits - 1,
        )
        qc.h(num_qubits - 1)

        # Restore the original computational-basis mapping.
        for qubit, bit in enumerate(reversed(marked_state)):
            if bit == "0":
                qc.x(qubit)

        qc.barrier()

        # -------------------------------------------------
        # Diffusion operator
        # -------------------------------------------------
        qc.h(range(num_qubits))
        qc.x(range(num_qubits))

        qc.h(num_qubits - 1)
        qc.mcx(
            list(range(num_qubits - 1)),
            num_qubits - 1,
        )
        qc.h(num_qubits - 1)

        qc.x(range(num_qubits))
        qc.h(range(num_qubits))

        if iteration < reps - 1:
            qc.barrier()

    return qc

def parameterized_ansatz_circuit(
    num_qubits: int = 6,
    repetitions: int = 3,
    seed: int = 42,
) -> QuantumCircuit:
    """
    Create a deterministic, numerically bound variational-style ansatz.

    Each repetition contains:
      1. RY and RZ rotations on every qubit.
      2. A nearest-neighbor entangling layer.
      3. A closing entangling gate between the final and first qubits.

    Numerical parameter values are generated deterministically from the
    supplied seed. This makes the circuit directly executable by SamplerV2
    without requiring separate parameter binding.

    Args:
        num_qubits:
            Number of qubits. Must be at least 2.

        repetitions:
            Number of variational layers. Must be at least 1.

        seed:
            Seed used to deterministically generate rotation angles.

    Returns:
        QuantumCircuit containing parameterized-style rotation and
        entanglement layers with bound numerical values.
    """
    if num_qubits < 2:
        raise ValueError("Parameterized ansatz requires at least 2 qubits.")

    if repetitions < 1:
        raise ValueError("Number of repetitions must be at least 1.")

    rng = random.Random(seed)

    qc = QuantumCircuit(num_qubits)

    for _ in range(repetitions):
        # Single-qubit rotation layer.
        for qubit in range(num_qubits):
            theta = rng.uniform(0.0, 2.0 * math.pi)
            phi = rng.uniform(0.0, 2.0 * math.pi)

            qc.ry(theta, qubit)
            qc.rz(phi, qubit)

        # Linear entanglement.
        for qubit in range(num_qubits - 1):
            qc.cx(qubit, qubit + 1)

        # Close the entanglement ring.
        if num_qubits > 2:
            qc.cx(num_qubits - 1, 0)

        qc.barrier()

    return qc


def random_quantum_circuit(
    num_qubits: int = 10,
    depth: int = 20,
    seed: int = 42,
) -> QuantumCircuit:
    """
    Create a deterministic random quantum circuit.

    The fixed seed makes the generated workload reproducible across
    experimental runs.

    Args:
        num_qubits:
            Number of qubits. Must be at least 2.

        depth:
            Requested random-circuit depth. Must be at least 1.

        seed:
            Random seed controlling circuit generation.

    Returns:
        Randomly generated QuantumCircuit.
    """
    if num_qubits < 2:
        raise ValueError("Random circuit requires at least 2 qubits.")

    if depth < 1:
        raise ValueError("Random circuit depth must be at least 1.")

    return random_circuit(
        num_qubits=num_qubits,
        depth=depth,
        max_operands=2,
        measure=False,
        seed=seed,
    )
