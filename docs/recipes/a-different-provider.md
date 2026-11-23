# A different provider

```python
from featherweight import Claude


class Triager(AgentBase):
    ...
    model: Claude = Claude(model="claude-haiku-5-5", effort="low")
```

`pip install "featherweight[anthropic]"` and set `ANTHROPIC_API_KEY`.
