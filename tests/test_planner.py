"""Brute-force tests for the exact join planner."""

from __future__ import annotations

from fractions import Fraction
from decimal import Decimal
from itertools import combinations
import json
import math

import pytest

from joinplan.planner import JoinPlanner, PlannerError, plan_request


def leaf_tree(name: str) -> str:
    return json.dumps(name, ensure_ascii=False)


def split_children(tree: str) -> tuple[str, str]:
    """Split '(' left ',' right ')' at the single depth-zero comma."""
    depth = 0
    in_string = False
    escaped = False
    for position, char in enumerate(tree):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 1:
            return tree[1:position], tree[position + 1 : -1]
    raise AssertionError(f"not a join tree: {tree}")


def enumerate_trees(mask: int, dp: dict[int, list]) -> None:
    """Enumerate every full parenthesization whose merge has a crossing edge."""
    if mask in dp:
        return
    if mask & (mask - 1) == 0:
        dp[mask] = [(leaf_tree(f"T{mask.bit_length()-1}"), mask)]
        return

    results = []
    seen = set()
    left = (mask - 1) & mask
    while left:
        right = mask ^ left
        if left < right:
            enumerate_trees(left, dp)
            enumerate_trees(right, dp)
            for left_tree, _ in dp[left]:
                for right_tree, _ in dp[right]:
                    ordered = sorted((left_tree, right_tree))
                    tree = f"({ordered[0]},{ordered[1]})"
                    if tree not in seen:
                        seen.add(tree)
                        results.append((tree, mask))
        left = (left - 1) & mask
    dp[mask] = results


def evaluate_tree(tree, pair_sel):
    """Return (tree, leaves mask, rows, local cost, effective ids)."""
    if tree.startswith('"'):
        name = json.loads(tree)
        i = int(name[1:])
        return tree, 1 << i, Fraction(10 + i), Fraction(0), ()

    left_tree, right_tree = split_children(tree)
    left = evaluate_tree(left_tree, pair_sel)
    right = evaluate_tree(right_tree, pair_sel)
    left_mask, right_mask = left[1], right[1]

    effective = []
    selectivity = Fraction(1)
    a_nodes = left_mask
    while a_nodes:
        bit = a_nodes & -a_nodes
        a = bit.bit_length() - 1
        a_nodes ^= bit
        b_nodes = right_mask
        while b_nodes:
            other = b_nodes & -b_nodes
            b = other.bit_length() - 1
            b_nodes ^= other
            key = (a, b) if a < b else (b, a)
            if key in pair_sel:
                selectivity *= pair_sel[key][0]
                effective.extend(pair_sel[key][1])

    rows = left[2] * right[2] * selectivity
    cost = left[3] + right[3] + rows
    return tree, left_mask | right_mask, rows, cost, tuple(sorted(effective))


@pytest.mark.parametrize("n", [2, 3, 4])
def test_exhaustive_graphs_and_all_binary_trees(n):
    vertices = list(range(n))
    all_edges = list(combinations(vertices, 2))
    all_trees: dict[int, list] = {}
    full_mask = (1 << n) - 1
    enumerate_trees(full_mask, all_trees)
    assert len(all_trees[full_mask]) == _full_tree_count(n)

    # Include both one and two predicates on the same pair in the enumeration.
    for edge_mask in range(1 << len(all_edges)):
        predicates = []
        pair_sel = {}
        for edge_index, edge in enumerate(all_edges):
            if edge_mask & (1 << edge_index):
                # Deterministic, distinct rational factors.
                selectivity = Fraction(edge_index + 1, edge_index + 3)
                pid = f"p{edge[0]}{edge[1]}"
                predicates.append(
                    {
                        "id": pid,
                        "left": f"T{edge[0]}",
                        "right": f"T{edge[1]}",
                        "selectivity": {
                            "numerator": selectivity.numerator,
                            "denominator": selectivity.denominator,
                        },
                    }
                )
                pair_sel[edge] = (selectivity, [pid])

                # One graph has multiple predicates between the same table pair.
                if edge_index == 0:
                    second = Fraction(1, 7)
                    predicates.append(
                        {
                            "id": f"{pid}b",
                            "left": f"T{edge[0]}",
                            "right": f"T{edge[1]}",
                            "selectivity": [second.numerator, second.denominator],
                        }
                    )
                    pair_sel[edge] = (
                        pair_sel[edge][0] * second,
                        [pid, f"{pid}b"],
                    )

        request = {
            "tables": {f"T{i}": 10 + i for i in vertices},
            "predicates": predicates,
        }

        adjacency = [0 for _ in vertices]
        for a, b in pair_sel:
            adjacency[a] |= 1 << b
            adjacency[b] |= 1 << a
        connected = _is_connected(full_mask, adjacency)

        if connected:
            # The test enumerator generates every binary partition, but a tree
            # is legal only when at least one predicate crosses at every join.
            scored = [evaluate_tree(tree, pair_sel) for tree, _ in all_trees[full_mask]]
            legal = [
                item
                for item in scored
                if _is_legal_tree(item[0], pair_sel)
            ]
            assert legal

            best = min(
                legal,
                key=lambda item: (item[3], item[0].encode("ascii")),
            )
            result = plan_request(request)
            assert result["connected"] is True
            assert Fraction(result["cost"]["numerator"], result["cost"]["denominator"]) == best[3]
            assert result["tree"] == best[0]
            _assert_tree_metadata(result["root"], request, pair_sel)
        else:
            result = plan_request(request)
            assert result["connected"] is False
            flattened = sorted(name for component in result["components"] for name in component["tables"])
            assert flattened == [f"T{i}" for i in vertices]
            assert all(
                _is_component_connected(component["tables"], pair_sel, n)
                for component in result["components"]
            )
            assert len(result["components"]) == _component_count(adjacency)


