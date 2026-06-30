#!/usr/bin/env python3
"""
Memory System for 3am-claude — asyncio-native, project-scoped, MCP-ready.

Storage: single SQLite DB (default ~/.local/share/3am-claude/memory.db)
  memories     — id, content, project_id, session_id, universe, priority, tags,
                  timestamp, ttl_expires, cluster_id, access_count, last_accessed
  clusters     — id, theme, priority, last_update, torque_mass, center_vector, memory_refs
  memory_links — source_id, target_id, weight, link_type, created_at
  fts_memories — memory_id UNINDEXED, content  (FTS5, rebuilt on startup)
  vec_memories — memory_id TEXT, embedding float[768] distance_metric=cosine
  meta         — key, value

Changes from 3am-AI memory.py:
  - asyncio.Lock + single connection (no threading)
  - project_id scoping (not user_id); NULL = general/shared
  - session_id field for targeted episodic wipe
  - ttl_expires field for automatic TTL expiry
  - No local LLM calls — Claude does classification, passes facts to store_memory
  - PPR enforces lane topology: blocks Project A → Project B walks
  - Keyring key management (caller passes DataEncryptor; see mcp_server.py)
"""

import asyncio
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import sqlite_vec

# ── Constants ─────────────────────────────────────────────────────────────────

DEDUP_DISTANCE               = 1.0 - 0.92   # 0.08 — identical fact, skip

# Auto-promotion thresholds (cosine similarity). When a newly stored project
# memory closely matches memories that exist in OTHER projects, the knowledge is
# general rather than project-local — a candidate for the shared general pool.
# Recall never crosses projects (see _is_visible), so store-time cross-project
# similarity is the only viable promotion signal.
PROMOTE_AUTO_COSINE          = 0.90   # >= → auto-promote (near-identical)
PROMOTE_CANDIDATE_COSINE     = 0.82   # >= (and < auto) → queue for confirmation
PROMOTE_MIN_PROJECTS         = 2      # distinct projects the concept must span
PROMOTE_SCAN_LIMIT           = 15     # nearest neighbors to inspect at store time

CLUSTER_HEALTH_THRESHOLD     = 0.35
MESSAGE_RETENTION_THRESHOLD  = 0.35
MIN_MEMORIES_FOR_CLUSTERING  = 5
NEW_CLUSTER_SIMILARITY_THRESHOLD = 0.5
LANE_MIN_SIM  = 0.55
LANE_MAX_SIM  = 0.92
LANE_MAX_K    = 8
VALID_UNIVERSES = {"episodic", "declarative", "procedural"}

DECAY_RATES = {5: 0.0005, 4: 0.0005, 3: 0.005, 2: 0.025, 1: 0.1}

UNIVERSE_DECAY_MULTIPLIERS = {
    "episodic":    1.0,   # standard decay
    "declarative": 0.3,   # slow decay — reference knowledge persists
    "procedural":  0.1,   # very slow decay — only replaced by better patterns
}

# Category-aware decay (ported from 3am-AI). Stable facts (who you are, what you
# prefer) resist decay; time-bound facts (a project, an activity) fade normally.
# Applied on top of universe + priority rates. Unknown categories → 1.0.
CATEGORY_DECAY_MULTIPLIERS = {
    "identity":    0.1,   # who you are — near-permanent
    "preferences": 0.2,   # tastes change slowly
    "relationship":0.3,
    "projects":    1.0,   # time-bounded — normal decay
    "activities":  1.0,   # time-bound — normal decay
    "general":     1.0,
}
VALID_CATEGORIES = set(CATEGORY_DECAY_MULTIPLIERS) | {"research", "skill"}

# Superseded memories fade fast (10×) but aren't hard-deleted — audit trail.
SUPERSEDED_DECAY_MULT = 10.0

# Auto conflict detection (ported from 3am-AI): same-category neighbors whose
# distance falls in this band are "close but not identical" → potential conflict.
CONFLICT_DIST_NEAR = DEDUP_DISTANCE          # 0.08 — closer = duplicate
CONFLICT_DIST_FAR  = 1.0 - 0.75              # 0.25 — further = different topic
CONFLICT_SCAN_LIMIT = 20

# Similarity gate for matching a free-text "wrong claim" to an existing memory.
CORRECTION_MATCH_COSINE = 0.65

CHARS_PER_TOKEN = 4  # rough approximation for max_tokens budget

# ── Temporal query detection (time-grounded retrieval) ────────────────────────
_TEMPORAL_PATTERNS = re.compile(
    r"\b(when|what year|what month|what day|what time|how long ago|since when|"
    r"before|after|earlier|later|first time|last time|recently|"
    r"date of|day did|year did|month did)\b",
    re.IGNORECASE,
)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


def _is_temporal_query(query: str) -> bool:
    """True if the question is asking about time ('when did X', 'before Y')."""
    q = query.lower()
    if _TEMPORAL_PATTERNS.search(q):
        return True
    return any(m in q for m in _MONTHS)

# Per-universe retrieval fractions by query type (episodic, declarative, procedural).
# Applied as soft caps on the result set — keeps context balanced for the query intent.
CONTEXT_BIAS = {
    "personal":    (0.55, 0.25, 0.20),
    "factual":     (0.17, 0.63, 0.20),
    "procedural":  (0.20, 0.25, 0.55),
    "balanced":    (0.35, 0.40, 0.25),
}


def _classify_query(query: str) -> str:
    """
    Heuristic classifier for three-universe context allocation.
    Returns: "personal" | "factual" | "procedural" | "balanced"
    """
    q = query.lower()
    personal   = sum(1 for w in ("i ", "my ", " me ", " i'", " we ", "our ", "prefer", "like ", "feel ", "tell me about myself", "what do i") if w in q)
    factual    = sum(1 for w in ("how ", "what is", "explain", "define", "does ", "work ", "what are", "describe", "documentation") if w in q)
    procedural = sum(1 for w in ("when ", "should ", "pattern", "behavior", "behave", "rule ", "always ", "never ", "if i", "next time", "remember to") if w in q)
    mx = max(personal, factual, procedural)
    if mx == 0:
        return "balanced"
    if personal == mx and personal > factual and personal > procedural:
        return "personal"
    if factual == mx and factual > personal and factual > procedural:
        return "factual"
    if procedural == mx and procedural > personal and procedural > factual:
        return "procedural"
    return "balanced"

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "is", "was", "are", "were", "be", "been", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "can", "this", "that", "these", "those", "it", "its", "from",
    "by", "about", "as", "into", "through", "use", "used", "using", "not",
})


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    id: str
    content: str
    project_id: Optional[str]    # None = general/shared
    session_id: Optional[str]    # for targeted episodic wipe
    universe: str                # episodic | declarative | procedural
    priority: int
    tags: list
    timestamp: float             # store time (when this was written)
    ttl_expires: Optional[float] # None = permanent
    cluster_id: Optional[str]
    access_count: int
    last_accessed: float
    event_time: Optional[float] = None   # when the fact is ABOUT (for temporal recall); None → use timestamp
    category: str = "general"            # identity|preferences|projects|activities|… (decay-aware)
    superseded_by: Optional[str] = None  # id of the memory that replaced this one


@dataclass
class MemoryCluster:
    id: str
    theme: str
    center_vector: list
    memory_refs: list
    priority: int
    last_update: float
    torque_mass: float = 0.0


# ── Embedding model ───────────────────────────────────────────────────────────

class EmbeddingModel:
    """Lazy-loaded sentence-transformer. CPU only. Stays loaded in daemon."""

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5"):
        self.model_name = model_name
        self._model = None
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self):
        import torch
        from sentence_transformers import SentenceTransformer
        kwargs = dict(
            trust_remote_code=True,
            device="cpu",
            model_kwargs={"torch_dtype": torch.float16},
        )
        try:
            return SentenceTransformer(self.model_name, local_files_only=True, **kwargs)
        except Exception:
            return SentenceTransformer(self.model_name, **kwargs)

    async def _ensure_loaded(self):
        if self._model is None:
            async with self._get_lock():
                if self._model is None:
                    loop = asyncio.get_event_loop()
                    self._model = await loop.run_in_executor(None, self._load_sync)

    async def embed(self, text: str) -> list:
        await self._ensure_loaded()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._model.encode(
                text, convert_to_numpy=True, normalize_embeddings=True
            ).tolist(),
        )

    async def embed_batch(self, texts: list) -> list:
        await self._ensure_loaded()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._model.encode(
                texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=8
            ).tolist(),
        )

    def is_loaded(self) -> bool:
        return self._model is not None


_SHARED_EMBEDDER: Optional[EmbeddingModel] = None


def get_shared_embedder() -> EmbeddingModel:
    global _SHARED_EMBEDDER
    if _SHARED_EMBEDDER is None:
        _SHARED_EMBEDDER = EmbeddingModel()
    return _SHARED_EMBEDDER


