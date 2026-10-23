"""Queue preference-pair tasks for the gesture suite (ADR 0049), from its own seeded plan.

Called by `seed.ts::seedReviewTasks` before each Review test, into the suite's ledger
(`LABELS_DIR`) and never a runner's. Written in Python with the real `PairTask` and
`Candidate` types rather than as JSON in TypeScript: a hand-written task would be a second
copy of the schema, and the page would be tested against the copy.

The two lines are the seeded `synthetic-hazards` route and the same route bowed north over
its middle third, so they part somewhere the page has to draw. Their summaries are chosen,
not measured - a street line and a trail line - because what is under test is the page, and
`test_label_pairs.py` owns what a real pair is made of.

    uv run python ui/gestures/seed_review.py --plan PLAN.json --labels DIR --count 2
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from longrun.core.labels.ledger import Ledger
from longrun.core.labels.pairs import Candidate, PairTask
from longrun.core.models.geometry import Route
from longrun.core.models.plan import Plan
from longrun.core.routing.tuning import RouteSummary, WayClass

#: How far north the middle third of line B is moved: about 220 m, well past the 25 m a
#: `route_diff` calls the same street.
BOW_DEGREES = 0.002


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()

    route = Plan.model_validate_json(args.plan.read_text(encoding="utf-8")).route
    third = len(route.points) // 3
    bowed = Route(
        id="bowed",
        points=[
            point.model_copy(update={"lat": point.lat + BOW_DEGREES})
            if third <= index <= 2 * third
            else point
            for index, point in enumerate(route.points)
        ],
    )
    streets = RouteSummary.from_ways("a", [(WayClass(lts=3), route.length_m)])
    trail = RouteSummary.from_ways("b", [(WayClass(unpaved=True, is_path=True), route.length_m)])
    start, end = route.points[0], route.points[-1]

    ledger = Ledger(args.labels)
    for number in range(1, args.count + 1):
        ledger.add_task(
            PairTask(
                id=f"gesture-{number:02d}",
                region=None,
                ends=((start.lat, start.lon), (end.lat, end.lon)),
                a=Candidate.of(f"ga{number:02d}", "gesture", route, streets),
                b=Candidate.of(f"gb{number:02d}", "gesture", bowed, trail),
                stratum="path_bonus",
                split=0.5,
                created_at=datetime.now(UTC).replace(microsecond=0),
            )
        )


if __name__ == "__main__":
    main()
