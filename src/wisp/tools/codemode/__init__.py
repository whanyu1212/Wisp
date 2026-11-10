"""The experimental codemode tool: a Python script that calls other tools.

`tool.py` defines the tool the model sees; `sandbox.py` runs its script in monty;
`calls.py` holds the vocabulary the executor uses to run the script's tool calls.
monty is an optional dependency (the `codemode` extra), imported only when a
script runs.
"""
