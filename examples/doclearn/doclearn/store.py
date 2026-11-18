"""Storage for what the learning pipeline learns: one row per learning run in `learned_documents`.

The tables are specific to this task, so they live here and not in `micro`; they are created on the shared
sqlite connection. Each run adds a row (history is kept); `latest` returns the newest for a category.
"""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from doclearn.models import DocumentTemplate, FactsSchema, LearnedTree
from micro.db import Database, new_id

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_documents (
    learned_id   TEXT PRIMARY KEY,
    job_id       TEXT REFERENCES jobs(job_id),
    category     TEXT NOT NULL,
    template     TEXT NOT NULL,
    tree         TEXT NOT NULL,
    facts_schema TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learned_documents_category ON learned_documents(category, created_at);
"""


class LearnedDocument(BaseModel):
    learned_id: str
    job_id: str | None
    category: str
    template: DocumentTemplate  # the reconciled template
    tree: LearnedTree  # the normalised document tree
    facts_schema: FactsSchema  # the inputs to collect
    created_at: str


class LearnedStore:
    def __init__(self, db: Database):
        self.db = db

    @classmethod
    async def open(cls, db: Database) -> "LearnedStore":
        """Create the table if needed and return the store."""
        await db.conn.executescript(SCHEMA)
        await db.conn.commit()
        return cls(db)

    async def save(
        self,
        *,
        category: str,
        template: DocumentTemplate,
        tree: LearnedTree,
        facts_schema: FactsSchema,
        job_id: str | None = None,
    ) -> str:
        learned_id = new_id()
        await self.db.conn.execute(
            """INSERT INTO learned_documents (learned_id, job_id, category, template, tree, facts_schema, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                learned_id,
                job_id,
                category,
                template.model_dump_json(),
                tree.model_dump_json(),
                facts_schema.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        await self.db.conn.commit()
        logger.info(
            "learned_saved learned_id=%s job_id=%s category=%s units=%d schema_fields=%d",
            learned_id,
            job_id,
            category,
            len(tree.nodes),
            len(facts_schema.fields),
        )
        return learned_id

    async def get(self, learned_id: str) -> LearnedDocument | None:
        async with self.db.conn.execute("SELECT * FROM learned_documents WHERE learned_id = ?", (learned_id,)) as cur:
            row = await cur.fetchone()
        return self._to_model(row) if row else None

    async def latest(self, category: str) -> LearnedDocument | None:
        """The most recent learning result for a category."""
        async with self.db.conn.execute(
            "SELECT * FROM learned_documents WHERE category = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (category,),
        ) as cur:
            row = await cur.fetchone()
        return self._to_model(row) if row else None

    async def list(self, category: str | None = None) -> list[LearnedDocument]:
        """All results, oldest first, optionally for one category."""
        sql, params = "SELECT * FROM learned_documents", []
        if category is not None:
            sql, params = sql + " WHERE category = ?", [category]
        async with self.db.conn.execute(sql + " ORDER BY rowid", params) as cur:
            rows = await cur.fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row) -> LearnedDocument:
        data = dict(row)
        return LearnedDocument(
            learned_id=data["learned_id"],
            job_id=data["job_id"],
            category=data["category"],
            template=DocumentTemplate.model_validate_json(data["template"]),
            tree=LearnedTree.model_validate_json(data["tree"]),
            facts_schema=FactsSchema.model_validate_json(data["facts_schema"]),
            created_at=data["created_at"],
        )
