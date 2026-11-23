# Telling a retry what went wrong

A retry does not see the last error. If the next attempt should, make it part of the input:

```python
class FillInput(AgentInput):
    form: str
    problems: list[str] = []


result = await runner.run(Filler, FillInput(form=form), job_id=job_id)
for _ in range(2):
    if result.status == RunStatus.SUCCESS:
        break
    result = await runner.run(Filler, FillInput(form=form, problems=[result.error or ""]), job_id=job_id)
```
