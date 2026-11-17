# Backend evaluation (disposable research)

Evidence for [docs/backend-evaluation.md](../../docs/backend-evaluation.md). Not Gama code.

- `SPEC.md`: the shared prototype contract and experiments E1–E6
- `harness/`: fixture builder (reads `/storage/music`, writes elsewhere), work-copy script, neutral verifier
- `python/`, `go/`, `rust/`: the three prototypes (mutagen, go-taglib, lofty)
- `results/<candidate>/REPORT.md`: per-candidate findings; `results/REVIEW.md`: independent review (32 findings)

Reproduce: `python3 -I harness/build_fixtures.py /storage/music $EVAL`, then each candidate's scripts.
Fixtures and work copies are kept out of git (they are hundreds of MB).
