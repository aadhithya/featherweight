# A result store of your own

Resuming a job reads the outputs of finished steps back from a **result store**. Featherweight ships a sqlite one for local
development; in a deployment you write your own, because where results live (a folder, a bucket, a database per tenant) is your
decision. This example is the whole job: [`store.py`](store.py) is a store that keeps a JSON file per result, in about a dozen
lines, and then checks it with the conformance kit.

```
make example-results
```

It needs no model and no API key. Without Docker: `uv run python examples/results/store.py`.

The store subclasses `ResultStore` and writes three methods (`put`, `get`, `delete_job`). One that misses a method cannot be
built. `testing.check_result_store` then runs the contract against it and raises an `AssertionError` that says which rule it
breaks (for example "delete_job must remove every result of the job"). To use it, pass it to the runner:
`AgentRunner(db, results=FileResultStore(folder))`.
