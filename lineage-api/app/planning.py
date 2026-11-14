"""Minimum-cost recalculation planning over one lineage snapshot.

Problem
-------
Given target report columns and a non-negative execution cost for every
relevant non-source column, pick one candidate input group for every column
that must be executed so that:

* every target is produced;
* whenever a column is executed, every non-source input of its chosen group is
  executed too (transitively);
* a derived column shared by several targets is executed only once and always
  uses the *same* chosen group;
* the summed execution cost is globally minimal.

Algorithm
---------
Registration rejects cycles, so the lineage graph is a DAG.  The number of
relevant non-source columns is capped at 16, which makes subset enumeration
exact and fast:

1. Backward BFS from the targets collects every relevant non-source column.
2. Each candidate group becomes a bit-mask of the non-source inputs it needs
   (source inputs need no execution and are ignored).
3. Enumerate all subsets of relevant columns.  A subset is *closed* when each
   member has at least one group whose non-source inputs are all inside the
   subset.  Among the closed subsets containing every target, the one with the
   smallest summed cost is optimal — the columns executed by any feasible plan
   always form a closed subset, so the optimum is never missed.
4. Within the optimal subset each column deterministically takes its first
   feasible group (ties prefer the smallest group index), and a Kahn
   topological sort yields the dependency-respecting execution order.

All functions here are pure: they operate on graph data already loaded inside a
single snapshot transaction, so a plan can never mix definitions from before
and after a concurrent registration.
"""

from __future__ import annotations

from collections import deque

# Upper bound on relevant non-source columns; subset enumeration is O(2 ** n).
MAX_RELEVANT_NON_SOURCE = 16


def collect_relevant_non_source(
    targets: list[str],
    kinds: dict[str, str],
    groups: dict[str, list[list[str]]],
) -> list[str]:
    """Return the sorted non-source columns reachable backward from targets.

    Traversal follows every candidate input of every visited node, i.e. the
    result is independent of which group will eventually be chosen.  Source
    nodes are visited only to keep traversal moving and are dropped from the
    result because they are never executed.
    """
    seen: set[str] = set()
    pending: deque[str] = deque(targets)
    while pending:
        node = pending.popleft()
        if node in seen:
            continue
        seen.add(node)
        for group in groups.get(node, []):
            for inp in group:
                if inp not in seen:
                    pending.append(inp)
    return sorted(node for node in seen if kinds.get(node) != "source")


def _group_masks(
    relevant: list[str],
    groups: dict[str, list[list[str]]],
) -> dict[str, list[int]]:
    """Map each relevant column to one bit-mask per candidate group.

    A mask only records non-source inputs: source inputs require no execution.
    """
    index = {name: i for i, name in enumerate(relevant)}
    masks: dict[str, list[int]] = {}
    for name in relevant:
        node_masks: list[int] = []
        for group in groups.get(name, []):
            mask = 0
            for inp in group:
                bit = index.get(inp)
                if bit is not None:
                    mask |= 1 << bit
            node_masks.append(mask)
        masks[name] = node_masks
    return masks


def _topological_order(
    selected: list[str],
    chosen_groups: dict[str, list[str]],
) -> list[str]:
    """Kahn sort of selected columns: inputs come before their dependents."""
    selected_set = set(selected)
    indegree: dict[str, int] = {name: 0 for name in selected}
    dependents: dict[str, list[str]] = {name: [] for name in selected}
    for name in selected:
        for inp in chosen_groups[name]:
            if inp in selected_set:
                indegree[name] += 1
                dependents[inp].append(name)

    # Sorted ready queue makes the order deterministic across runs.
    ready = deque(sorted(name for name, deg in indegree.items() if deg == 0))
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for dep in sorted(dependents[node]):
            indegree[dep] -= 1
            if indegree[dep] == 0:
                ready.append(dep)
    # The registered graph is acyclic, so every selected node is emitted.
    return order


def build_plan(
    targets: list[str],
    costs: dict[str, float],
    kinds: dict[str, str],
    groups: dict[str, list[list[str]]],
) -> dict:
    """Compute the globally minimum-cost recalculation plan.

    Input validation (unknown targets, incomplete / unexpected costs, the
    relevant-set limit) is performed by the service layer before calling this.
    """
    relevant = collect_relevant_non_source(targets, kinds, groups)
    n = len(relevant)
    if n > MAX_RELEVANT_NON_SOURCE:
        raise ValueError("relevant set exceeds the configured limit")

    index = {name: i for i, name in enumerate(relevant)}
    target_mask = 0
    for name in targets:
        target_mask |= 1 << index[name]

    masks = _group_masks(relevant, groups)

    best_cost: float | None = None
    best_subset = -1

    for subset in range(1 << n):
        if subset & target_mask != target_mask:
            continue
        bits = subset
        closed = True
        while bits:
            bit = bits & -bits
            i = bit.bit_length() - 1
            # Closed: this member has at least one group whose non-source
            # inputs are all contained in the subset.
            if not any(mask & subset == mask for mask in masks[relevant[i]]):
                closed = False
                break
            bits ^= bit
        if not closed:
            continue

        subset_cost = 0.0
        bits = subset
        while bits:
            bit = bits & -bits
            subset_cost += costs[relevant[bit.bit_length() - 1]]
            bits ^= bit
        # Deterministic tie-break: the numerically smallest winning bitmask,
        # which prefers lexicographically earlier columns when costs are equal.
        if best_cost is None or subset_cost < best_cost or (
            subset_cost == best_cost and subset < best_subset
        ):
            best_cost = subset_cost
            best_subset = subset

    if best_subset < 0:  # pragma: no cover - full-set closure guarantees one
        raise ValueError("no feasible plan exists for the requested targets")

    selected = [
        relevant[i] for i in range(n) if best_subset & (1 << i)
    ]

    chosen_index: dict[str, int] = {}
    chosen_groups: dict[str, list[str]] = {}
    for name in selected:
        # First feasible group; registration order defines the tie-break.
        gi = next(
            i
            for i, mask in enumerate(masks[name])
            if mask & best_subset == mask
        )
        chosen_index[name] = gi
        chosen_groups[name] = groups[name][gi]

    order = _topological_order(selected, chosen_groups)

    columns = [
        {
            "name": name,
            "kind": kinds[name],
            "group_index": chosen_index[name],
            "inputs": chosen_groups[name],
            "cost": float(costs[name]),
        }
        for name in selected
    ]
    return {
        "targets": sorted(set(targets)),
        "total_cost": float(best_cost),
        "columns": columns,
        "execution_order": order,
    }
