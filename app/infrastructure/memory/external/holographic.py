from __future__ import annotations
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.domain.memory_provider import ExternalMemoryProvider
from app.infrastructure.memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)

_FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory. ACTIONS: add, search, probe, related, reason, "
        "contradict, update, remove, list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list"]},
            "content": {"type": "string"},
            "query": {"type": "string"},
            "entity": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "fact_id": {"type": "integer"},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "tags": {"type": "string"},
            "trust_delta": {"type": "number"},
            "min_trust": {"type": "number"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}
_FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": "Rate a fact after using it. Mark helpful/unhelpful to train trust scores.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer"},
        },
        "required": ["action", "fact_id"],
    },
}
_FACT_SEARCH_SCHEMA = {
    "name": "fact_search",
    "description": (
        "Read-only retrieval over the holographic fact store. ACTIONS: search, "
        "probe, related, reason, contradict, list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "probe", "related", "reason", "contradict", "list"]},
            "query": {"type": "string"},
            "entity": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "min_trust": {"type": "number"},
            "limit": {"type": "integer"},
        },
        "required": ["action"],
    },
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    tags TEXT NOT NULL DEFAULT '',
    trust_score REAL NOT NULL,
    created_at REAL NOT NULL,
    last_hit_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    fact_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (fact_id, name),
    FOREIGN KEY (fact_id) REFERENCES facts(id) ON DELETE CASCADE
);
"""

_PREF_PATTERNS = [
    re.compile(r'\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)', re.IGNORECASE),
    re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)', re.IGNORECASE),
    re.compile(r'\bI\s+(?:always|never|usually)\s+(.+)', re.IGNORECASE),
]
_DECISION_PATTERNS = [
    re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)', re.IGNORECASE),
    re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)', re.IGNORECASE),
]


class HolographicAdapter(ExternalMemoryProvider):
    def __init__(self, *, config: dict[str, Any]) -> None:
        self._config = config
        self._db_path = str(config.get("db_path", "holographic.db"))
        self._default_trust = float(config.get("default_trust", 0.5))
        self._min_trust = float(config.get("min_trust_threshold", 0.3))
        self._decay_half_life = float(config.get("temporal_decay_half_life", 0))
        self._auto_extract = bool(config.get("auto_extract", False))
        self._retriever = MemoryRetriever()
        self._conn: sqlite3.Connection | None = None
        mode = str(config.get("recall_mode", "hybrid"))
        self._recall_mode = mode if mode in ("context", "tools", "hybrid") else "hybrid"

    @classmethod
    def factory(cls, *, http_client=None, config: dict[str, Any], secret: str | None = None) -> "HolographicAdapter":
        return cls(config=config)

    @property
    def name(self) -> str:
        return "holographic"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    def system_prompt_block(self) -> str:
        if not self._conn:
            return ""
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            total = 0
        if self._recall_mode == "tools":
            mode_label = "tools-only (use fact_search to retrieve)"
        elif self._recall_mode == "context":
            mode_label = "context-injection"
        else:
            mode_label = "hybrid (context auto-injected AND tools available)"
        return (
            "# Holographic Memory\n"
            f"Active. {total} facts stored.\n"
            f"Mode: {mode_label}."
        )

    def prefetch(self, query: str, *, session_id: str) -> str:
        if not self._conn or not query or self._recall_mode == "tools":
            return ""
        facts = self._search_facts(query, limit=5)
        if not facts:
            return ""
        now = time.time()
        lines = []
        for f in facts:
            trust = self._score(f["trust_score"], f["last_hit_at"], now)
            if trust < self._min_trust:
                continue
            lines.append(f"- [{trust:.2f}] {f['content']}")
            self._conn.execute(
                "UPDATE facts SET last_hit_at=? WHERE id=?", (now, f["id"])
            )
        self._conn.commit()
        if not lines:
            return ""
        return "## Holographic Memory\n" + "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        if not self._auto_extract or not self._conn or not user_content:
            return
        for pattern in _PREF_PATTERNS + _DECISION_PATTERNS:
            m = pattern.search(user_content)
            if m:
                category = "user_pref" if pattern in _PREF_PATTERNS else "project"
                self._add_fact(user_content[:400], category=category, tags="")
                break

    def on_session_end(self, session_id: str) -> None:
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if self._recall_mode == "context":
            return []
        if self._recall_mode == "tools":
            return [_FACT_SEARCH_SCHEMA]
        return [_FACT_SEARCH_SCHEMA, _FACT_STORE_SCHEMA, _FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if not self._conn:
            return json.dumps({"success": False, "error": "holographic not initialized"})
        if self._recall_mode == "tools" and tool_name in ("fact_store", "fact_feedback"):
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        try:
            if tool_name == "fact_search":
                return self._handle_fact_search(args)
            if tool_name == "fact_store":
                return self._handle_fact_store(args)
            if tool_name == "fact_feedback":
                return self._handle_fact_feedback(args)
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        except Exception as exc:
            logger.warning("Holographic tool %s failed: %s", tool_name, exc)
            return json.dumps({"success": False, "error": type(exc).__name__})

    def shutdown(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- fact_store actions ---

    def _handle_fact_search(self, args: dict[str, Any]) -> str:
        action = args["action"]
        if action == "search":
            results = self._search_facts(args["query"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "list":
            facts = self._list_facts(limit=int(args.get("limit", 50)))
            return json.dumps({"success": True, "facts": facts, "count": len(facts)})
        if action == "probe":
            results = self._probe_entity(args["entity"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "related":
            results = self._related_entity(args["entity"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "reason":
            results = self._reason_entities(args.get("entities", []), limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "contradict":
            results = self._contradict(limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        return json.dumps({"success": False, "error": f"unknown action {action}"})

    def _handle_fact_store(self, args: dict[str, Any]) -> str:
        action = args["action"]
        if action == "add":
            fid = self._add_fact(
                args["content"], category=args.get("category", "general"), tags=args.get("tags", ""),
            )
            return json.dumps({"success": True, "fact_id": fid, "status": "added"})
        if action == "search":
            results = self._search_facts(args["query"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "list":
            facts = self._list_facts(limit=int(args.get("limit", 50)))
            return json.dumps({"success": True, "facts": facts, "count": len(facts)})
        if action == "probe":
            results = self._probe_entity(args["entity"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "related":
            results = self._related_entity(args["entity"], limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "reason":
            results = self._reason_entities(args.get("entities", []), limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "contradict":
            results = self._contradict(limit=int(args.get("limit", 10)))
            return json.dumps({"success": True, "results": results, "count": len(results)})
        if action == "update":
            ok = self._update_fact(args)
            return json.dumps({"success": True, "updated": ok})
        if action == "remove":
            ok = self._remove_fact(int(args["fact_id"]))
            return json.dumps({"success": True, "removed": ok})
        return json.dumps({"success": False, "error": f"unknown action {action}"})

    def _handle_fact_feedback(self, args: dict[str, Any]) -> str:
        fid = int(args["fact_id"])
        delta = 0.1 if args["action"] == "helpful" else -0.1
        row = self._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
        if row is None:
            return json.dumps({"success": False, "error": "fact not found"})
        new_trust = max(0.0, min(1.0, row["trust_score"] + delta))
        self._conn.execute("UPDATE facts SET trust_score=? WHERE id=?", (new_trust, fid))
        self._conn.commit()
        return json.dumps({"success": True, "fact_id": fid, "trust_score": new_trust})

    # --- storage helpers ---

    def _add_fact(self, content: str, *, category: str, tags: str) -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO facts(content, category, tags, trust_score, created_at, last_hit_at) VALUES (?,?,?,?,?,?)",
            (content, category, tags, self._default_trust, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def _search_facts(self, query: str, *, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (limit * 5,)
        ).fetchall()
        # 用 MemoryRetriever 做相关性打分（签名: retrieve(query, entries) -> [(entry, score)]）
        entries = [r["content"] for r in rows]
        scored = self._retriever.retrieve(query, entries)
        if not scored:
           return []
        content_to_row = {r["content"]: r for r in rows}
        result_rows = [content_to_row[entry] for entry, _ in scored[:limit]]
        return [self._row_to_fact(r) for r in result_rows]

    def _list_facts(self, *, limit: int = 50) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _probe_entity(self, entity: str, *, limit: int) -> list[dict]:
        rows = self._conn.execute(
            """SELECT f.* FROM facts f JOIN entities e ON e.fact_id=f.id
            WHERE e.name=? ORDER BY f.created_at DESC LIMIT ?""",
            (entity, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _related_entity(self, entity: str, *, limit: int) -> list[dict]:
        rows = self._conn.execute(
            """SELECT DISTINCT f.* FROM facts f
            JOIN entities e1 ON e1.fact_id=f.id
            JOIN entities e2 ON e2.name=e1.name
            WHERE e2.name=? LIMIT ?""",
            (entity, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _reason_entities(self, entities: list[str], *, limit: int) -> list[dict]:
        if not entities:
            return []
        placeholder = ",".join("?" * len(entities))
        rows = self._conn.execute(
            f"""SELECT f.*, COUNT(DISTINCT e.name) AS hit FROM facts f
            JOIN entities e ON e.fact_id=f.id
            WHERE e.name IN ({placeholder})
            GROUP BY f.id HAVING hit >= 2 ORDER BY hit DESC LIMIT ?""",
            (*entities, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _contradict(self, *, limit: int) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (limit * 3,)).fetchall()
        results = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                # MemoryRetriever.jaccard 静态方法（T5 新增）
                score = MemoryRetriever.jaccard(a["content"], b["content"])
                if 0.2 < score < 0.6:
                    results.append({"a": self._row_to_fact(a), "b": self._row_to_fact(b), "overlap": score})
                    if len(results) >= limit:
                        return results
        return results

    def _update_fact(self, args: dict[str, Any]) -> bool:
        fid = int(args["fact_id"])
        sets, params = [], []
        if "content" in args: sets.append("content=?"); params.append(args["content"])
        if "category" in args: sets.append("category=?"); params.append(args["category"])
        if "tags" in args: sets.append("tags=?"); params.append(args["tags"])
        if "trust_delta" in args:
            row = self._conn.execute("SELECT trust_score FROM facts WHERE id=?", (fid,)).fetchone()
            if row is None:
                return False
            new_trust = max(0.0, min(1.0, row["trust_score"] + float(args["trust_delta"])))
            sets.append("trust_score=?"); params.append(new_trust)
        if not sets:
            return False
        params.append(fid)
        cur = self._conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id=?", params)
        self._conn.commit()
        return cur.rowcount > 0

    def _remove_fact(self, fid: int) -> bool:
        cur = self._conn.execute("DELETE FROM facts WHERE id=?", (fid,))
        self._conn.commit()
        return cur.rowcount > 0

    def _row_to_fact(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "content": row["content"], "category": row["category"],
            "tags": row["tags"], "trust_score": row["trust_score"],
            "created_at": row["created_at"], "last_hit_at": row["last_hit_at"],
        }

    def _score(self, trust: float, last_hit_at: float, now: float) -> float:
        if self._decay_half_life <= 0:
            return trust
        age = now - last_hit_at
        decay = 0.5 ** (age / self._decay_half_life)
        return trust * decay
