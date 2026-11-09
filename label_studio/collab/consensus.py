"""Extensible agreement engine. Metrics are selected by annotation result type and can be registered/overridden."""

from itertools import combinations
from math import cos, isfinite, radians, sin

BBOX_TYPES = {'rectanglelabels', 'rectangle'}
SPAN_TYPES = {'labels'}
CHOICE_TYPES = {'choices'}

METRICS = {}


def metric(*types):
    def register(fn):
        for t in types:
            METRICS[t] = fn
        return fn

    return register


def span_iou(a, b):
    start = max(a.get('start', 0), b.get('start', 0))
    end = min(a.get('end', 0), b.get('end', 0))
    inter = max(0, end - start)
    union = (a.get('end', 0) - a.get('start', 0)) + (b.get('end', 0) - b.get('start', 0)) - inter
    return inter / union if union > 0 else 0.0


def html_span_iou(a, b):
    """HTML uses XPath boundaries; prefer editor-provided document-wide numeric offsets."""
    if a.get('globalOffsets') is not None and b.get('globalOffsets') is not None:
        return span_iou(a['globalOffsets'], b['globalOffsets'])
    if a.get('start') == a.get('end') == b.get('start') == b.get('end'):
        return span_iou(
            {'start': a.get('startOffset', 0), 'end': a.get('endOffset', 0)},
            {'start': b.get('startOffset', 0), 'end': b.get('endOffset', 0)},
        )
    fields = ('start', 'end', 'startOffset', 'endOffset')
    return float(all(a.get(k) == b.get(k) for k in fields))


def _image_aspect(*values):
    for value in values:
        try:
            width = float(value.get('_original_width', 0))
            height = float(value.get('_original_height', 0))
            if isfinite(width) and isfinite(height) and width > 0 and height > 0:
                return width / height
        except (TypeError, ValueError, OverflowError):
            continue
    return 1.0


def _rectangle_polygon(value, aspect):
    """Label Studio exports rotation around the rectangle's top-left anchor."""
    try:
        x, y, width, height = (float(value.get(key, 0)) for key in ('x', 'y', 'width', 'height'))
        rotation = float(value.get('rotation') or 0)
    except (TypeError, ValueError, OverflowError):
        return []
    if not all(isfinite(number) for number in (x, y, width, height, rotation)) or width <= 0 or height <= 0:
        return []
    angle = radians(rotation % 360)
    c, s = cos(angle), sin(angle)
    # X and Y percentages have different physical lengths on a non-square image.
    x, width = x * aspect, width * aspect
    polygon = [
        (x + dx * c - dy * s, y + dx * s + dy * c)
        for dx, dy in ((0, 0), (width, 0), (width, height), (0, height))
    ]
    return polygon if all(isfinite(number) for point in polygon for number in point) else []


def _polygon_area(polygon):
    if len(polygon) < 3:
        return 0.0
    area = abs(sum(
        ax * by - ay * bx
        for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1])
    )) / 2
    return area if isfinite(area) else 0.0


def _clip_convex_polygon(subject, clip):
    """Sutherland-Hodgman intersection of counterclockwise convex polygons."""
    epsilon = 1e-10
    for start, end in zip(clip, clip[1:] + clip[:1]):
        if not subject:
            break

        def distance(point):
            return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])

        output = []
        previous = subject[-1]
        previous_distance = distance(previous)
        for current in subject:
            current_distance = distance(current)
            previous_inside = previous_distance >= -epsilon
            current_inside = current_distance >= -epsilon
            if previous_inside != current_inside:
                denominator = previous_distance - current_distance
                if abs(denominator) > epsilon:
                    ratio = previous_distance / denominator
                    output.append((
                        previous[0] + ratio * (current[0] - previous[0]),
                        previous[1] + ratio * (current[1] - previous[1]),
                    ))
            if current_inside:
                output.append(current)
            previous, previous_distance = current, current_distance
        subject = output
    return subject


