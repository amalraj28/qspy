from qiskit_ibm_runtime import QiskitRuntimeService
from qiskit import QuantumCircuit
from circuits import bell_circuit
from dotenv import load_dotenv
import os
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime import SamplerV2 as Sampler


def draw_circuit(qc: QuantumCircuit, message: str):
    print(message)
    print(qc.draw("text"))

qc = bell_circuit(num_qubits=2)
qc.measure_all()

assert qc.num_qubits == qc.num_clbits, "measure_all may not be the best way to append measurements"

draw_circuit(qc, "Printing original circuit")

load_dotenv(".env")
api_key = "API_TOKEN"

key = os.environ.get(api_key)

if key is None:
    raise KeyError(f"API key is not available in environment variables, or is not set in the variable \"{api_key}\"")

print(f"Key = {key}")

QiskitRuntimeService.save_account(token=key, set_as_default=True, overwrite=True, instance="instance-sit")
service = QiskitRuntimeService()

backend = service.least_busy(simulator=False, operational=True)
print(f"Backend = {backend}")

pm = generate_preset_pass_manager(backend=backend)
transpiled = pm.run(qc)

draw_circuit(transpiled, "Printing transpiled circuit")

sampler = Sampler(backend)
job = sampler.run([transpiled], shots=1024)

res = job.result()

meas = res[0].data.meas.get_counts()

print(meas)
