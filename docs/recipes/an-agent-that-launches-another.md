# An agent that launches another

```python
class Editor(AgentBase):
    name: str = "editor"
    description: str = "Writes a headline"
    version: str = "0.1.0"
    prompt: str = "Write a headline. Ask the summariser for a summary first."
    subagents: list[type] = [Summariser]  # classes: the only agents this one may launch
```
