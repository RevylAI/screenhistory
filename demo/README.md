# Voyage demo

Live example against the Revyl app **Voyage** (`4cf9100b-2d6d-4771-bbc2-213278f0864e`):
one screen (`trips_list`) shipped across 5 builds, each a small deliberate visual change.

```bash
cd demo
export PYTHONPATH=../src
python3 -m atlas_review.cli builds
python3 -m atlas_review.cli history trips_list
python3 -m atlas_review.cli blame trips_list --region 0.04,0.175,0.90,0.045 --threshold 0.05
python3 -m atlas_review.cli check --markdown
python3 -m atlas_review.cli serve
```

`.atlas-review.json` holds the app id and the policy (1% change budget on
`trips_list`, approval required, price column and title watched).
`.atlas-review/review.json` holds the approvals: builds 1-3 and 5 approved,
build 4 rejected for low-contrast strike-through pricing.

`examples/` has the three render modes for the cumulative #1 -> #5 diff.
