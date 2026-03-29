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

CHARS_PER_TOKEN = 4  # rough approximation for max_tokens budget

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
    timestamp: float
    ttl_expires: Optional[float] # None = permanent
    cluster_id: Optional[str]
    access_count: int
    last_accessed: float


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
                last_accessed REAL NOT NULL DEFAULT 0
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

            CREATE INDEX IF NOT EXISTS idx_links_source   ON memory_links(source_id);
            CREATE INDEX IF NOT EXISTS idx_links_target   ON memory_links(target_id);
            CREATE INDEX IF NOT EXISTS idx_mem_project    ON memories(project_id);
            CREATE INDEX IF NOT EXISTS idx_mem_session    ON memories(session_id);
            CREATE INDEX IF NOT EXISTS idx_mem_universe   ON memories(universe);
        """)

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
            "timestamp, ttl_expires, cluster_id, access_count, last_accessed FROM memories"
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
        access_resistance = 1.0 / (1.0 + entry.access_count)
        effective_rate = base_rate * universe_mult * access_resistance
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
                 timestamp, ttl_expires, cluster_id, access_count, last_accessed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id, _e(entry.content), entry.project_id, entry.session_id,
                entry.universe, entry.priority, json.dumps(entry.tags),
                entry.timestamp, entry.ttl_expires, entry.cluster_id,
                entry.access_count, entry.last_accessed,
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
    ) -> dict:
        """
        Store a memory. Returns {id, cluster_theme}.
        Deduplicates within project scope.
        """
        if universe not in VALID_UNIVERSES:
            universe = "episodic"
        priority = max(1, min(5, priority))
        tags = tags or []

        embedding = await self.embedder.embed(content)
        emb_bytes = np.array(embedding, dtype=np.float32).tobytes()
        conn = self._get_conn()

        # Dedup check: nearest neighbor within distance threshold
        # sqlite-vec requires LIMIT on the vec0 table directly (inside subquery)
        row = conn.execute("""
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
        return {"id": entry_id, "cluster_theme": cluster_theme}

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

        # FTS5 seeds — post-filter by project visibility
        fts_seeds: list = []
        fts_query = " OR ".join(
            f'"{w}"' for w in query.split()[:8] if len(w) > 2
        )
        if fts_query:
            try:
                fts_rows = conn.execute("""
                    SELECT memory_id, rank FROM fts_memories
                    WHERE fts_memories MATCH ?
                    ORDER BY rank
                    LIMIT 20
                """, (fts_query,)).fetchall()
                fts_seeds = [
                    r["memory_id"] for r in fts_rows
                    if r["memory_id"] in self.memories
                    and self._is_visible(r["memory_id"], project_id)
                ][:8]
            except Exception:
                pass

        # Vec seeds — post-filter by project visibility
        vec_rows = conn.execute("""
            SELECT memory_id, distance FROM vec_memories
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT 20
        """, (emb_bytes,)).fetchall()
        vec_seeds = [
            r["memory_id"] for r in vec_rows
            if r["memory_id"] in self.memories
            and self._is_visible(r["memory_id"], project_id)
        ][:8]

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

        if self._lanes_ready:
            ranked = self._ppr_expand(seed_ids, project_id, top_k=limit + 6)
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
        token_count = 0
        for memory_id, score in ranked:
            if score < min_score:
                continue
            entry = self.memories.get(memory_id)
            if not entry:
                continue
            u = entry.universe
            if universe_counts.get(u, 0) >= universe_caps.get(u, limit):
                continue
            cluster_theme = "General"
            if entry.cluster_id and entry.cluster_id in self.clusters:
                cluster_theme = self.clusters[entry.cluster_id].theme
            entry_tokens = max(1, len(entry.content) // CHARS_PER_TOKEN)
            if token_count + entry_tokens > max_tokens and results:
                break
            token_count += entry_tokens
            universe_counts[u] = universe_counts.get(u, 0) + 1
            results.append({
                "id": entry.id,
                "content": entry.content,
                "universe": entry.universe,
                "score": round(score, 4),
                "cluster_theme": cluster_theme,
                "project_id": entry.project_id,
                "tags": entry.tags,
                "priority": entry.priority,
            })
            if len(results) >= limit:
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

    async def wipe_session_episodic(self, session_id: str) -> dict:
        """Wipe all episodic memories tagged with this session_id."""
        to_delete = [
            mid for mid, e in self.memories.items()
            if e.session_id == session_id and e.universe == "episodic"
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
    _SEEDS_VERSION = "v1"
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
                "surface 'what's in flight' even after compression."
            ),
            "universe": "procedural",
            "priority": 5,
            "tags": ["episodic", "memory", "workflow", "behavior"],
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
