# A skill from a database or S3

A skill is plain data, so it can live anywhere. Text in a database needs nothing; text in an object store is fetched when the agent asks.

```python
from featherweight import Skill

# the skill is a row of text in your database: build it from the rows, nothing to add
skill = Skill.from_markdown(row["skill_md"], resources={r["path"]: r["text"] for r in resource_rows})
```

```python
from featherweight import Skill


# the skill is in S3: fetch it when the agent asks, without blocking the event loop
class BucketSkill(Skill):
    etag: str  # changes when the text does: it is a field, so it is in the agent's hash

    async def aread(self, resource: str = "") -> str:
        return await bucket.get_text(f"{self.name}/{resource or 'SKILL.md'}")  # your client, awaited


skill = BucketSkill(name="meeting-notes", description="How this department writes meeting notes", etag=head.etag)
```

<!-- skill:omit -->
- **The name and the description are fields,** because they are in the prompt. Only the instructions and the files are read late: the agent's `get_skill` tool awaits `skill.aread(resource)`.
- **Give the subclass a field that changes when the text does** (an `etag`, a `version`). The agent's hash is made from the fields of its skills, and fetched text is not one of them: without it, an edit
  in the store leaves the hash as it was, and [resuming a job](../jobs.md#resuming-a-job) reuses results made with the old text.
- **The runner checks the path first.** The model chooses `resource`, so the runner gives `aread` only a normalised path inside the skill (never `/x`, `a/../b`, a backslash or a path over 256 characters): the key you build from it stays under `{self.name}/`. It does not decode `%2e%2e`, so a client that does must check again.
- **`aread` returns text and has a timeout.** `None` or `bytes` is a failed read, and so is one that takes longer than `FTW_SKILL_READ_TIMEOUT` seconds (15): the agent gets `could not be read`, and the run goes on.
- **Per tenant?** `CURRENT_IDENTITY` (`from featherweight.context import CURRENT_IDENTITY`) is who the run is for, inside `aread`: check what it may fetch, as [a tool does](a-tool-that-gets-a-token-for-its-identity.md).
- **A read that fails does not fail the run:** the agent is told `The skill 'x' could not be read: <the type of the error>`, and the error goes to the log as `skill_read_failed`, because its message can name a bucket or a key.
- **Validate what users write before you save it:** `Skill.from_markdown(text)` raises `SkillError` for a skill that does not follow the specification, which is the check a web editor needs.
- The guide has the rules: [reading a skill from somewhere else](../skills.md#reading-a-skill-from-somewhere-else).
<!-- /skill:omit -->
