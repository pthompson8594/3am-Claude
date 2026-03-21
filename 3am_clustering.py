"""3am_clustering.py — Universe-aware, lane-aware, priority-weighted memory clustering.

Pure computation module — no I/O, no imports from memory.py.
Dependencies: numpy, scipy
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import scipy.sparse
import scipy.sparse.csgraph

# ── Module-level constants ────────────────────────────────────────────────────

UNIVERSE_PENALTY = 0.30
LANE_BOOST = 0.15
LANE_BOOST_MIN_WEIGHT = 0.65
PRIORITY_WEIGHTS: dict[int, float] = {5: 3.0, 4: 2.0, 3: 1.0, 2: 0.6, 1: 0.3}
ACCESS_COUNT_MASS_CAP = 2.0
ACCESS_COUNT_FACTOR = 0.1
TEMPORAL_LOCALITY_DAYS = 7
TEMPORAL_LOCALITY_BONUS = 0.05
ADJUSTMENT_FACTOR = 0.5
MIN_CLUSTER_SIZE = 2
MAX_CLUSTER_SIZE = 20
REASSIGNMENT_THRESHOLD = 0.5
MEMORIES_PER_CLUSTER_TARGET = 4

UNIVERSE_DISTANCES: dict[frozenset, float] = {
    frozenset({"episodic", "declarative"}): 0.20,
    frozenset({"episodic", "procedural"}): 0.40,
    frozenset({"declarative", "procedural"}): 0.25,
}

_CLUSTERING_CONFIG_KEYS = frozenset({
    "adjustment_factor", "lane_boost", "lane_boost_min_weight",
    "use_access_count_mass", "use_decay_vitality",
    "temporal_locality_days", "temporal_locality_bonus",
    "memories_per_cluster_target", "min_clusters", "max_clusters",
    "centroid_strategy", "reassignment_threshold", "max_cluster_size",
    "use_new_clusterer",
})


# ── ClusteringConfig ──────────────────────────────────────────────────────────

@dataclass
class ClusteringConfig:
    adjustment_factor: float = ADJUSTMENT_FACTOR
    universe_distances: dict = field(default_factory=lambda: dict(UNIVERSE_DISTANCES))
    lane_boost: float = LANE_BOOST
    lane_boost_min_weight: float = LANE_BOOST_MIN_WEIGHT
    use_access_count_mass: bool = True
    use_decay_vitality: bool = True
    temporal_locality_days: int = TEMPORAL_LOCALITY_DAYS
    temporal_locality_bonus: float = TEMPORAL_LOCALITY_BONUS
    memories_per_cluster_target: int = MEMORIES_PER_CLUSTER_TARGET
    min_clusters: int = 2
    max_clusters: int = 30
    centroid_strategy: str = "weighted_mean"  # "mean" | "weighted_mean" | "medoid"
    reassignment_threshold: float = REASSIGNMENT_THRESHOLD
    max_cluster_size: int = MAX_CLUSTER_SIZE
    use_new_clusterer: bool = False  # feature flag — flip to True to activate

    @classmethod
    def from_dict(cls, d: dict) -> "ClusteringConfig":
        return cls(**{k: v for k, v in d.items() if k in _CLUSTERING_CONFIG_KEYS})


# ── ClusterHealthReport ───────────────────────────────────────────────────────

@dataclass
class ClusterHealthReport:
    total_memories: int
    total_clusters: int
    sizes: list[int]
    avg_size: float
    min_size: int
    max_size: int
    oversized_count: int       # > config.max_cluster_size
    tiny_count: int            # < MIN_CLUSTER_SIZE
    universe_distribution: dict[str, dict[str, int]]
    mixed_cluster_count: int   # clusters with >1 universe present
    current_factor: float
    suggested_factor: Optional[float]  # None = no change needed
    warnings: list[str]

    def summary_line(self) -> str:
        return (
            f"[Clustering] {self.total_memories} facts → {self.total_clusters} clusters | "
            f"avg {self.avg_size:.1f}/cluster | "
            f"min {self.min_size}, max {self.max_size} | "
            f"adjustment_factor={self.current_factor}"
        )

    def print_report(self) -> None:
        print(self.summary_line())
        for w in self.warnings:
            print(w)


# ── Pure functions ────────────────────────────────────────────────────────────

def adjusted_distance(
    emb_a: np.ndarray,
    emb_b: np.ndarray,
    universe_a: str,
    universe_b: str,
    has_lane: bool,
    timestamp_a: float,
    timestamp_b: float,
    config: ClusteringConfig,
) -> float:
    """Compute adjusted cosine distance between two memories."""
    base = 1.0 - float(np.dot(emb_a, emb_b))

    if universe_a != universe_b:
        key = frozenset({universe_a, universe_b})
        base += config.universe_distances.get(key, UNIVERSE_PENALTY)

    if has_lane:
        base = max(0.0, base - config.lane_boost)

    if config.temporal_locality_days > 0:
        age_diff_days = abs(timestamp_a - timestamp_b) / 86400
        if age_diff_days <= config.temporal_locality_days:
            base = max(0.0, base - config.temporal_locality_bonus)

    return base


def score_cluster(
    retention_scores: list[float],
    size: int,
    torque_mass: float,
    has_recent_member: bool,
) -> float:
    """Health score for a cluster (replaces _calculate_cluster_health in memory.py)."""
    if not retention_scores:
        return 0.0
    avg_decay = sum(retention_scores) / len(retention_scores)
    size_bonus = min(size / 10, 1.0) * 0.1
    recent = 0.2 if has_recent_member else 0.0
    mass_bonus = min(torque_mass / 100, 0.2) if torque_mass > 0 else 0.0
    return avg_decay + size_bonus + recent + mass_bonus


# ── MemoryClusterer ───────────────────────────────────────────────────────────

class MemoryClusterer:
    def __init__(self, config: Optional[ClusteringConfig] = None):
        self.config = config or ClusteringConfig()

    # ── Public API ────────────────────────────────────────────────────────────

    def cluster(
        self,
        memory_ids: list[str],
        embeddings: np.ndarray,           # (n, 768) float32, L2-normalized
        universes: list[str],             # len n
        priorities: list[int],            # len n, 1-5
        access_counts: list[int],         # len n
        timestamps: list[float],          # len n, unix
        retention_scores: list[float],    # len n, 0.0-1.0
        lane_pairs: set[tuple[str, str]], # (id_a, id_b) semantic lanes
        target_clusters: int = 0,         # 0 = auto
    ) -> dict[int, dict]:
        """Full O(n²) recluster. Returns {label: {memory_ids, centroid, mass}}."""
        n = len(memory_ids)
        if n == 0:
            return {}

        k = self._resolve_target(n, target_clusters)

        # Normalize embeddings exactly once.
        # sqlite-vec stores float32; after roundtrip the L2 norm may drift from 1.0.
        # cdist(embeddings, embeddings, 'cosine') in TorqueClustering accounts for this;
        # our dot-product shortcut doesn't unless we renormalize first.
        emb = embeddings.astype(np.float64)
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        np.maximum(norms, 1e-8, out=norms)
        emb /= norms

        masses = self._build_community_masses(priorities, access_counts, retention_scores)

        # Plain cosine distance for Phase 1 NN construction (now equivalent to cdist cosine).
        D_base = (1.0 - (emb @ emb.T))
        np.fill_diagonal(D_base, 0.0)
        np.clip(D_base, 0.0, None, out=D_base)

        # Adjusted distance for Phase 2: universe penalties, lane boosts, temporal locality.
        # Semantic adjustments belong here — not in the mass / torque computation.
        D = self._build_distance_matrix(emb, universes, timestamps, lane_pairs, memory_ids)

        labels = self._torque_merge(D_base, D, k)
        return self._build_output(labels, memory_ids, embeddings, priorities, masses)

    def assign_new(
        self,
        new_ids: list[str],
        new_embeddings: np.ndarray,              # (m, 768)
        new_universes: list[str],
        existing_cluster_ids: list[str],
        cluster_centroids: list[np.ndarray],
        cluster_dominant_universes: list[str],
        cluster_sizes: list[int],
    ) -> dict[str, str]:
        """Incremental assignment. Returns {new_memory_id: cluster_id | "orphan"}."""
        centroids = np.array(cluster_centroids)  # (k, 768)
        result: dict[str, str] = {}

        for mid, emb, univ in zip(new_ids, new_embeddings, new_universes):
            best_cluster = "orphan"
            best_sim = self.config.reassignment_threshold

            for ci, (cid, dom_univ, csize) in enumerate(
                zip(existing_cluster_ids, cluster_dominant_universes, cluster_sizes)
            ):
                if csize >= self.config.max_cluster_size:
                    continue
                sim = float(np.dot(emb, centroids[ci]))
                if univ != dom_univ:
                    key = frozenset({univ, dom_univ})
                    penalty = self.config.universe_distances.get(key, UNIVERSE_PENALTY)
                    sim -= penalty
                if sim > best_sim:
                    best_sim = sim
                    best_cluster = cid

            result[mid] = best_cluster

        return result

    def health_report(
        self,
        cluster_member_universes: dict[str, list[str]],
        total_memories: int,
    ) -> ClusterHealthReport:
        """Build a ClusterHealthReport from current cluster state."""
        if not cluster_member_universes:
            return ClusterHealthReport(
                total_memories=total_memories,
                total_clusters=0,
                sizes=[],
                avg_size=0.0,
                min_size=0,
                max_size=0,
                oversized_count=0,
                tiny_count=0,
                universe_distribution={},
                mixed_cluster_count=0,
                current_factor=self.config.adjustment_factor,
                suggested_factor=None,
                warnings=[],
            )

        sizes = [len(v) for v in cluster_member_universes.values()]
        avg_size = sum(sizes) / len(sizes)
        oversized = sum(1 for s in sizes if s > self.config.max_cluster_size)
        tiny = sum(1 for s in sizes if s < MIN_CLUSTER_SIZE)

        universe_distribution: dict[str, dict[str, int]] = {}
        mixed = 0
        for cid, univs in cluster_member_universes.items():
            counts: dict[str, int] = {}
            for u in univs:
                counts[u] = counts.get(u, 0) + 1
            universe_distribution[cid] = counts
            if len(counts) > 1:
                mixed += 1

        factor = self.config.adjustment_factor
        n_clusters = len(sizes)
        suggested: Optional[float] = None
        warnings: list[str] = []

        if oversized:
            warnings.append(
                f"[Clustering] WARNING: {oversized} cluster(s) have >{self.config.max_cluster_size} facts "
                f"— consider increasing clustering_adjustment_factor in config.json (currently {factor})"
            )
            suggested = round(factor + 0.1, 2)

        if tiny > n_clusters * 0.3:
            warnings.append(
                f"[Clustering] WARNING: {tiny} cluster(s) have <{MIN_CLUSTER_SIZE} facts "
                f"— consider decreasing clustering_adjustment_factor in config.json (currently {factor})"
            )
            if suggested is None:
                suggested = round(factor - 0.1, 2)

        return ClusterHealthReport(
            total_memories=total_memories,
            total_clusters=n_clusters,
            sizes=sizes,
            avg_size=avg_size,
            min_size=min(sizes),
            max_size=max(sizes),
            oversized_count=oversized,
            tiny_count=tiny,
            universe_distribution=universe_distribution,
            mixed_cluster_count=mixed,
            current_factor=factor,
            suggested_factor=suggested,
            warnings=warnings,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_target(self, n: int, target_clusters: int) -> int:
        if target_clusters > 0:
            return max(self.config.min_clusters, min(target_clusters, self.config.max_clusters))
        auto = max(self.config.min_clusters, n // self.config.memories_per_cluster_target)
        return min(auto, self.config.max_clusters)

    def _build_distance_matrix(
        self,
        embeddings: np.ndarray,
        universes: list[str],
        timestamps: list[float],
        lane_pairs: set[tuple[str, str]],
        memory_ids: list[str],
    ) -> np.ndarray:
        # 1. Base cosine distance — vectorized O(n²)
        base = (1.0 - (embeddings @ embeddings.T)).astype(np.float64)

        # 2. Universe penalties — vectorized per pair
        u_arr = np.array(universes)
        for key, penalty in self.config.universe_distances.items():
            names = tuple(key)
            if len(names) != 2:
                continue
            a_name, b_name = names
            mask = (
                np.outer(u_arr == a_name, u_arr == b_name)
                | np.outer(u_arr == b_name, u_arr == a_name)
            )
            base[mask] += penalty

        # 3. Lane boost — loop over lane_pairs (bounded by LANE_MAX_K * n)
        if lane_pairs:
            id_to_idx = {mid: i for i, mid in enumerate(memory_ids)}
            for id_a, id_b in lane_pairs:
                ia = id_to_idx.get(id_a)
                ib = id_to_idx.get(id_b)
                if ia is not None and ib is not None:
                    base[ia, ib] = max(0.0, base[ia, ib] - self.config.lane_boost)
                    base[ib, ia] = max(0.0, base[ib, ia] - self.config.lane_boost)

        # 4. Temporal locality — vectorized
        if self.config.temporal_locality_days > 0 and self.config.temporal_locality_bonus > 0:
            ts = np.array(timestamps)
            age_diff = np.abs(np.subtract.outer(ts, ts)) / 86400
            local_mask = age_diff <= self.config.temporal_locality_days
            base[local_mask] = np.maximum(0.0, base[local_mask] - self.config.temporal_locality_bonus)

        np.fill_diagonal(base, 0.0)
        np.clip(base, 0.0, None, out=base)
        return base

    def _build_community_masses(
        self,
        priorities: list[int],
        access_counts: list[int],
        retention_scores: list[float],
    ) -> np.ndarray:
        masses = np.array(
            [PRIORITY_WEIGHTS.get(p, 1.0) for p in priorities], dtype=np.float64
        )
        if self.config.use_access_count_mass:
            ac = np.array(access_counts, dtype=np.float64)
            masses *= np.minimum(ACCESS_COUNT_MASS_CAP, 1.0 + ACCESS_COUNT_FACTOR * ac)
        if self.config.use_decay_vitality:
            masses *= np.array(retention_scores, dtype=np.float64)
        return masses

    def _torque_merge(
        self,
        D_phase1: np.ndarray,  # (n, n) plain cosine distance — Phase 1 NN graph
        D_phase2: np.ndarray,  # (n, n) adjusted distance — Phase 2 inter-community
        target_k: int,
    ) -> np.ndarray:           # (n,) int64 cluster labels
        n = len(D_phase1)
        if n == 1:
            return np.zeros(1, dtype=np.int64)

        D_inf = D_phase1.copy()
        np.fill_diagonal(D_inf, np.inf)

        # Phase 1: First-layer NN graph — UNCONDITIONAL, no cycle prevention.
        # Every node connects to its nearest neighbor regardless of existing edges.
        # Connected components of the resulting (possibly cyclic) graph form initial
        # communities.  Matching the original TorqueClustering Phase 1 exactly.
        adj = scipy.sparse.lil_matrix((n, n), dtype=np.float64)
        seen_edges: set[tuple[int, int]] = set()
        link_log: list[tuple[int, int, float, float, float]] = []

        for i in range(n):
            j = int(np.argmin(D_inf[i]))
            adj[i, j] = 1.0
            adj[j, i] = 1.0
            key = (min(i, j), max(i, j))
            if key not in seen_edges:
                seen_edges.add(key)
                # Phase 1 mass = 1.0 (matching original TorqueClustering).
                # Priority-weighted community masses are applied in Phase 2 where
                # they boost torque on inter-cluster bridges.  Using priority mass
                # here inflates torque on short high-priority edges and causes the
                # wrong edges to be cut first.
                link_log.append((i, j, float(D_phase1[i, j]), 1.0, 1.0))

        n_comp, labels = scipy.sparse.csgraph.connected_components(adj.tocsr(), directed=False)
        comp_sizes = sorted([int((labels == i).sum()) for i in range(n_comp)], reverse=True)
        print(f"[Clustering] Phase 1: {n_comp} components from {n} nodes — sizes: {comp_sizes}")

        # Phase 2: iterative community merge until convergence.
        # Each community connects to its nearest community with size >= its own.
        # This matches TorqueClustering's "nearest larger-or-equal" rule exactly.
        # NO FALLBACK: the current largest community skips connecting this iteration.
        # The adjusted distance matrix (D_phase2) guides WHICH communities merge,
        # so universe penalties and lane boosts influence the merge structure without
        # disrupting the size-based hierarchy.
        prev_n_clusters = n_comp + 1

        while True:
            unique_labels = np.unique(labels)
            n_comm = len(unique_labels)

            if n_comm <= 1 or n_comm == prev_n_clusters:
                break
            prev_n_clusters = n_comm

            comm_indices = [np.where(labels == lbl)[0] for lbl in unique_labels]
            # Community SIZE (not priority-weighted mass) — matching TorqueClustering's
            # len(community[i]) criterion.  Priority weighting lives in the distance matrix.
            comm_sizes = np.array([len(ci) for ci in comm_indices], dtype=np.float64)

            # Inter-community minimum distance matrix — use D_phase2 (adjusted) so that
            # universe penalties and lane boosts influence which communities merge.
            C = np.full((n_comm, n_comm), np.inf)
            for i in range(n_comm):
                for j in range(i + 1, n_comm):
                    min_d = D_phase2[np.ix_(comm_indices[i], comm_indices[j])].min()
                    C[i, j] = min_d
                    C[j, i] = min_d

            new_adj_comm = scipy.sparse.lil_matrix((n_comm, n_comm), dtype=np.float64)
            for i in range(n_comm):
                # Connect to nearest community with size >= this community (TorqueClustering rule).
                candidates = np.where(comm_sizes >= comm_sizes[i])[0]
                candidates = candidates[candidates != i]
                if len(candidates) == 0:
                    # Largest community this iteration — no fallback (matches TorqueClustering).
                    continue
                j = int(candidates[np.argmin(C[i, candidates])])
                new_adj_comm[i, j] = 1.0
                new_adj_comm[j, i] = 1.0

                # Representative memory pair for this inter-community link
                sub      = D_phase2[np.ix_(comm_indices[i], comm_indices[j])]
                ii2, jj2 = np.unravel_index(int(np.argmin(sub)), sub.shape)
                pa       = int(comm_indices[i][ii2])
                pb       = int(comm_indices[j][jj2])

                adj[pa, pb] = 1.0
                adj[pb, pa] = 1.0
                # Do NOT deduplicate Phase 2 entries against seen_edges.
                # The same (pa, pb) node pair may already exist in link_log as a Phase 1 entry
                # with mass=1*1.  The Phase 2 entry has mass=size_i*size_j (30-64x larger), so
                # its torque must be recorded separately so inter-community edges win the cut
                # ranking over intra-community Phase 1 edges.
                #
                # IMPORTANT: store the UNADJUSTED distance D_phase1[pa,pb] for torque computation,
                # not C[i,j] from D_phase2.  D_phase2 applies temporal locality (-0.05) and lane
                # boosts (-0.15), which when squared can reduce inter-community torques 4-7x below
                # Phase 1 within-community torques — causing the wrong edges to be cut (singletons
                # instead of balanced clusters).  The semantic adjustments in D_phase2 influence
                # WHICH community to merge with (merge criterion above), which is correct.
                # Torque must use raw cosine distance, matching TorqueClustering's formula exactly.
                link_log.append((pa, pb, float(D_phase1[pa, pb]), float(comm_sizes[i]), float(comm_sizes[j])))

            _, comm_labels = scipy.sparse.csgraph.connected_components(
                new_adj_comm.tocsr(), directed=False
            )

            new_labels = np.empty(n, dtype=np.int64)
            for ci_idx, lbl in enumerate(unique_labels):
                new_labels[labels == lbl] = comm_labels[ci_idx]
            labels = new_labels

        if not link_log:
            return labels

        # Phase 3: compute torques
        dists     = np.array([e[2] for e in link_log])
        m_a       = np.array([e[3] for e in link_log])
        m_b       = np.array([e[4] for e in link_log])
        dists_sq  = dists ** 2
        edge_mass = m_a * m_b
        torques   = edge_mass * dists_sq


        # Phase 4: determine cut count.
        # target_k > 0: cut exactly target_k - 1 edges (same as original K-1).
        # target_k == 0: auto-detect via std thresholds.
        if target_k > 0:
            n_cuts = target_k - 1
        else:
            sort_idx   = np.argsort(torques)[::-1]
            s_torques  = torques[sort_idx]
            s_dists_sq = dists_sq[sort_idx]
            s_masses   = edge_mass[sort_idx]

            fac         = self.config.adjustment_factor
            R_thresh    = float(np.nanmean(s_dists_sq))  - fac * float(np.nanstd(s_dists_sq))
            mass_thresh = float(np.nanmean(s_masses))    - fac * float(np.nanstd(s_masses))
            p_thresh    = float(np.nanmean(s_torques))   - fac * float(np.nanstd(s_torques))

            qualifying = np.where(
                (s_dists_sq >= R_thresh) &
                (s_masses   >= mass_thresh) &
                (s_torques  >= p_thresh)
            )[0]
            n_cuts = int(np.max(qualifying)) if len(qualifying) > 0 else 1

        n_cuts = max(0, min(int(n_cuts), len(link_log)))
        if n_cuts == 0:
            return labels

        # Phase 5: cut top n_cuts edges by torque → connected components = final labels
        cut_order = np.argsort(torques)[::-1]
        adj_cut   = adj.copy()
        for k in range(n_cuts):
            edge_idx = int(cut_order[k])
            pa, pb   = link_log[edge_idx][0], link_log[edge_idx][1]
            adj_cut[pa, pb] = 0.0
            adj_cut[pb, pa] = 0.0

        _, final_labels = scipy.sparse.csgraph.connected_components(
            adj_cut.tocsr(), directed=False
        )
        return final_labels.astype(np.int64)

    def _build_output(
        self,
        labels: np.ndarray,
        memory_ids: list[str],
        embeddings: np.ndarray,
        priorities: list[int],
        masses: np.ndarray,
    ) -> dict[int, dict]:
        result: dict[int, dict] = {}
        for label in np.unique(labels):
            idxs = np.where(labels == label)[0]
            ids  = [memory_ids[i] for i in idxs]
            embs = embeddings[idxs]
            w    = masses[idxs]

            strategy = self.config.centroid_strategy
            if strategy == "weighted_mean":
                total_w = float(w.sum())
                centroid = np.average(embs, weights=w, axis=0) if total_w > 0 else embs.mean(axis=0)
            elif strategy == "medoid":
                sub_D = 1.0 - (embs @ embs.T)
                centroid = embs[int(np.argmin(sub_D.sum(axis=1)))]
            else:  # "mean"
                centroid = embs.mean(axis=0)

            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm

            result[int(label)] = {
                "memory_ids": ids,
                "centroid": centroid.tolist(),
                "mass": float(w.sum()),
            }

        return result