def bbox_iou(a, b):
    aspect = _image_aspect(a, b)
    polygon_a, polygon_b = _rectangle_polygon(a, aspect), _rectangle_polygon(b, aspect)
    area_a, area_b = _polygon_area(polygon_a), _polygon_area(polygon_b)
    if not area_a or not area_b:
        return 0.0
    intersection = min(area_a, area_b, _polygon_area(_clip_convex_polygon(polygon_a, polygon_b)))
    union = area_a + area_b - intersection
    return max(0.0, min(1.0, intersection / union)) if union > 0 else 0.0


def _labels(value, key):
    return frozenset(value.get(key) or [])


def _match_regions(items_a, items_b, iou, key):
    """Greedy one-to-one matching of regions carrying the same labels; returns the sum of IoUs."""
    used, total = set(), 0.0
    for a in items_a:
        best, best_j = 0.0, None
        for j, b in enumerate(items_b):
            if j in used or _labels(a, key) != _labels(b, key):
                continue
            score = iou(a, b)
            if score > best:
                best, best_j = score, j
        if best_j is not None:
            used.add(best_j)
            total += best
    return total


def _region_score(items_a, items_b, iou, key):
    if not items_a and not items_b:
        return 1.0
    # A comparison must not depend on which annotator happened to be inserted first.
    matched = max(_match_regions(items_a, items_b, iou, key), _match_regions(items_b, items_a, iou, key))
    return matched / max(len(items_a), len(items_b))


@metric(*CHOICE_TYPES)
def choices_metric(values_by_annotator):
    """Jaccard of the selected choices, plus per-label agreement (share of annotators agreeing on each label)."""
    sets = {k: set(c for v in vals for c in v.get('choices', [])) for k, vals in values_by_annotator.items()}
    pair_scores = []
    for (_, a), (_, b) in combinations(sets.items(), 2):
        pair_scores.append(len(a & b) / len(a | b) if (a | b) else 1.0)
    labels = set().union(*sets.values()) if sets else set()
    per_label = {}
    for label in sorted(labels):
        picks = sum(1 for s in sets.values() if label in s)
        per_label[label] = max(picks, len(sets) - picks) / len(sets)
    return pair_scores, per_label


@metric(*SPAN_TYPES)
def spans_metric(values_by_annotator):
    pair_scores = [
        _region_score(a, b, span_iou, 'labels')
        for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)
    ]
    return pair_scores, {}


@metric('hypertextlabels')
def html_spans_metric(values_by_annotator):
    return [
        _region_score(a, b, html_span_iou, 'hypertextlabels')
        for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)
    ], {}


@metric(*BBOX_TYPES)
def bbox_metric(values_by_annotator):
    pair_scores = [
        _region_score(a, b, bbox_iou, 'rectanglelabels')
        for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)
    ]
    return pair_scores, {}


def generic_metric(values_by_annotator):
    """Exact equality for result types without a dedicated metric."""
    pair_scores = [1.0 if a == b else 0.0 for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)]
    return pair_scores, {}


def compute_agreement(results_by_annotator):
    """`results_by_annotator`: {annotator_key: [Label Studio result items]}.

    Returns {'score': float|None, 'by_control': {from_name: {...}}}. `score` is None with fewer than 2 annotators.
    """
    if len(results_by_annotator) < 2:
        return {'score': None, 'by_control': {}}
    controls = {}
    for key, items in results_by_annotator.items():
        for item in items or []:
            rtype = item.get('type')
            value = item.get('value', {})
            if rtype in BBOX_TYPES:
                value = {
                    **(value if isinstance(value, dict) else {}),
                    '_original_width': item.get('original_width'),
                    '_original_height': item.get('original_height'),
                }
            controls.setdefault((item.get('from_name'), rtype), {}).setdefault(key, []).append(value)
    by_control, scores = {}, []
    for (from_name, rtype), per_annotator in sorted(controls.items(), key=lambda x: str(x[0])):
        # annotators who produced nothing for this control still take part in the comparison
        values = {key: per_annotator.get(key, []) for key in results_by_annotator}
        fn = METRICS.get(rtype, generic_metric)
        pair_scores, per_label = fn(values)
        score = sum(pair_scores) / len(pair_scores) if pair_scores else None
        if score is not None:
            scores.append(score)
        by_control[from_name] = {'type': rtype, 'score': score, 'per_label': per_label}
    overall = sum(scores) / len(scores) if scores else 1.0
    return {'score': overall, 'by_control': by_control}
