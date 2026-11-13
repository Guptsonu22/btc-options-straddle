from nautilus_trader.data.engine import DataEngine
from nautilus_trader.execution.engine import ExecutionEngine

print("DataEngine methods:")
for m in dir(DataEngine):
    if not m.startswith("_"):
        print(f"  DataEngine.{m}")

print("\nExecutionEngine methods:")
for m in dir(ExecutionEngine):
    if not m.startswith("_"):
        print(f"  ExecutionEngine.{m}")
