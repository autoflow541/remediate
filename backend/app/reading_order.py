"""Reading order validation and correction for PDF manifest nodes.

PDF structure trees require elements in logical reading order. Single-column
documents auto-tag correctly, but multi-column layouts (academic papers,
newsletters, course catalogs) are frequently tagged in DOM order, which
interleaves columns and produces unintelligible output for screen readers.

Algorithm:
  1. For each page, collect top-level manifest nodes with bounding boxes.
  2. Detect whether the page is multi-column by checking if node x-centers
     cluster into two or more horizontal bands separated by a gutter.
  3. Sort nodes: within a column, top-to-bottom (higher y first in PDF space).
     Across columns, left-to-right.
  4. Reorder the manifest nodes list to match.

Container nodes (Table, L, etc.) are treated as atomic units — their internal
children order is not touched.  Only top-level structural nodes are reordered.

PDF coordinate system note: y = 0 is the BOTTOM of the page.  Nodes at the top
of the page have HIGHER y values.  We sort by -y (descending y = top first).
"""

from __future__ import annotations

from collections import defaultdict

Y_BAND = 8.0        # pt — tolerance for grouping nodes on the "same line"
GUTTER_MIN = 36.0   # pt — minimum gap width that qualifies as a column gutter
MIN_COL_NODES = 2   # each detected column must have at least this many nodes


def _bbox_center_x(bbox: list) -> float:
    return (bbox[0] + bbox[2]) / 2.0


def _bbox_top_y(bbox: list) -> float:
    """Upper edge of the bounding box (higher value = higher on page)."""
    return max(bbox[1], bbox[3])


def _detect_columns(nodes_with_bbox: list[tuple]) -> list[tuple[float, float]]:
    """Return a list of (x_left, x_right) column ranges, sorted left-to-right.

    Uses a simple gap-detection heuristic: project all nodes onto the x-axis,
    sort by x-center, then look for gaps > GUTTER_MIN between consecutive nodes.
    Returns a single full-width column if no significant gap is found.
    """
    if not nodes_with_bbox:
        return []

    xs = sorted(_bbox_center_x(n[1]) for n in nodes_with_bbox)
    x_min = min(n[1][0] for n in nodes_with_bbox)
    x_max = max(n[1][2] for n in nodes_with_bbox)

    # Find gaps between consecutive x-center values
    gaps = []
    for i in range(1, len(xs)):
        gap = xs[i] - xs[i - 1]
        if gap >= GUTTER_MIN:
            gaps.append((xs[i - 1], xs[i]))  # (end_of_left_col, start_of_right_col)

    if not gaps:
        return [(x_min, x_max)]

    # Build column boundaries from gap midpoints
    boundaries = [x_min]
    for left_end, right_start in gaps:
        boundaries.append((left_end + right_start) / 2.0)
    boundaries.append(x_max)

    columns = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]
    return columns


def _node_column(bbox: list, columns: list[tuple[float, float]]) -> int:
    """Return the 0-based index of the column this node belongs to."""
    cx = _bbox_center_x(bbox)
    for i, (left, right) in enumerate(columns):
        if left <= cx <= right:
            return i
    # Fallback: assign to nearest column
    return min(range(len(columns)), key=lambda i: abs(cx - (columns[i][0] + columns[i][1]) / 2))


def sort_nodes_by_reading_order(nodes: list[dict]) -> list[dict]:
    """Return a new list of top-level nodes sorted into logical reading order.

    Nodes without bounding boxes are placed after all positioned nodes in their
    original relative order.
    """
    if not nodes:
        return nodes

    # Separate nodes with and without bboxes
    with_bbox = [(i, n) for i, n in enumerate(nodes) if n.get("bbox") and len(n["bbox"]) == 4]
    without_bbox = [n for n in nodes if not (n.get("bbox") and len(n.get("bbox", [])) == 4)]

    if not with_bbox:
        return nodes

    # Group by page first
    pages: dict[int, list] = defaultdict(list)
    for orig_idx, node in with_bbox:
        page = int(node.get("page", 1))
        pages[page].append((orig_idx, node, node["bbox"]))

    sorted_nodes: list[dict] = []
    for page_num in sorted(pages):
        page_nodes = pages[page_num]  # list of (orig_idx, node, bbox)
        columns = _detect_columns([(n, b) for _, n, b in page_nodes])

        if len(columns) <= 1:
            # Single column — sort top-to-bottom
            page_nodes.sort(key=lambda t: -_bbox_top_y(t[2]))
        else:
            # Multi-column — sort by (column index, -y)
            page_nodes.sort(
                key=lambda t: (_node_column(t[2], columns), -_bbox_top_y(t[2]))
            )

        sorted_nodes.extend(node for _, node, _ in page_nodes)

    return sorted_nodes + without_bbox


def fix_reading_order(manifest: dict) -> tuple[dict, int]:
    """Reorder top-level manifest nodes into logical reading order.

    Returns (updated_manifest, number_of_nodes_reordered).
    """
    original = manifest.get("nodes", [])
    reordered = sort_nodes_by_reading_order(original)

    # Count how many moved
    changed = sum(1 for a, b in zip(original, reordered) if a is not b)

    return {**manifest, "nodes": reordered}, changed