def _rrf_merge(list_a: list, list_b: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion — merge two ranked lists."""
    scores: dict = {}
    for rank, item in enumerate(list_a):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    for rank, item in enumerate(list_b):
        scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda x: -scores[x])


# ── Memory System ─────────────────────────────────────────────────────────────

class MemorySystem:
    """
    Persistent memory for 3am-claude.
    Single DB, project_id scoping, asyncio-native.
    """

    def __init__(
        self,
        db_path: Path,
        encryptor=None,           # Optional[DataEncryptor] from data_security.py
        llm_url: Optional[str] = None,   # optional async cluster theme generation
        clustering_config: Optional[dict] = None,
    ):
        self.db_file = Path(db_path).expanduser()
        self.encryptor = encryptor
        self.llm_url = llm_url
        self.clustering_config_dict = clustering_config or {}

        # Auto-promotion config (overridable via config.json)
        _cfg = clustering_config or {}
        self.auto_promote            = _cfg.get("auto_promote", True)
        self.promote_auto_cosine     = float(_cfg.get("promote_auto_cosine", PROMOTE_AUTO_COSINE))
        self.promote_candidate_cosine = float(_cfg.get("promote_candidate_cosine", PROMOTE_CANDIDATE_COSINE))
        self.promote_min_projects    = int(_cfg.get("promote_min_projects", PROMOTE_MIN_PROJECTS))
        self.conflict_detection      = _cfg.get("conflict_detection", True)

        self.memories: dict[str, MemoryEntry] = {}
        self.clusters: dict[str, MemoryCluster] = {}
        self._clustering_dirty = False
        self._lanes_ready: Optional[bool] = None
        self._lock: Optional[asyncio.Lock] = None
        self._conn: Optional[sqlite3.Connection] = None
        self.embedder = get_shared_embedder()

        self.stats = {
            "total_memories": 0,
            "active_clusters": 0,
            "last_cleanup": time.time(),
            "last_clustering": 0,
        }

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._conn = conn
        return self._conn

    def initialize(self):
        """Synchronous init — call from lifespan at startup."""
        conn = self._get_conn()
        self._init_db(conn)
        self._load_from_db(conn)
        self._cleanup_expired(conn)

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_db(self, conn: sqlite3.Connection):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id            TEXT PRIMARY KEY,
                content       TEXT NOT NULL,
                project_id    TEXT,
                session_id    TEXT,
                universe      TEXT NOT NULL DEFAULT 'episodic',
                priority      INTEGER NOT NULL DEFAULT 3,
                tags          TEXT NOT NULL DEFAULT '[]',
                timestamp     REAL NOT NULL,
                ttl_expires   REAL,
                cluster_id    TEXT,
                access_count  INTEGER NOT NULL DEFAULT 0,
                last_accessed REAL NOT NULL DEFAULT 0,
                event_time    REAL,
                category      TEXT NOT NULL DEFAULT 'general',
                superseded_by TEXT
            );

            CREATE TABLE IF NOT EXISTS clusters (
                id            TEXT PRIMARY KEY,
                theme         TEXT NOT NULL,
                priority      INTEGER NOT NULL DEFAULT 3,
                last_update   REAL NOT NULL,
                torque_mass   REAL NOT NULL DEFAULT 0.0,
                center_vector BLOB NOT NULL,
                memory_refs   TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS memory_links (
                source_id  TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                weight     REAL NOT NULL DEFAULT 1.0,
                link_type  TEXT NOT NULL DEFAULT 'semantic',
                created_at REAL NOT NULL,
                PRIMARY KEY (source_id, target_id)
            );

            -- Auto-promotion audit log: every project→general promotion, reversible.
            CREATE TABLE IF NOT EXISTS promotions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id        TEXT NOT NULL,
                original_project TEXT,
                projects         TEXT NOT NULL DEFAULT '[]',
                match_ids        TEXT NOT NULL DEFAULT '[]',
                best_cosine      REAL NOT NULL DEFAULT 0,
                timestamp        REAL NOT NULL,
                trigger          TEXT NOT NULL DEFAULT 'auto',
                reverted         INTEGER NOT NULL DEFAULT 0
            );

            -- Queue of borderline promotion candidates awaiting confirmation.
            CREATE TABLE IF NOT EXISTS promotion_candidates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id   TEXT NOT NULL,
                match_ids   TEXT NOT NULL DEFAULT '[]',
                projects    TEXT NOT NULL DEFAULT '[]',
                best_cosine REAL NOT NULL DEFAULT 0,
                timestamp   REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_links_source   ON memory_links(source_id);
            CREATE INDEX IF NOT EXISTS idx_links_target   ON memory_links(target_id);
            CREATE INDEX IF NOT EXISTS idx_mem_project    ON memories(project_id);
            CREATE INDEX IF NOT EXISTS idx_mem_session    ON memories(session_id);
            CREATE INDEX IF NOT EXISTS idx_mem_universe   ON memories(universe);
            CREATE INDEX IF NOT EXISTS idx_cand_status    ON promotion_candidates(status);
        """)

        # Migrate older DBs: add columns introduced after first release.
        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        for col, ddl in (
            ("event_time",    "ALTER TABLE memories ADD COLUMN event_time REAL"),
            ("category",      "ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'general'"),
            ("superseded_by", "ALTER TABLE memories ADD COLUMN superseded_by TEXT"),
        ):
            if col not in existing_cols:
                conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_superseded ON memories(superseded_by)")
        conn.commit()

        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                memory_id TEXT,
                embedding float[768] distance_metric=cosine
            )
        """)

        # FTS5 — drop + recreate on every startup to stay in sync
        conn.execute("DROP TABLE IF EXISTS fts_memories")
        conn.execute("""
            CREATE VIRTUAL TABLE fts_memories USING fts5(
                memory_id UNINDEXED,
                content,
                tokenize='porter ascii'
            )
        """)
        conn.execute("""
            INSERT INTO fts_memories(memory_id, content)
            SELECT id, content FROM memories
        """)
        conn.commit()

    # ── Load ──────────────────────────────────────────────────────────────────

    def _load_from_db(self, conn: sqlite3.Connection):
        enc = self.encryptor
        _d = (lambda s: enc.decrypt_str(s)) if (enc and enc.config.enabled) else (lambda s: s)

        for row in conn.execute(
            "SELECT id, content, project_id, session_id, universe, priority, tags, "
            "timestamp, ttl_expires, cluster_id, access_count, last_accessed, "
            "event_time, category, superseded_by FROM memories"
        ):
            try:
                tags = json.loads(row["tags"] or "[]")
            except Exception:
                tags = []
            self.memories[row["id"]] = MemoryEntry(
                id=row["id"],
                content=_d(row["content"]),
                project_id=row["project_id"],
                session_id=row["session_id"],
                universe=row["universe"] or "episodic",
                priority=row["priority"],
                tags=tags,
                timestamp=row["timestamp"],
                ttl_expires=row["ttl_expires"],
                cluster_id=row["cluster_id"],
                access_count=row["access_count"] or 0,
                last_accessed=row["last_accessed"] or 0.0,
                event_time=row["event_time"],
                category=row["category"] or "general",
                superseded_by=row["superseded_by"],
            )

        for row in conn.execute(
            "SELECT id, theme, priority, last_update, torque_mass, center_vector, memory_refs FROM clusters"
        ):
            center_vector = (
                np.frombuffer(row["center_vector"], dtype=np.float32).tolist()
                if row["center_vector"] else []
            )
            enc = self.encryptor
            _d = (lambda s: enc.decrypt_str(s)) if (enc and enc.config.enabled) else (lambda s: s)
            self.clusters[row["id"]] = MemoryCluster(
                id=row["id"],
                theme=_d(row["theme"]),
                center_vector=center_vector,
                memory_refs=json.loads(row["memory_refs"]),
                priority=row["priority"],
                last_update=row["last_update"],
                torque_mass=row["torque_mass"],
            )

        row = conn.execute("SELECT value FROM meta WHERE key='stats'").fetchone()
        if row:
            try:
                self.stats.update(json.loads(row["value"]))
            except Exception:
                pass

        self.stats["total_memories"] = len(self.memories)
        self.stats["active_clusters"] = len(self.clusters)
        print(f"[Memory] Loaded {len(self.memories)} memories, {len(self.clusters)} clusters")

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _cleanup_expired(self, conn: sqlite3.Connection):
        """Remove TTL-expired and decayed memories."""
        current_time = time.time()
        to_delete = []

        for mid, entry in self.memories.items():
            if entry.ttl_expires and current_time > entry.ttl_expires:
                to_delete.append(mid)
                continue
            if self._calculate_retention(entry, current_time) <= MESSAGE_RETENTION_THRESHOLD:
                to_delete.append(mid)

        for mid in to_delete:
            entry = self.memories.pop(mid, None)
            self._delete_memory_conn(conn, mid)
            if entry and entry.cluster_id and entry.cluster_id in self.clusters:
                self.clusters[entry.cluster_id].memory_refs = [
                    r for r in self.clusters[entry.cluster_id].memory_refs if r != mid
                ]

        to_remove = [cid for cid, c in self.clusters.items() if not c.memory_refs]
        for cid in to_remove:
            self.clusters.pop(cid)
            conn.execute("DELETE FROM clusters WHERE id=?", (cid,))

        if to_delete or to_remove:
            conn.commit()
            print(f"[Memory] Cleanup: removed {len(to_delete)} memories, {len(to_remove)} clusters")

        self.stats["total_memories"] = len(self.memories)
        self.stats["active_clusters"] = len(self.clusters)
        self.stats["last_cleanup"] = current_time

    # ── Retention / decay ────────────────────────────────────────────────────

    def _calculate_retention(self, entry: MemoryEntry, current_time: float) -> float:
        age_hours = (current_time - entry.timestamp) / 3600
        base_rate = DECAY_RATES.get(entry.priority, DECAY_RATES[1])
        universe_mult = UNIVERSE_DECAY_MULTIPLIERS.get(entry.universe, 1.0)
        category_mult = CATEGORY_DECAY_MULTIPLIERS.get(entry.category, 1.0)
        access_resistance = 1.0 / (1.0 + entry.access_count)
        # Superseded memories fade fast — replaced by a newer/corrected fact.
        superseded_mult = SUPERSEDED_DECAY_MULT if entry.superseded_by else 1.0
        effective_rate = (base_rate * universe_mult * category_mult
                          * access_resistance * superseded_mult)
        return math.exp(-effective_rate * age_hours)

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _delete_memory_conn(self, conn: sqlite3.Connection, memory_id: str):
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        conn.execute("DELETE FROM vec_memories WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM fts_memories WHERE memory_id=?", (memory_id,))
        conn.execute(
            "DELETE FROM memory_links WHERE source_id=? OR target_id=?",
            (memory_id, memory_id),
        )

    async def _write_memory(self, entry: MemoryEntry, embedding: list):
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        enc = self.encryptor
        _e = (lambda s: enc.encrypt_str(s)) if (enc and enc.config.enabled) else (lambda s: s)
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute("""
                INSERT OR REPLACE INTO memories
                (id, content, project_id, session_id, universe, priority, tags,
                 timestamp, ttl_expires, cluster_id, access_count, last_accessed,
                 event_time, category, superseded_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id, _e(entry.content), entry.project_id, entry.session_id,
                entry.universe, entry.priority, json.dumps(entry.tags),
                entry.timestamp, entry.ttl_expires, entry.cluster_id,
                entry.access_count, entry.last_accessed,
                entry.event_time, entry.category, entry.superseded_by,
            ))
            conn.execute("DELETE FROM vec_memories WHERE memory_id=?", (entry.id,))
            conn.execute(
                "INSERT INTO vec_memories(memory_id, embedding) VALUES (?, ?)",
                (entry.id, emb_bytes),
            )
            conn.execute(
                "INSERT OR REPLACE INTO fts_memories(memory_id, content) VALUES (?, ?)",
                (entry.id, entry.content),  # FTS: always plain text
            )
            conn.commit()

    async def _write_cluster(self, cluster: MemoryCluster):
        center_bytes = np.array(cluster.center_vector, dtype=np.float32).tobytes()
        enc = self.encryptor
        theme = enc.encrypt_str(cluster.theme) if (enc and enc.config.enabled) else cluster.theme
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute("""
                INSERT OR REPLACE INTO clusters
                (id, theme, priority, last_update, torque_mass, center_vector, memory_refs)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                cluster.id, theme, cluster.priority,
                cluster.last_update, cluster.torque_mass,
                center_bytes, json.dumps(cluster.memory_refs),
            ))
            conn.commit()

    # ── Semantic lanes ────────────────────────────────────────────────────────

    async def _build_semantic_links(
        self, new_id: str, embedding: list, universe: str
    ) -> int:
        """Find K nearest neighbors and create semantic lanes."""
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        conn = self._get_conn()

        rows = conn.execute("""
            SELECT sub.memory_id, sub.distance, m.universe
            FROM (
                SELECT memory_id, distance
                FROM vec_memories
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            ) sub
            JOIN memories m ON m.id = sub.memory_id
            WHERE sub.memory_id != ?
        """, (emb_bytes, LANE_MAX_K + 2, new_id)).fetchall()

        links = []
        now = time.time()
        for row in rows:
            sim = 1.0 - row["distance"]
            if LANE_MIN_SIM <= sim < LANE_MAX_SIM:
                neighbor_universe = row["universe"] or "episodic"
                same_universe = (neighbor_universe == universe)
                links.append((new_id, row["memory_id"], sim, "semantic", now))
                if same_universe:
                    links.append((row["memory_id"], new_id, sim, "semantic", now))

        if links:
            async with self._get_lock():
                conn.executemany("""
                    INSERT OR REPLACE INTO memory_links
                    (source_id, target_id, weight, link_type, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, links)
                conn.commit()
            self._lanes_ready = True

        return len(links)

    # ── Project visibility ────────────────────────────────────────────────────

    def _is_visible(self, memory_id: str, project_id: Optional[str]) -> bool:
        """
        A memory is visible to a query if:
          - it is general (project_id=None): always visible
          - it belongs to the current project: visible
          - project_id=None (no project context): only general memories visible
        """
        entry = self.memories.get(memory_id)
        if not entry:
            return False
        if entry.project_id is None:
            return True   # general: always visible
        if project_id is None:
            return False  # querying without project: only general visible
        return entry.project_id == project_id

    def _lane_topology_ok(
        self,
        src_project: Optional[str],
        dst_project: Optional[str],
    ) -> bool:
        """
        Lane topology enforcement:
          - same project ↔ same project: allowed
          - any → general (None): allowed
          - general → any: allowed
          - project A → project B (both non-None, different): BLOCKED
        """
        if src_project is None or dst_project is None:
            return True
        return src_project == dst_project

    # ── Core API ──────────────────────────────────────────────────────────────

    async def store_memory(
        self,
        content: str,
        project_id: Optional[str],
        universe: str,
        priority: int,
        tags: Optional[list] = None,
        ttl_days: Optional[int] = None,
        session_id: Optional[str] = None,
        event_time: Optional[float] = None,
        category: str = "general",
        skip_dedup: bool = False,
    ) -> dict:
        """
        Store a memory. Returns {id, cluster_theme}.
        Deduplicates within project scope (unless skip_dedup — used by supersede/
        correction paths, where the new fact may be near-identical to the old one
        it replaces and must not be merged back into it).

        event_time: epoch seconds the fact is ABOUT (e.g. when an event happened),
                    for time-grounded recall. None → falls back to store time.
        category:   identity|preferences|projects|activities|general|… — drives
                    category-aware decay (stable facts resist decay).
        """
        if universe not in VALID_UNIVERSES:
            universe = "episodic"
        if category not in VALID_CATEGORIES:
            category = "general"
        priority = max(1, min(5, priority))
        tags = tags or []

        embedding = await self.embedder.embed(content)
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        conn = self._get_conn()

        # Dedup check: nearest neighbor within distance threshold
        # sqlite-vec requires LIMIT on the vec0 table directly (inside subquery)
        row = None if skip_dedup else conn.execute("""
            SELECT sub.memory_id, sub.distance, m.project_id
            FROM (
                SELECT memory_id, distance
                FROM vec_memories
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT 1
            ) sub
            JOIN memories m ON m.id = sub.memory_id
        """, (emb_bytes,)).fetchone()

        if row and row["distance"] < DEDUP_DISTANCE:
            # Only dedup within the same project scope
            existing_proj = row["project_id"]
            scope_match = (existing_proj == project_id) or (
                existing_proj is None and project_id is None
            )
            if scope_match:
                existing = self.memories.get(row["memory_id"])
                cluster_theme = "General"
                if existing and existing.cluster_id and existing.cluster_id in self.clusters:
                    cluster_theme = self.clusters[existing.cluster_id].theme
                return {"id": row["memory_id"], "cluster_theme": cluster_theme, "deduped": True}

        now = time.time()
        entry_id = f"mem_{time.time_ns()}"
        ttl_expires = now + ttl_days * 86400 if ttl_days else None

        entry = MemoryEntry(
            id=entry_id,
            content=content,
            project_id=project_id,
            session_id=session_id,
            universe=universe,
            priority=priority,
            tags=tags,
            timestamp=now,
            ttl_expires=ttl_expires,
            cluster_id=None,
            access_count=0,
            last_accessed=0.0,
            event_time=event_time,
            category=category,
            superseded_by=None,
        )
        self.memories[entry_id] = entry
        await self._write_memory(entry, embedding)
        await self._build_semantic_links(entry_id, embedding, universe)

        # Incremental cluster assignment
        cluster_theme = "Unclustered"
        if self.clusters:
            emb_arr = np.array(embedding)
            best_cluster = None
            best_sim = NEW_CLUSTER_SIMILARITY_THRESHOLD
            for cluster in self.clusters.values():
                sim = float(np.dot(emb_arr, np.array(cluster.center_vector)))
                if sim > best_sim:
                    best_sim = sim
                    best_cluster = cluster
            if best_cluster:
                entry.cluster_id = best_cluster.id
                if entry_id not in best_cluster.memory_refs:
                    best_cluster.memory_refs.append(entry_id)
                best_cluster.last_update = now
                async with self._get_lock():
                    conn.execute(
                        "UPDATE memories SET cluster_id=? WHERE id=?",
                        (best_cluster.id, entry_id),
                    )
                    conn.commit()
                await self._write_cluster(best_cluster)
                cluster_theme = best_cluster.theme

        self._clustering_dirty = True
        self.stats["total_memories"] = len(self.memories)

        result = {"id": entry_id, "cluster_theme": cluster_theme}

        # Hybrid auto-promotion: if this project memory mirrors knowledge already
        # present in other projects, promote it (high confidence) or queue it
        # (borderline). Only declarative/procedural — episodic is transient.
        if entry.project_id is not None and universe in ("declarative", "procedural"):
            promo = await self._evaluate_promotion(entry, embedding)
            if promo:
                result["promotion"] = promo

        return result

    # ── Auto-promotion ──────────────────────────────────────────────────────────

    async def _evaluate_promotion(self, entry: "MemoryEntry", embedding) -> Optional[dict]:
        """
        Decide whether a freshly-stored project memory should move to the general
        pool, based on how closely it matches memories from OTHER projects.

          best cross-project cosine >= promote_auto_cosine, spanning >= N projects
            → auto-promote (if auto_promote enabled) else queue
          >= promote_candidate_cosine
            → queue as a candidate for confirmation

        Returns a small dict describing the action, or None if nothing fired.
        """
        conn = self._get_conn()
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        rows = conn.execute("""
            SELECT sub.memory_id, sub.distance, m.project_id
            FROM (
                SELECT memory_id, distance
                FROM vec_memories
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
            ) sub
            JOIN memories m ON m.id = sub.memory_id
        """, (emb_bytes, PROMOTE_SCAN_LIMIT)).fetchall()

        # Cross-project neighbors only (different non-null project than this entry).
        matches = []  # (memory_id, cosine, project_id)
        for r in rows:
            if r["memory_id"] == entry.id:
                continue
            proj = r["project_id"]
            if proj is None or proj == entry.project_id:
                continue
            cosine = 1.0 - r["distance"]
            if cosine >= self.promote_candidate_cosine:
                matches.append((r["memory_id"], cosine, proj))

        if not matches:
            return None

        best_cosine = max(c for _, c, _ in matches)
        distinct_projects = {entry.project_id} | {p for _, _, p in matches}
        if len(distinct_projects) < self.promote_min_projects:
            return None

        match_ids = [mid for mid, _, _ in matches]
        projects = sorted(distinct_projects)
        now = time.time()

        if best_cosine >= self.promote_auto_cosine and self.auto_promote:
            # Auto-promote this entry to the general pool. We move only this entry
            # (not the matched originals) — fully reversible via revert_promotion.
            original_project = entry.project_id
            entry.project_id = None
            async with self._get_lock():
                conn.execute("UPDATE memories SET project_id=NULL WHERE id=?", (entry.id,))
                conn.execute("""
                    INSERT INTO promotions
                        (memory_id, original_project, projects, match_ids,
                         best_cosine, timestamp, trigger, reverted)
                    VALUES (?, ?, ?, ?, ?, ?, 'auto', 0)
                """, (entry.id, original_project, json.dumps(projects),
                      json.dumps(match_ids), best_cosine, now))
                conn.commit()
            return {
                "action": "promoted",
                "best_cosine": round(best_cosine, 4),
                "projects": projects,
            }

        # Borderline (or auto disabled): queue for confirmation, de-duping pending.
        async with self._get_lock():
            existing = conn.execute(
                "SELECT id FROM promotion_candidates WHERE memory_id=? AND status='pending'",
                (entry.id,),
            ).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO promotion_candidates
                        (memory_id, match_ids, projects, best_cosine, timestamp, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                """, (entry.id, json.dumps(match_ids), json.dumps(projects),
                      best_cosine, now))
                conn.commit()
        return {
            "action": "queued",
            "best_cosine": round(best_cosine, 4),
            "projects": projects,
        }

    async def query_memory(
        self,
        query: str,
        project_id: Optional[str],
        limit: int = 10,
        max_tokens: int = 2000,
        min_score: float = 0.0,
        min_cosine: float = 0.5,
    ) -> list:
        """
        Hybrid FTS5 + vec retrieval with PPR expansion.
        Returns project-scoped + general memories only.
        Never returns other projects' memories.
        """
        if not self.memories:
            return []

        conn = self._get_conn()
        embedding = await self.embedder.embed(query)
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()

        # Time-grounded retrieval: temporal questions ("when did X") widen the
        # retrieval beam — the supporting turn is often outside a narrow top-k.
        temporal = _is_temporal_query(query)
        cand_limit = 40 if temporal else 20
        seed_keep  = 16 if temporal else 8

        # FTS5 seeds — drop stopwords + time-question words so real terms drive
        # the match; post-filter by project visibility and supersession.
        fts_seeds: list = []
        fts_terms = [w for w in re.findall(r"[A-Za-z0-9']+", query)
                     if len(w) > 2 and w.lower() not in _STOPWORDS]
        fts_query = " OR ".join(f'"{w}"' for w in fts_terms[:10])
        if fts_query:
            try:
                fts_rows = conn.execute("""
                    SELECT memory_id, rank FROM fts_memories
                    WHERE fts_memories MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, cand_limit)).fetchall()
                fts_seeds = [
                    r["memory_id"] for r in fts_rows
                    if r["memory_id"] in self.memories
                    and self._is_visible(r["memory_id"], project_id)
                    and not self.memories[r["memory_id"]].superseded_by
                ][:seed_keep]
            except Exception:
                pass

        # Vec seeds — post-filter by project visibility and supersession
        vec_rows = conn.execute("""
            SELECT memory_id, distance FROM vec_memories
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
        """, (emb_bytes, cand_limit)).fetchall()
        vec_seeds = [
            r["memory_id"] for r in vec_rows
            if r["memory_id"] in self.memories
            and self._is_visible(r["memory_id"], project_id)
            and not self.memories[r["memory_id"]].superseded_by
        ][:seed_keep]

        # Cosine seed gate: if the best cosine similarity is below min_cosine,
        # PPR would only amplify noise — bail out before graph expansion.
        if min_cosine > 0.0:
            _best_distance = next(
                (r["distance"] for r in vec_rows
                 if r["memory_id"] in self.memories
                 and self._is_visible(r["memory_id"], project_id)),
                1.0,
            )
            if (1.0 - _best_distance) < min_cosine and not fts_seeds:
                return []

        seed_ids = _rrf_merge(fts_seeds, vec_seeds, k=60)[:limit]
        if not seed_ids:
            return []

        # PPR expansion
        if self._lanes_ready is None:
            self._lanes_ready = conn.execute(
                "SELECT COUNT(*) FROM memory_links LIMIT 1"
            ).fetchone()[0] > 0

        ppr_topk = limit + (15 if temporal else 6)
        if self._lanes_ready:
            ranked = self._ppr_expand(seed_ids, project_id, top_k=ppr_topk)
        else:
            ranked = [(sid, 1.0) for sid in seed_ids]

        # Increment access_count
        now = time.time()
        recalled_ids = [mid for mid, _ in ranked]
        if recalled_ids:
            async with self._get_lock():
                conn.executemany(
                    "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    [(now, mid) for mid in recalled_ids],
                )
                conn.commit()
            for mid in recalled_ids:
                entry = self.memories.get(mid)
                if entry:
                    entry.access_count += 1
                    entry.last_accessed = now

        # Build result list within token budget, respecting per-universe caps
        query_type = _classify_query(query)
        ep_frac, dec_frac, proc_frac = CONTEXT_BIAS[query_type]
        universe_caps = {
            "episodic":    max(1, round(limit * ep_frac)),
            "declarative": max(1, round(limit * dec_frac)),
            "procedural":  max(1, round(limit * proc_frac)),
        }
        universe_counts: dict = {"episodic": 0, "declarative": 0, "procedural": 0}

        results = []
        chosen: set = set()
        token_count = 0

        def _emit(entry, score) -> bool:
            """Append a result; returns False if the token budget is exhausted."""
            nonlocal token_count
            cluster_theme = "General"
            if entry.cluster_id and entry.cluster_id in self.clusters:
                cluster_theme = self.clusters[entry.cluster_id].theme
            entry_tokens = max(1, len(entry.content) // CHARS_PER_TOKEN)
            if token_count + entry_tokens > max_tokens and results:
                return False
            token_count += entry_tokens
            chosen.add(entry.id)
            results.append({
                "id": entry.id,
                "content": entry.content,
                "universe": entry.universe,
                "score": round(score, 4),
                "cluster_theme": cluster_theme,
                "project_id": entry.project_id,
                "tags": entry.tags,
                "priority": entry.priority,
                "category": entry.category,
                "event_time": entry.event_time,
                "timestamp": entry.timestamp,
            })
            return True

        # Pass 1: respect per-universe caps (keeps context balanced for intent).
        budget_left = True
        for memory_id, score in ranked:
            if score < min_score:
                continue
            entry = self.memories.get(memory_id)
            if not entry or entry.superseded_by:   # newer/corrected facts win
                continue
            u = entry.universe
            if universe_counts.get(u, 0) >= universe_caps.get(u, limit):
                continue
            if not _emit(entry, score):
                budget_left = False
                break
            universe_counts[u] = universe_counts.get(u, 0) + 1
            if len(results) >= limit:
                break

        # Pass 2 (soft caps): backfill unused slots ignoring caps — a temporal
        # question on a declarative-only corpus shouldn't be throttled to the
        # declarative cap and waste budget.
        if budget_left and len(results) < limit:
            for memory_id, score in ranked:
                if len(results) >= limit:
                    break
                if score < min_score or memory_id in chosen:
                    continue
                entry = self.memories.get(memory_id)
                if not entry or entry.superseded_by:
                    continue
                if not _emit(entry, score):
                    break

        return results

    async def get_session_summary(self, project_id: Optional[str]) -> list:
        """
        Return cluster themes for current project + general clusters.
        [{theme, size, sample_memories, scope: "project"|"general"}]
        """
        result = []
        for cluster in sorted(self.clusters.values(), key=lambda c: -c.torque_mass):
            refs = [self.memories.get(mid) for mid in cluster.memory_refs if mid in self.memories]
            if not refs:
                continue

            project_refs = (
                [e for e in refs if e.project_id == project_id]
                if project_id else []
            )
            general_refs = [e for e in refs if e.project_id is None]

            if project_id and project_refs:
                scope = "project"
                visible_refs = project_refs
            elif general_refs:
                scope = "general"
                visible_refs = general_refs
            else:
                continue

            sample = sorted(visible_refs, key=lambda e: -e.priority)[:3]
            result.append({
                "theme": cluster.theme,
                "size": len(visible_refs),
                "sample_memories": [e.content[:120] for e in sample],
                "scope": scope,
                "cluster_id": cluster.id,
            })

        return result

    async def promote_to_general(self, memory_id: str) -> dict:
        """Promote a project-specific memory to general (project_id=NULL)."""
        entry = self.memories.get(memory_id)
        if not entry:
            return {"ok": False, "error": "memory_not_found"}
        entry.project_id = None
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute("UPDATE memories SET project_id=NULL WHERE id=?", (memory_id,))
            conn.commit()
        return {"ok": True}

    # ── Promotion management ──────────────────────────────────────────────────

    def _dec(self, s: str) -> str:
        enc = self.encryptor
        if enc and getattr(enc, "config", None) and enc.config.enabled:
            try:
                return enc.decrypt_str(s)
            except Exception:
                return s
        return s

    def _mem_preview(self, memory_id: str, n: int = 120) -> Optional[dict]:
        e = self.memories.get(memory_id)
        if not e:
            return None
        content = e.content
        return {
            "id": e.id,
            "preview": content[:n] + ("…" if len(content) > n else ""),
            "project_id": e.project_id,
            "universe": e.universe,
        }

    async def list_promotion_candidates(self, status: str = "pending") -> list:
        """Pending (or other status) promotion candidates, with content previews."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM promotion_candidates WHERE status=? ORDER BY best_cosine DESC, id DESC",
            (status,),
        ).fetchall()
        out = []
        for r in rows:
            mem = self.memories.get(r["memory_id"])
            if not mem:
                continue
            match_ids = json.loads(r["match_ids"])
            out.append({
                "candidate_id": r["id"],
                "memory_id": r["memory_id"],
                "content": mem.content,
                "universe": mem.universe,
                "project_id": mem.project_id,
                "best_cosine": round(r["best_cosine"], 4),
                "projects": json.loads(r["projects"]),
                "matches": [m for m in (self._mem_preview(mid) for mid in match_ids) if m],
                "timestamp": r["timestamp"],
            })
        return out

    async def approve_promotion(self, candidate_id: int) -> dict:
        """Approve a queued candidate: promote its memory to the general pool."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "candidate_not_found"}
        if row["status"] != "pending":
            return {"ok": False, "error": f"candidate_{row['status']}"}
        entry = self.memories.get(row["memory_id"])
        if not entry:
            async with self._get_lock():
                conn.execute("UPDATE promotion_candidates SET status='dismissed' WHERE id=?",
                             (candidate_id,))
                conn.commit()
            return {"ok": False, "error": "memory_gone"}
        original_project = entry.project_id
        entry.project_id = None
        async with self._get_lock():
            conn.execute("UPDATE memories SET project_id=NULL WHERE id=?", (entry.id,))
            conn.execute("UPDATE promotion_candidates SET status='approved' WHERE id=?",
                         (candidate_id,))
            conn.execute("""
                INSERT INTO promotions
                    (memory_id, original_project, projects, match_ids,
                     best_cosine, timestamp, trigger, reverted)
                VALUES (?, ?, ?, ?, ?, ?, 'approved', 0)
            """, (entry.id, original_project, row["projects"], row["match_ids"],
                  row["best_cosine"], time.time()))
            conn.commit()
        return {"ok": True, "memory_id": entry.id}

    async def dismiss_promotion(self, candidate_id: int) -> dict:
        """Dismiss a queued candidate without promoting."""
        conn = self._get_conn()
        async with self._get_lock():
            cur = conn.execute(
                "UPDATE promotion_candidates SET status='dismissed' WHERE id=? AND status='pending'",
                (candidate_id,),
            )
            conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "candidate_not_found_or_not_pending"}
        return {"ok": True}

    async def list_promotions(self, include_reverted: bool = False) -> list:
        """The promotion audit log (auto + approved), newest first."""
        conn = self._get_conn()
        sql = "SELECT * FROM promotions"
        if not include_reverted:
            sql += " WHERE reverted=0"
        sql += " ORDER BY id DESC"
        out = []
        for r in conn.execute(sql).fetchall():
            mem = self.memories.get(r["memory_id"])
            out.append({
                "promotion_id": r["id"],
                "memory_id": r["memory_id"],
                "content": mem.content if mem else "(deleted)",
                "original_project": r["original_project"],
                "projects": json.loads(r["projects"]),
                "best_cosine": round(r["best_cosine"], 4),
                "trigger": r["trigger"],
                "timestamp": r["timestamp"],
                "reverted": bool(r["reverted"]),
            })
        return out

    def export_graph(self) -> dict:
        """
        Export the full memory graph for the visualizer: nodes (memories),
        links (semantic lanes), and cluster metadata. Content is already
        decrypted in memory, so no key access is needed here.
        """
        conn = self._get_conn()
        cluster_theme = {cid: c.theme for cid, c in self.clusters.items()}
        nodes = []
        for e in self.memories.values():
            nodes.append({
                "id": e.id,
                "content": e.content,
                "universe": e.universe,
                "project_id": e.project_id,
                "is_general": e.project_id is None,
                "cluster_id": e.cluster_id,
                "cluster_theme": cluster_theme.get(e.cluster_id, "Unclustered"),
                "priority": e.priority,
                "access_count": e.access_count,
                "tags": e.tags,
                "category": e.category,
                "event_time": e.event_time,
                "superseded": e.superseded_by is not None,
            })
        links = []
        try:
            for r in conn.execute(
                "SELECT source_id, target_id, weight, link_type FROM memory_links"
            ):
                if r["source_id"] in self.memories and r["target_id"] in self.memories:
                    links.append({
                        "source": r["source_id"],
                        "target": r["target_id"],
                        "weight": round(r["weight"], 3),
                        "type": r["link_type"],
                    })
        except Exception:
            pass
        clusters = [
            {"id": c.id, "theme": c.theme, "size": len(c.memory_refs),
             "priority": c.priority}
            for c in self.clusters.values()
        ]
        return {
            "nodes": nodes,
            "links": links,
            "clusters": clusters,
            "stats": {
                "memories": len(self.memories),
                "general": sum(1 for e in self.memories.values() if e.project_id is None),
                "clusters": len(self.clusters),
                "links": len(links),
            },
        }

    async def revert_promotion(self, promotion_id: int) -> dict:
        """Undo a promotion: return the memory to its original project scope."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM promotions WHERE id=?", (promotion_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "promotion_not_found"}
        if row["reverted"]:
            return {"ok": False, "error": "already_reverted"}
        entry = self.memories.get(row["memory_id"])
        if entry:
            entry.project_id = row["original_project"]
        async with self._get_lock():
            conn.execute("UPDATE memories SET project_id=? WHERE id=?",
                         (row["original_project"], row["memory_id"]))
            conn.execute("UPDATE promotions SET reverted=1 WHERE id=?", (promotion_id,))
            conn.commit()
        return {"ok": True, "restored_project": row["original_project"]}

    # ── Compression / summarization (context economy) ────────────────────────

    async def list_compression_candidates(
        self,
        project_id: Optional[str] = None,
        min_chars: int = 600,
        limit: int = 20,
    ) -> list:
        """
        Verbose memories worth compressing — long, low/medium priority, not
        superseded. Claude reads these and rewrites them tighter via
        correct_memory, keeping the DB context-efficient as it grows. (Ported
        concept from 3am-AI's introspection summarizer, adapted so Claude — not a
        local LLM — does the summarizing.)
        """
        cands = []
        for e in self.memories.values():
            if e.superseded_by or e.universe == "episodic":
                continue
            if project_id is not None and e.project_id not in (project_id, None):
                continue
            if len(e.content) < min_chars:
                continue
            cands.append({
                "id": e.id,
                "chars": len(e.content),
                "content": e.content,
                "universe": e.universe,
                "priority": e.priority,
                "project_id": e.project_id,
            })
        cands.sort(key=lambda c: -c["chars"])
        return cands[:limit]

    async def summarize_cluster(self, cluster_id: str, http_client=None) -> dict:
        """
        Optional autonomous compression: if an llm_url is configured, ask it to
        write a tight summary memory for a cluster. Without an LLM this is a
        no-op (call list_compression_candidates and let Claude do it instead).
        """
        if not self.llm_url or http_client is None:
            return {"ok": False, "error": "no_llm_url"}
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return {"ok": False, "error": "cluster_not_found"}
        members = [self.memories[m] for m in cluster.memory_refs
                   if m in self.memories and not self.memories[m].superseded_by]
        if len(members) < 3:
            return {"ok": False, "error": "too_small"}
        joined = "\n".join(f"- {m.content}" for m in members[:30])
        prompt = ("Summarize these related memories into 1-3 concise factual "
                  "sentences, preserving specifics:\n\n" + joined)
        try:
            resp = await http_client.post(
                self.llm_url, json={"prompt": prompt, "max_tokens": 200}, timeout=30.0)
            summary = resp.json().get("content", "").strip()
        except Exception as e:
            return {"ok": False, "error": f"llm_error: {e}"}
        if not summary:
            return {"ok": False, "error": "empty_summary"}
        res = await self.store_memory(
            content=summary, project_id=members[0].project_id,
            universe="declarative", priority=4, category="general",
            tags=["summary", f"cluster:{cluster_id}"])
        return {"ok": True, "summary_memory_id": res["id"], "summarized": len(members)}

    # ── Supersession / corrections (temporal: facts that change) ──────────────

    async def mark_superseded(self, old_id: str, new_id: str) -> bool:
        """Flag an existing memory as replaced by a newer one. It is not deleted
        — it fades fast (10× decay) and is filtered from retrieval, preserving an
        audit trail."""
        entry = self.memories.get(old_id)
        if not entry:
            return False
        entry.superseded_by = new_id
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
            conn.commit()
        return True

    async def supersede_memory(
        self,
        old_memory_id: str,
        new_content: str,
        priority: Optional[int] = None,
        event_time: Optional[float] = None,
    ) -> dict:
        """
        Record that a fact CHANGED: store new_content as a fresh memory (inheriting
        the old one's scope/universe/category) and mark the old one superseded.
        Use when a previously-true fact is now different ("we migrated X→Y").
        Differs from correct_memory, which overwrites in place for a simple fix.
        """
        old = self.memories.get(old_memory_id)
        if not old:
            return {"ok": False, "error": "memory_not_found"}
        res = await self.store_memory(
            content=new_content,
            project_id=old.project_id,
            universe=old.universe,
            priority=priority if priority is not None else max(old.priority, 4),
            tags=old.tags,
            session_id=old.session_id,
            event_time=event_time,
            category=old.category,
            skip_dedup=True,
        )
        new_id = res["id"]
        if new_id != old_memory_id:
            await self.mark_superseded(old_memory_id, new_id)
        return {"ok": True, "new_memory_id": new_id, "superseded": old_memory_id}

    async def apply_correction(
        self,
        wrong_claim: str,
        correct_fact: str,
        project_id: Optional[str],
    ) -> dict:
        """
        Apply an explicit user correction. Find the stored memory closest to
        `wrong_claim` (within the project's visibility) and supersede it with a
        high-priority memory of `correct_fact`. If no close match, just store the
        correct fact.
        """
        wrong_claim = (wrong_claim or "").strip()
        correct_fact = (correct_fact or "").strip()
        if not correct_fact:
            return {"ok": False, "error": "empty_correct_fact"}

        matched_id = None
        if wrong_claim:
            emb = await self.embedder.embed(wrong_claim)
            emb_bytes = np.array(emb, dtype=np.float32).tobytes()
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT memory_id, distance FROM vec_memories
                WHERE embedding MATCH ? ORDER BY distance LIMIT 5
            """, (emb_bytes,)).fetchall()
            for r in rows:
                mid = r["memory_id"]
                if (mid in self.memories and self._is_visible(mid, project_id)
                        and not self.memories[mid].superseded_by
                        and (1.0 - r["distance"]) >= CORRECTION_MATCH_COSINE):
                    matched_id = mid
                    break

        if matched_id:
            old = self.memories[matched_id]
            res = await self.store_memory(
                content=correct_fact, project_id=project_id,
                universe=old.universe, priority=max(old.priority, 4),
                tags=old.tags, category=old.category, skip_dedup=True,
            )
            new_id = res["id"]
            if new_id != matched_id:
                await self.mark_superseded(matched_id, new_id)
            return {"ok": True, "corrected": True, "superseded": matched_id,
                    "new_memory_id": new_id}

        res = await self.store_memory(
            content=correct_fact, project_id=project_id,
            universe="declarative", priority=4,
        )
        return {"ok": True, "corrected": False, "new_memory_id": res["id"]}

    async def detect_conflicts(self, project_id: Optional[str] = None) -> dict:
        """
        Automatic conflict pass (ported from 3am-AI): within a scope, find pairs
        of same-category memories that are close-but-not-duplicate (distance in
        the conflict band) and keep the NEWER by timestamp, soft-superseding the
        older. Runs as part of the maintenance/clustering pass.
        """
        conn = self._get_conn()
        superseded = 0
        seen: set = set()
        for entry_id, entry in list(self.memories.items()):
            if entry.superseded_by or entry_id in seen:
                continue
            emb_row = conn.execute(
                "SELECT embedding FROM vec_memories WHERE memory_id=?", (entry_id,)
            ).fetchone()
            if not emb_row or not emb_row[0]:
                continue
            neighbors = conn.execute("""
                SELECT memory_id, distance FROM vec_memories
                WHERE embedding MATCH ? ORDER BY distance LIMIT ?
            """, (emb_row[0], CONFLICT_SCAN_LIMIT)).fetchall()
            for nb in neighbors:
                nid, dist = nb["memory_id"], nb["distance"]
                if nid == entry_id or nid in seen:
                    continue
                if dist < CONFLICT_DIST_NEAR:
                    continue          # duplicate — handled by dedup
                if dist > CONFLICT_DIST_FAR:
                    break             # ordered by distance — past conflict band
                other = self.memories.get(nid)
                if (not other or other.superseded_by
                        or other.category != entry.category
                        or other.project_id != entry.project_id):
                    continue
                if project_id is not None and entry.project_id != project_id:
                    continue
                older = entry if entry.timestamp <= other.timestamp else other
                newer = other if older is entry else entry
                await self.mark_superseded(older.id, newer.id)
                seen.add(older.id)
                superseded += 1
                if older.id == entry_id:
                    break
        return {"superseded": superseded}

    async def wipe_session_episodic(self, session_id: str) -> dict:
        """Wipe all episodic memories tagged with this session_id, plus orphaned
        episodics (session_id=None) older than 1 day that will never be cleaned up."""
        import time
        cutoff = time.time() - 86400  # 1 day ago
        to_delete = [
            mid for mid, e in self.memories.items()
            if e.universe == "episodic" and (
                e.session_id == session_id
                or (e.session_id is None and e.timestamp < cutoff)
            )
        ]
        async with self._get_lock():
            conn = self._get_conn()
            for mid in to_delete:
                self.memories.pop(mid, None)
                self._delete_memory_conn(conn, mid)
            if to_delete:
                conn.commit()
        self.stats["total_memories"] = len(self.memories)
        return {"wiped": len(to_delete)}

    async def delete_memory(self, memory_id: str) -> dict:
        """Hard delete a specific memory."""
        if memory_id not in self.memories:
            return {"ok": False, "error": "memory_not_found"}
        entry = self.memories.pop(memory_id)
        if entry.cluster_id and entry.cluster_id in self.clusters:
            cluster = self.clusters[entry.cluster_id]
            cluster.memory_refs = [r for r in cluster.memory_refs if r != memory_id]
            await self._write_cluster(cluster)
        async with self._get_lock():
            conn = self._get_conn()
            self._delete_memory_conn(conn, memory_id)
            conn.commit()
        self.stats["total_memories"] = len(self.memories)
        return {"ok": True}

    async def correct_memory(self, memory_id: str, new_content: str) -> dict:
        """Replace content of an existing memory, re-embed, rebuild lanes."""
        entry = self.memories.get(memory_id)
        if not entry:
            return {"ok": False, "error": "memory_not_found"}
        new_embedding = await self.embedder.embed(new_content)
        entry.content = new_content
        # Remove old lane links
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM memory_links WHERE source_id=? OR target_id=?",
                (memory_id, memory_id),
            )
            conn.commit()
        self._lanes_ready = None
        await self._write_memory(entry, new_embedding)
        await self._build_semantic_links(memory_id, new_embedding, entry.universe)
        return {"id": memory_id, "ok": True}

    # ── PPR ───────────────────────────────────────────────────────────────────

    def _ppr_expand(
        self,
        seed_ids: list,
        project_id: Optional[str],
        damping: float = 0.65,
        iterations: int = 15,
        top_k: int = 10,
    ) -> list:
        """
        Personalized PageRank through the lane graph.
        Enforces lane topology: blocks Project A → Project B walks.
        """
        if not seed_ids:
            return []

        conn = self._get_conn()
        placeholders = ",".join("?" * len(seed_ids))

        # 1-hop neighborhood
        hop1_rows = conn.execute(f"""
            SELECT DISTINCT target_id FROM memory_links WHERE source_id IN ({placeholders})
            UNION
            SELECT DISTINCT source_id FROM memory_links WHERE target_id IN ({placeholders})
        """, seed_ids + seed_ids).fetchall()

        neighborhood = set(seed_ids)
        for r in hop1_rows:
            mid = r[0]
            if mid in self.memories and self._is_visible(mid, project_id):
                neighborhood.add(mid)

        nb_list = list(neighborhood)
        nb_placeholders = ",".join("?" * len(nb_list))

        rows = conn.execute(f"""
            SELECT source_id, target_id, weight FROM memory_links
            WHERE source_id IN ({nb_placeholders})
        """, nb_list).fetchall()

        if not rows:
            return [(sid, 1.0) for sid in seed_ids if sid in self.memories]

        nodes_set: set = set(seed_ids)
        adj: dict = {}

        for source, target, weight in rows:
            src_entry = self.memories.get(source)
            dst_entry = self.memories.get(target)
            if not src_entry or not dst_entry:
                continue
            # Topology: block cross-project lane walks
            if not self._lane_topology_ok(src_entry.project_id, dst_entry.project_id):
                continue
            # Only include destinations that are visible to this query
            if not self._is_visible(target, project_id):
                continue
            nodes_set.add(source)
            nodes_set.add(target)
            adj.setdefault(source, []).append((target, float(weight)))

        if len(nodes_set) < 2:
            return [(sid, 1.0) for sid in seed_ids if sid in self.memories]

        node_list = list(nodes_set)
        idx = {n: i for i, n in enumerate(node_list)}
        n = len(node_list)

        # Column-stochastic transition matrix
        A = np.zeros((n, n))
        for source, neighbors in adj.items():
            si = idx[source]
            total = sum(w for _, w in neighbors)
            if total == 0:
                continue
            for target, w in neighbors:
                ti = idx.get(target)
                if ti is not None:
                    A[ti][si] = w / total

        # Personalization vector: uniform over seeds
        p = np.zeros(n)
        valid_seeds = [s for s in seed_ids if s in idx]
        if not valid_seeds:
            return []
        for sid in valid_seeds:
            p[idx[sid]] = 1.0 / len(valid_seeds)

        # Power iteration
        r = p.copy()
        for _ in range(iterations):
            r = (1.0 - damping) * p + damping * (A @ r)

        results = [
            (node_list[i], float(r[i]))
            for i in range(n)
            if node_list[i] in self.memories
            and r[i] > 0
            and self._is_visible(node_list[i], project_id)
        ]
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    # ── Clustering ────────────────────────────────────────────────────────────

    def _fetch_all_embeddings(self, memory_ids: list) -> tuple:
        if not memory_ids:
            return [], np.array([])
        conn = self._get_conn()
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            f"SELECT memory_id, embedding FROM vec_memories WHERE memory_id IN ({placeholders})",
            memory_ids,
        ).fetchall()
        ids_out, embs_out = [], []
        for row in rows:
            ids_out.append(row[0])
            embs_out.append(np.frombuffer(row[1], dtype=np.float32))
        return ids_out, np.array(embs_out) if embs_out else np.array([])

    def _keyword_theme(self, memory_ids: list) -> str:
        """Keyword-based cluster theme — no LLM required."""
        word_counts: dict = {}
        for mid in memory_ids[:10]:
            entry = self.memories.get(mid)
            if not entry:
                continue
            words = re.findall(r'\b[a-z][a-z-]{2,}\b', entry.content.lower())
            for w in words:
                if w not in _STOPWORDS:
                    word_counts[w] = word_counts.get(w, 0) + 1
        top = sorted(word_counts, key=lambda w: -word_counts[w])[:3]
        return " / ".join(top) if top else "General"

    def _clustering_cpu_work(
        self, target_clusters: int, memory_id_subset: Optional[list] = None
    ) -> dict:
        """Pure CPU clustering — safe to run in executor."""
        ids_to_cluster = memory_id_subset or list(self.memories.keys())
        memory_ids, embeddings = self._fetch_all_embeddings(ids_to_cluster)

        if len(memory_ids) < MIN_MEMORIES_FOR_CLUSTERING:
            return {}

        print(f"[Clustering] {len(memory_ids)} memories → target {target_clusters} clusters")

        from clustering import ClusteringConfig, MemoryClusterer

        config = ClusteringConfig.from_dict(self.clustering_config_dict)
        entries = [self.memories[m] for m in memory_ids]
        universes = [e.universe for e in entries]
        priorities = [e.priority for e in entries]
        access_counts = [e.access_count for e in entries]
        timestamps = [e.timestamp for e in entries]
        current_time = time.time()
        retention_scores = [self._calculate_retention(e, current_time) for e in entries]

        id_set = set(memory_ids)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT source_id, target_id, weight FROM memory_links WHERE link_type='semantic'"
        ).fetchall()
        lane_pairs = {
            (r[0], r[1]) for r in rows
            if r[0] in id_set and r[1] in id_set
            and r[2] >= config.lane_boost_min_weight
        }

        clusterer = MemoryClusterer(config)
        return clusterer.cluster(
            memory_ids, embeddings, universes, priorities,
            access_counts, timestamps, retention_scores,
            lane_pairs, target_clusters,
        )

    async def run_clustering(self) -> dict:
        """Full Torque Clustering pass. Replaces all existing clusters."""
        n = len(self.memories)
        if n < MIN_MEMORIES_FOR_CLUSTERING:
            return {"status": "skipped", "reason": "too_few_memories"}

        target_clusters = max(2, n // 4)
        loop = asyncio.get_event_loop()
        try:
            raw = await loop.run_in_executor(
                None, self._clustering_cpu_work, target_clusters, None
            )
        except Exception as e:
            print(f"[Clustering] Error: {e}")
            return {"status": "error", "error": str(e)}

        if not raw:
            return {"status": "skipped", "reason": "no_output"}

        current_time = time.time()
        new_clusters: dict = {}
        for label, cdata in raw.items():
            sub_ids = cdata["memory_ids"]
            if not sub_ids:
                continue
            theme = self._keyword_theme(sub_ids)
            cid = f"tc_{int(current_time * 1000)}_{label}"
            max_priority = max(
                (self.memories[mid].priority for mid in sub_ids if mid in self.memories),
                default=3,
            )
            cluster = MemoryCluster(
                id=cid,
                theme=theme,
                center_vector=cdata["centroid"],
                memory_refs=sub_ids,
                priority=max_priority,
                last_update=current_time,
                torque_mass=cdata["mass"],
            )
            new_clusters[cid] = cluster

        # Write all new clusters atomically
        async with self._get_lock():
            conn = self._get_conn()
            conn.execute("DELETE FROM clusters")
            for cluster in new_clusters.values():
                center_bytes = np.array(cluster.center_vector, dtype=np.float32).tobytes()
                conn.execute("""
                    INSERT INTO clusters
                    (id, theme, priority, last_update, torque_mass, center_vector, memory_refs)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    cluster.id, cluster.theme, cluster.priority,
                    cluster.last_update, cluster.torque_mass,
                    center_bytes, json.dumps(cluster.memory_refs),
                ))
                for mid in cluster.memory_refs:
                    if mid in self.memories:
                        self.memories[mid].cluster_id = cluster.id
                    conn.execute(
                        "UPDATE memories SET cluster_id=? WHERE id=?", (cluster.id, mid)
                    )
            conn.commit()

        self.clusters = new_clusters
        self._clustering_dirty = False
        self.stats["active_clusters"] = len(self.clusters)
        self.stats["last_clustering"] = current_time

        # Maintenance: auto-resolve contradictions (keep newer same-category fact).
        if self.conflict_detection:
            try:
                conflict = await self.detect_conflicts()
                if conflict.get("superseded"):
                    print(f"[Clustering] Conflict resolution: superseded {conflict['superseded']} stale memories")
            except Exception as e:
                print(f"[Clustering] Conflict detection error: {e}")

        print(f"[Clustering] Done: {len(raw)} clusters from {n} memories")
        return {"status": "success", "clusters": len(new_clusters), "memories": n}

    def unclustered_count(self) -> int:
        return sum(
            1 for m in self.memories.values()
            if m.cluster_id is None or m.cluster_id not in self.clusters
        )

    # ── Document ingestion ────────────────────────────────────────────────────

    async def ingest_document(
        self,
        propositions: list,
        project_id: Optional[str],
        source_path: Optional[str] = None,
        universe: str = "declarative",
        priority: int = 3,
    ) -> dict:
        """Batch store a list of propositions from a document."""
        if not propositions:
            return {"stored": 0, "total": 0}
        tags = ["ingest"]
        if source_path:
            tags.append(f"source:{Path(source_path).name}")
        stored = 0
        for proposition in propositions:
            p = proposition.strip()
            if not p or len(p) < 10:
                continue
            result = await self.store_memory(
                content=p,
                project_id=project_id,
                universe=universe,
                priority=priority,
                tags=tags,
            )
            if not result.get("deduped"):
                stored += 1
        return {"stored": stored, "total": len(propositions)}

    # ── Export / import ───────────────────────────────────────────────────────

    async def export_memories(self, project_id: Optional[str] = None) -> dict:
        """Export memories as a JSON-serializable dict. Content is decrypted."""
        current_time = time.time()
        memories = []
        for entry in self.memories.values():
            if project_id is not None and entry.project_id != project_id:
                continue
            if entry.ttl_expires and current_time > entry.ttl_expires:
                continue
            memories.append({
                "id": entry.id,
                "content": entry.content,
                "project_id": entry.project_id,
                "session_id": entry.session_id,
                "universe": entry.universe,
                "priority": entry.priority,
                "tags": entry.tags,
                "timestamp": entry.timestamp,
                "ttl_expires": entry.ttl_expires,
            })
        return {
            "version": 1,
            "exported_at": current_time,
            "count": len(memories),
            "memories": memories,
        }

    async def import_memories(self, data: dict) -> dict:
        """Import memories from export format. Re-embeds each memory."""
        memories = data.get("memories", [])
        if not memories:
            return {"imported": 0, "skipped": 0}
        imported = 0
        skipped = 0
        for m in memories:
            content = (m.get("content") or "").strip()
            if not content:
                skipped += 1
                continue
            ttl = m.get("ttl_expires")
            if ttl and time.time() > ttl:
                skipped += 1
                continue
            result = await self.store_memory(
                content=content,
                project_id=m.get("project_id"),
                universe=m.get("universe", "declarative"),
                priority=m.get("priority", 3),
                tags=m.get("tags") or [],
                ttl_days=None,
                session_id=m.get("session_id"),
            )
            if result.get("deduped"):
                skipped += 1
            else:
                imported += 1
        return {"imported": imported, "skipped": skipped}

    # ── CLAUDE.md bootstrap ───────────────────────────────────────────────────

    @staticmethod
    def _parse_claude_md_propositions(text: str, max_chunk_chars: int = 600) -> list:
        """Split CLAUDE.md into proposition-sized chunks without LLM."""
        chunks = []
        current_header = ""
        current_lines: list = []

        def emit():
            body = "\n".join(current_lines).strip()
            if not body or len(body) < 20:
                return
            chunk = (f"{current_header}\n{body}").strip() if current_header else body
            if len(chunk) <= max_chunk_chars:
                chunks.append(chunk)
            else:
                prefix = f"{current_header}: " if current_header else ""
                for para in body.split("\n\n"):
                    para = para.strip()
                    if len(para) >= 20:
                        chunks.append(prefix + para)

        for line in text.splitlines():
            if line.startswith("#"):
                emit()
                current_lines.clear()
                current_header = line.lstrip("#").strip()
            else:
                current_lines.append(line)
        emit()
        return chunks

    async def maybe_ingest_claude_md(
        self, project_root: str, project_id: Optional[str]
    ) -> int:
        """
        If CLAUDE.md exists at project_root and hasn't been ingested for this
        project_id, parse and store its contents as declarative memories.
        Returns number of propositions stored (0 if already done or no file).
        """
        meta_key = f"claude_md_ingested:{project_id or 'general'}"
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (meta_key,)).fetchone()
        if row:
            return 0

        claude_md = Path(project_root) / "CLAUDE.md"
        if not claude_md.exists():
            async with self._get_lock():
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (meta_key, "checked_no_file"),
                )
                conn.commit()
            return 0

        try:
            text = claude_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0

        propositions = self._parse_claude_md_propositions(text)
        if not propositions:
            return 0

        result = await self.ingest_document(
            propositions=propositions,
            project_id=project_id,
            source_path=str(claude_md),
            universe="declarative",
            priority=4,
        )

        async with self._get_lock():
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (meta_key, str(time.time())),
            )
            conn.commit()

        print(f"[3am-claude] Ingested CLAUDE.md: {result['stored']} propositions")
        return result["stored"]

    # ── Behavioral seeds ──────────────────────────────────────────────────────

    # Bump the version string to re-seed on next server start (e.g. when adding
    # new seeds or correcting existing ones).
    _SEEDS_VERSION = "v2"
    _SEEDS: list[dict] = [
        {
            "content": (
                "Use episodic memories (universe=episodic) to track in-flight state "
                "during long or complex tasks. Store them when: (1) starting a task "
                "that spans 5+ edits — note the plan and where you are; (2) mid-debug "
                "— what was tried, what failed, what's next; (3) leaving work incomplete "
                "— what's done, what still needs doing. Episodic memories decay in 1 day "
                "and get wiped at session end, so use them freely. They're most valuable "
                "when context gets compacted mid-session — get_session_summary will still "
                "surface 'what's in flight' even after compression. The PreCompact hook "
                "blocks once right before compaction to give you a final checkpoint — "
                "treat that prompt as the cue to flush anything not yet stored."
            ),
            "universe": "procedural",
            "priority": 5,
            "tags": ["episodic", "memory", "workflow", "behavior"],
        },
        {
            "content": (
                "Build a personal skill/approach playbook. When a Claude Code Skill "
                "(e.g. /openscad, /code-review, /verify) or a specific non-obvious "
                "approach works well — or notably fails — for a recurring kind of task, "
                "store a procedural memory: which skill or approach, for what task type, "
                "and the outcome. Example: 'For 3D-printable parametric parts, the "
                "openscad skill is the right tool — invoke it rather than hand-writing "
                "SCAD.' This lets future sessions pick the right tool immediately instead "
                "of re-discovering it. Query for these before tackling a task that smells "
                "familiar."
            ),
            "universe": "procedural",
            "priority": 5,
            "tags": ["skills", "tools", "workflow", "behavior", "playbook"],
        },
        {
            "content": (
                "Store user preferences and working style as general memories "
                "(project_id=None) so they apply everywhere: how the user likes work "
                "presented, tools/libraries they favor or avoid, conventions they've "
                "corrected you on, and recurring constraints. These are high-priority "
                "(4-5) and rarely decay. Query them before answering questions about the "
                "user or making a stylistic choice — don't guess at what's already known."
            ),
            "universe": "procedural",
            "priority": 5,
            "tags": ["preferences", "user", "workflow", "behavior"],
        },
    ]

    async def seed_behavioral_memories(self) -> int:
        """
        Plant baseline procedural memories in the general pool on first server start.
        Gated by a meta key — runs once per _SEEDS_VERSION, never re-runs unless
        the version is bumped. Returns number of memories stored (0 if already seeded).
        """
        meta_key = f"behavioral_seeds:{self._SEEDS_VERSION}"
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (meta_key,)
        ).fetchone()
        if row:
            return 0

        stored = 0
        for seed in self._SEEDS:
            try:
                await self.store_memory(
                    content=seed["content"],
                    project_id=None,
                    universe=seed["universe"],
                    priority=seed["priority"],
                    tags=seed.get("tags", []),
                )
                stored += 1
            except Exception:
                pass

        async with self._get_lock():
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (meta_key, str(time.time())),
            )
            conn.commit()

        return stored

    # ── Stats ─────────────────────────────────────────────────────────────────

    def save_stats(self):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('stats', ?)",
            (json.dumps(self.stats),),
        )
        conn.commit()
