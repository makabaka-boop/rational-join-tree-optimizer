"""Exact optimizer for the JSON join-plan service.

The planner uses subset dynamic programming.  Every legal partition of a
connected subset is considered, so it does not make the greedy mistake of
merely joining the two currently smallest base relations.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any


class PlannerError(ValueError):
    """Raised when the JSON request does not satisfy the planner contract."""


def _is_ascii_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0 and value.isascii()


def _parse_positive_int(value: Any, what: str) -> int:
    # bool is a subtype of int; explicitly reject it.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PlannerError(f"{what} must be a positive integer")
    return value


def _parse_selectivity(value: Any, where: str) -> Fraction:
    """Parse and reduce a rational selectivity in [0, 1]."""
    if isinstance(value, bool):
        raise PlannerError(f"{where} selectivity must be rational in [0,1]")

    if isinstance(value, int):
        numerator = value
        denominator = 1
    elif isinstance(value, float):
        # The service parses JSON decimals as Decimal.  This branch keeps direct
        # Python callers usable while retaining the short decimal representation.
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise PlannerError(f"{where} selectivity must be finite")
        fraction = Fraction(decimal_value)
        if fraction < 0 or fraction > 1:
            raise PlannerError(f"{where} selectivity must be in [0,1]")
        return fraction
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise PlannerError(f"{where} selectivity must be finite")
        fraction = Fraction(value)
        if fraction < 0 or fraction > 1:
            raise PlannerError(f"{where} selectivity must be in [0,1]")
        return fraction
    elif isinstance(value, dict):
        if set(value) != {"numerator", "denominator"}:
            raise PlannerError(
                f"{where} selectivity must contain numerator and denominator"
            )
        numerator = value["numerator"]
        denominator = value["denominator"]
        if isinstance(numerator, bool) or isinstance(denominator, bool):
            raise PlannerError(f"{where} selectivity numerator and denominator must be integers")
        if not isinstance(numerator, int) or not isinstance(denominator, int):
            raise PlannerError(f"{where} selectivity numerator and denominator must be integers")
    elif isinstance(value, list):
        if len(value) != 2 or any(isinstance(x, bool) for x in value) or not all(
            isinstance(x, int) for x in value
        ):
            raise PlannerError(
                f"{where} list selectivity must contain exactly two integers"
            )
        numerator, denominator = value
    elif isinstance(value, str):
        parts = value.split("/")
        if len(parts) != 2:
            raise PlannerError(f"{where} string selectivity must look like '1/3'")
        try:
            numerator = int(parts[0], 10)
            denominator = int(parts[1], 10)
        except ValueError as exc:
            raise PlannerError(f"{where} string selectivity must contain integers") from exc
    else:
        raise PlannerError(f"{where} selectivity must be rational in [0,1]")

    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise PlannerError(f"{where} selectivity must be in [0,1] with a positive denominator")

    return Fraction(numerator, denominator)


def _rational_dict(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


@dataclass(frozen=True)
class Predicate:
    id: str
    left: str
    right: str
    selectivity: Fraction


@dataclass(frozen=True)
class Candidate:
    rows: Fraction
    cost: Fraction
    tree: str
    node: tuple[Any, ...]
    effective: tuple[str, ...]


class JoinPlanner:
    def __init__(self, request: dict[str, Any]):
        tables = request.get("tables")
        if not isinstance(tables, dict):
            raise PlannerError("request must contain a 'tables' object")
        if not 2 <= len(tables) <= 9:
            raise PlannerError("the number of unique tables must be between 2 and 9")

        self.names: list[str] = []
        self.base_rows: list[int] = []
        for name, rows in tables.items():
            if not _is_ascii_str(name):
                raise PlannerError("every table name must be a non-empty ASCII string")
            self.names.append(name)
            self.base_rows.append(_parse_positive_int(rows, f"table {name!r}"))

        if len(set(self.names)) != len(self.names):
            raise PlannerError("table names must be unique")

        # A fixed index also makes generated predicate identifiers stable.
        self.index = {name: i for i, name in enumerate(self.names)}
        self.n = len(self.names)
        self.predicates = self._parse_predicates(request.get("predicates", []))

        # Product of base table cardinalities, indexed by subset mask.
        self.base_product = [Fraction(1) for _ in range(1 << self.n)]
        for mask in range(1, 1 << self.n):
            bit = mask & -mask
            i = bit.bit_length() - 1
            self.base_product[mask] = self.base_product[mask ^ bit] * self.base_rows[i]

        # Product of all predicate selectivities fully contained in a subset.
        self.internal_selectivity = [Fraction(1) for _ in range(1 << self.n)]
        # predicate_ids_by_pair is sorted by predicate id byte order.
        self.predicate_ids_by_pair: dict[tuple[int, int], list[str]] = {}
        self.predicate_by_id: dict[str, Predicate] = {}
        self.selectivity_by_pair: dict[tuple[int, int], Fraction] = {}

        for predicate in self.predicates:
            if predicate.id in self.predicate_by_id:
                raise PlannerError(f"duplicate predicate id {predicate.id!r}")
            self.predicate_by_id[predicate.id] = predicate

            a = self.index[predicate.left]
            b = self.index[predicate.right]
            pair = (a, b) if a < b else (b, a)
            self.predicate_ids_by_pair.setdefault(pair, []).append(predicate.id)
            self.selectivity_by_pair[pair] = (
                self.selectivity_by_pair.get(pair, Fraction(1)) * predicate.selectivity
            )

        # A pair's predicates are internal to every subset containing both
        # endpoints.  Adding them by endpoint bit masks is valid because all
        # predicates are binary.
        for (a, b), selectivity in self.selectivity_by_pair.items():
            pair_mask = (1 << a) | (1 << b)
            complement = ((1 << self.n) - 1) ^ pair_mask
            sub = complement
            while True:
                self.internal_selectivity[pair_mask | sub] *= selectivity
                if sub == 0:
                    break
                sub = (sub - 1) & complement

        self.predicate_ids_by_pair = {
            pair: sorted(ids, key=lambda item: item.encode("ascii"))
            for pair, ids in self.predicate_ids_by_pair.items()
        }
        self.neighbor_mask = [0 for _ in range(self.n)]
        for a, b in self.selectivity_by_pair:
            self.neighbor_mask[a] |= 1 << b
            self.neighbor_mask[b] |= 1 << a

    def _parse_predicates(self, raw: Any) -> list[Predicate]:
        if not isinstance(raw, list):
            raise PlannerError("'predicates' must be a list")

        predicates: list[Predicate] = []
        seen_ids: set[str] = set()
        for position, item in enumerate(raw, start=1):
            where = f"predicate {position}"
            if not isinstance(item, dict):
                raise PlannerError(f"{where} must be an object")

            missing = {"id", "left", "right", "selectivity"} - set(item)
            if missing:
                raise PlannerError(f"{where} is missing {sorted(missing)}")

            predicate_id = item["id"]
            if not _is_ascii_str(predicate_id):
                raise PlannerError(f"{where} id must be a non-empty ASCII string")
            if predicate_id in seen_ids:
                raise PlannerError(f"duplicate predicate id {predicate_id!r}")
            seen_ids.add(predicate_id)

            left = item["left"]
            right = item["right"]
            if not _is_ascii_str(left) or not _is_ascii_str(right):
                raise PlannerError(f"{where} endpoints must be non-empty ASCII strings")
            if left not in self.index or right not in self.index:
                raise PlannerError(f"{where} references an unknown table")
            if left == right:
                raise PlannerError(f"{where} must connect two different tables")

            predicates.append(
                Predicate(
                    id=predicate_id,
                    left=left,
                    right=right,
                    selectivity=_parse_selectivity(item["selectivity"], where),
                )
            )

        return predicates

    def _components(self) -> list[list[int]]:
        remaining = (1 << self.n) - 1
        components: list[list[int]] = []
        while remaining:
            bit = remaining & -remaining
            seed = bit.bit_length() - 1
            reachable = bit
            frontier = bit
            while frontier:
                current_bit = frontier & -frontier
                current = current_bit.bit_length() - 1
                frontier ^= current_bit
                new_nodes = self.neighbor_mask[current] & remaining & ~reachable
                frontier |= new_nodes
                reachable |= new_nodes
            components.append([i for i in range(self.n) if reachable & (1 << i)])
            remaining ^= reachable
        components.sort(
            key=lambda component: tuple(self.names[i].encode("ascii") for i in component)
        )
        return components

    def _rows_for(self, mask: int) -> Fraction:
        return self.base_product[mask] * self.internal_selectivity[mask]

    def _crossing_predicates(self, left_mask: int, right_mask: int) -> tuple[str, ...]:
        result: list[str] = []
        # Iterating only vertices on one side costs at most nine edge scans.
        left_nodes = left_mask
        while left_nodes:
            bit = left_nodes & -left_nodes
            a = bit.bit_length() - 1
            left_nodes ^= bit
            candidates = self.neighbor_mask[a] & right_mask
            while candidates:
                other_bit = candidates & -candidates
                b = other_bit.bit_length() - 1
                candidates ^= other_bit
                pair = (a, b) if a < b else (b, a)
                result.extend(self.predicate_ids_by_pair.get(pair, ()))
        return tuple(sorted(result, key=lambda item: item.encode("ascii")))

    def optimize(self) -> dict[str, Any]:
        components = self._components()
        if len(components) != 1:
            component_payload = []
            for component in components:
                ordered_component = sorted(
                    component, key=lambda i: self.names[i].encode("ascii")
                )
                component_payload.append(
                    {"tables": [self.names[i] for i in ordered_component]}
                )
            component_payload.sort(
                key=lambda component: [
                    name.encode("ascii") for name in component["tables"]
                ]
            )
            return {"connected": False, "components": component_payload}

        full_mask = (1 << self.n) - 1
        leaves = [
            Candidate(
                rows=Fraction(self.base_rows[i]),
                cost=Fraction(0),
                tree=_json_leaf(self.names[i]),
                node=("leaf", i),
                effective=(),
            )
            for i in range(self.n)
        ]
        dp: list[Candidate | None] = [None] * (1 << self.n)
        for i, leaf in enumerate(leaves):
            dp[1 << i] = leaf

        for mask in range(1, 1 << self.n):
            if mask & (mask - 1) == 0:
                continue

            # A subtree must itself be predicate-connected.
            if self._crossing_connected(mask) is False:
                continue

            rows = self._rows_for(mask)
            best: Candidate | None = None
            left_mask = (mask - 1) & mask
            while left_mask:
                right_mask = mask ^ left_mask
                # Keep one canonical orientation per partition.  The children
                # are later byte-ordered regardless of their partition role.
                if left_mask < right_mask:
                    left = dp[left_mask]
                    right = dp[right_mask]
                    if left is not None and right is not None:
                        effective = self._crossing_predicates(left_mask, right_mask)
                        if effective:
                            ordered = sorted(
                                (left, right),
                                key=lambda candidate: candidate.tree.encode("ascii"),
                            )
                            tree = "(" + ordered[0].tree + "," + ordered[1].tree + ")"
                            candidate = Candidate(
                                rows=rows,
                                cost=ordered[0].cost + ordered[1].cost + rows,
                                tree=tree,
                                node=(
                                    "join",
                                    ordered[0].node,
                                    ordered[1].node,
                                    effective,
                                ),
                                effective=effective,
                            )
                            if best is None or (
                                candidate.cost,
                                candidate.tree.encode("ascii"),
                            ) < (best.cost, best.tree.encode("ascii")):
                                best = candidate

                left_mask = (left_mask - 1) & mask

            dp[mask] = best

        root = dp[full_mask]
        if root is None:  # Defensive: a connected graph always has a legal tree.
            raise PlannerError("failed to construct a join tree")

        return {
            "connected": True,
            "cost": _rational_dict(root.cost),
            "tree": root.tree,
            "root": self._node_to_json(root.node),
        }

    def _crossing_connected(self, mask: int) -> bool:
        first = mask & -mask
        seen = first
        frontier = first
        available = mask
        while frontier:
            bit = frontier & -frontier
            i = bit.bit_length() - 1
            frontier ^= bit
            new_nodes = self.neighbor_mask[i] & available & ~seen
            frontier |= new_nodes
            seen |= new_nodes
        return seen == mask

    def _node_to_json(self, node: tuple[Any, ...]) -> dict[str, Any]:
        kind = node[0]
        if kind == "leaf":
            i = node[1]
            tree = _json_leaf(self.names[i])
            return {
                "type": "leaf",
                "table": self.names[i],
                "tree": tree,
                "estimated_rows": _rational_dict(Fraction(self.base_rows[i])),
            }

        _, left_node, right_node, effective = node
        left_json = self._node_to_json(left_node)
        right_json = self._node_to_json(right_node)

        # The child tree strings here are exactly those in the optimal
        # Candidate and therefore are already byte-ordered.
        left_tree = left_json["tree"]
        right_tree = right_json["tree"]
        return {
            "type": "join",
            "tree": "(" + left_tree + "," + right_tree + ")",
            "left": left_json,
            "right": right_json,
            "effective_predicates": list(effective),
            "estimated_rows": _rational_dict(
                self._rows_for(self._node_mask(node))
            ),
        }

    def _node_mask(self, node: tuple[Any, ...]) -> int:
        if node[0] == "leaf":
            return 1 << node[1]
        return self._node_mask(node[1]) | self._node_mask(node[2])


def _json_leaf(name: str) -> str:
    # json.dumps guarantees an unambiguous ASCII leaf representation and
    # escapes backslashes/quotes in arbitrary ASCII table names.
    import json

    return json.dumps(name, ensure_ascii=False)


def plan_request(request: Any) -> dict[str, Any]:
    """Validate a decoded JSON request and return the optimal plan envelope."""
    if not isinstance(request, dict):
        raise PlannerError("request must be a JSON object")
    return JoinPlanner(request).optimize()
