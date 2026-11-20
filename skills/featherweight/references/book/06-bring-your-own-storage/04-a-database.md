<!-- Copied from docs/book/06-bring-your-own-storage/04-a-database.md by scripts/build_skill.py. Edit the original, not this file. -->

# A database

Put the run log in a store that featherweight does not ship.

This is an outline, and the biggest of the four. The databases are `SqliteDatabase`, `PostgresDatabase` and yours. Most people never need to write one.

1. **Read the schema.** The tables, their columns and constraints are written down in [the schema](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md#the-schema).
2. **Subclass `Database`** and write its methods. They come in groups: connect and close; create, get and change a job; the compare-and-set steps
   that take, cancel, renew or take over a job; the events of a job; and the runs (create, mark running, finish, get, list, find one to reuse).
   Records go in and rows come out as Pydantic models, so a wrong type fails when the row is built.
3. **Mind the two hard rules.** An event is written in the same transaction as the status change, and a compare-and-set step is one atomic statement, so
   that of a worker and a client exactly one wins.
4. **Check it** with the kit. It gives you a fresh, empty and unopened database, opens and closes it itself, and names the rule that you break:

```python
await testing.check_database(lambda: MyDatabase(dsn))
```

## What to notice

- Both ready-made databases pass this same kit. It is the contract, and the schema is the documentation.
- If you only need the results somewhere else, you want [a result store](02-a-result-store.md), not a database.

> **Why** a `Database` is an abstract class with a kit: we cannot know your store, so we say exactly what it must do. See [the database](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md#the-database).