def _full_tree_count(n: int) -> int:
    # Number of full binary rooted trees over n labeled leaves, with children
    # identified as an unordered pair.
    count = [0, 1]
    for size in range(2, n + 1):
        total = 0
        for left_size in range(1, size // 2 + 1):
            right_size = size - left_size
            if left_size == right_size:
                # Unordered equal-sized partitions of fixed labeled leaves.
                partitions = math.comb(size, left_size) // 2
            else:
                partitions = math.comb(size, left_size)
            total += partitions * count[left_size] * count[right_size]
        count.append(total)
    return count[n]


def _is_connected(mask, adjacency):
    first = mask & -mask
    seen = first
    frontier = first
    while frontier:
        bit = frontier & -frontier
        i = bit.bit_length() - 1
        frontier ^= bit
        new = adjacency[i] & mask & ~seen
        frontier |= new
        seen |= new
    return seen == mask


def _component_count(adjacency):
    n = len(adjacency)
    remaining = (1 << n) - 1
    count = 0
    while remaining:
        count += 1
        first = remaining & -remaining
        seen = first
        frontier = first
        while frontier:
            bit = frontier & -frontier
            i = bit.bit_length() - 1
            frontier ^= bit
            new = adjacency[i] & remaining & ~seen
            frontier |= new
            seen |= new
        remaining ^= seen
    return count


def _tree_mask(tree):
    if tree.startswith('"'):
        i = int(json.loads(tree)[1:])
        return 1 << i
    left, right = split_children(tree)
    return _tree_mask(left) | _tree_mask(right)


def _is_legal_tree(tree, pair_sel):
    if tree.startswith('"'):
        return True
    left, right = split_children(tree)
    if not _is_legal_tree(left, pair_sel) or not _is_legal_tree(right, pair_sel):
        return False
    return bool(_crossing_for_masks(_tree_mask(left), _tree_mask(right), pair_sel))


def _crossing_for_masks(left_mask, right_mask, pair_sel):
    ids = []
    left = left_mask
    while left:
        bit = left & -left
        a = bit.bit_length() - 1
        left ^= bit
        right = right_mask
        while right:
            other = right & -right
            b = other.bit_length() - 1
            right ^= other
            key = (a, b) if a < b else (b, a)
            if key in pair_sel:
                ids.extend(pair_sel[key][1])
    return sorted(ids)


def _is_component_connected(tables, pair_sel, n):
    indices = [int(name[1:]) for name in tables]
    mask = sum(1 << i for i in indices)
    adjacency = [0 for _ in range(n)]
    for a, b in pair_sel:
        adjacency[a] |= 1 << b
        adjacency[b] |= 1 << a
    return _is_connected(mask, adjacency)


def _assert_tree_metadata(node, request, pair_sel):
    names = list(request["tables"])
    if node["type"] == "leaf":
        i = names.index(node["table"])
        assert node["tree"] == leaf_tree(names[i])
        assert node["estimated_rows"] == {"numerator": 10 + i, "denominator": 1}
        return

    _assert_tree_metadata(node["left"], request, pair_sel)
    _assert_tree_metadata(node["right"], request, pair_sel)
    left_tree = node["left"]["tree"]
    right_tree = node["right"]["tree"]
    assert node["tree"] == f"({left_tree},{right_tree})"
    # Every internal node in the returned tree is byte ordered.
    assert left_tree.encode("ascii") < right_tree.encode("ascii")

    expected_effective = _crossing_for_masks(
        _tree_mask(left_tree), _tree_mask(right_tree), pair_sel
    )
    assert node["effective_predicates"] == expected_effective

    expected_rows = _expected_rows(node["tree"], request, pair_sel)
    assert Fraction(
        node["estimated_rows"]["numerator"],
        node["estimated_rows"]["denominator"],
    ) == expected_rows


def _expected_rows(tree, request, pair_sel):
    if tree.startswith('"'):
        return Fraction(request["tables"][json.loads(tree)])
    left, right = split_children(tree)
    selectivity = Fraction(1)
    for (a, b), (factor, _) in pair_sel.items():
        if ((1 << a) & _tree_mask(left) and (1 << b) & _tree_mask(right)) or (
            (1 << b) & _tree_mask(left) and (1 << a) & _tree_mask(right)
        ):
            selectivity *= factor
    return _expected_rows(left, request, pair_sel) * _expected_rows(
        right, request, pair_sel
    ) * selectivity


def test_multiple_predicates_same_pair_multiply_exactly():
    result = plan_request(
        {
            "tables": {"a": 100, "b": 10},
            "predicates": [
                {"id": "x", "left": "a", "right": "b", "selectivity": "1/2"},
                {"id": "y", "left": "b", "right": "a", "selectivity": [2, 6]},
            ],
        }
    )
    assert result["cost"] == {"numerator": 500, "denominator": 3}
    assert result["root"]["effective_predicates"] == ["x", "y"]


def test_zero_selectivity_tie_uses_byte_order_tree():
    request = {
        "tables": {"a": 3, "b": 5, "c": 7},
        "predicates": [
            {"id": "ab", "left": "a", "right": "b", "selectivity": 0},
            {"id": "bc", "left": "b", "right": "c", "selectivity": 0},
            {"id": "ac", "left": "a", "right": "c", "selectivity": 0},
        ],
    }
    result = plan_request(request)
    assert result["cost"] == {"numerator": 0, "denominator": 1}
    assert result["tree"] == '("a",("b","c"))'
    assert result["root"]["effective_predicates"] == ["ab", "ac"]


def test_disconnected_returns_components():
    result = plan_request(
        {
            "tables": {"a": 1, "b": 2, "c": 3, "d": 4},
            "predicates": [
                {"id": "ab", "left": "a", "right": "b", "selectivity": "1/2"},
                {"id": "cd", "left": "c", "right": "d", "selectivity": "1/2"},
            ],
        }
    )
    assert result == {
        "connected": False,
        "components": [
            {"tables": ["a", "b"]},
            {"tables": ["c", "d"]},
        ],
    }


def test_non_greedy_future_filter_wins():
    # A greedy "smallest intermediate first" can be forced away from the
    # globally optimal edge; subset DP checks all connected partitions.
    result = plan_request(
        {
            "tables": {
                "big": 1000,
                "medium": 100,
                "small": 10,
                "other": 20,
            },
            "predicates": [
                {"id": "bm", "left": "big", "right": "medium", "selectivity": "1/1000"},
                {"id": "so", "left": "small", "right": "other", "selectivity": "9/10"},
                {"id": "mo", "left": "medium", "right": "other", "selectivity": "1/10"},
            ],
        }
    )
    assert result["connected"] is True
    assert result["tree"] == '(("big","medium"),("other","small"))'


def test_validation_errors():
    with pytest.raises(PlannerError, match="between 2 and 9"):
        plan_request({"tables": {"a": 1}})

    with pytest.raises(PlannerError, match="references an unknown table"):
        plan_request(
            {
                "tables": {"a": 1, "b": 2},
                "predicates": [
                    {"id": "p", "left": "a", "right": "x", "selectivity": "1/2"}
                ],
            }
        )

    with pytest.raises(PlannerError, match="selectivity must be in"):
        plan_request(
            {
                "tables": {"a": 1, "b": 2},
                "predicates": [
                    {"id": "p", "left": "a", "right": "b", "selectivity": "3/2"}
                ],
            }
        )


def test_fraction_is_reduced_in_output():
    result = plan_request(
        {
            "tables": {"a": 2, "b": 3},
            "predicates": [
                {"id": "p", "left": "a", "right": "b", "selectivity": "2/4"}
            ],
        }
    )
    assert result["root"]["estimated_rows"] == {"numerator": 3, "denominator": 1}


def test_decimal_selectivity_is_exact():
    result = plan_request(
        {
            "tables": {"a": 10, "b": 4},
            "predicates": [
                {"id": "p", "left": "a", "right": "b", "selectivity": Decimal("0.25")}
            ],
        }
    )
    assert result["root"]["estimated_rows"] == {"numerator": 10, "denominator": 1}


def test_planner_class_is_exported_for_direct_use():
    assert JoinPlanner is not None
