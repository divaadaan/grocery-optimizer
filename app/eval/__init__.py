"""Recipe evaluation harness (ROADMAP Track E).

Code the harness runs on, kept in the app package (not under ``tests/``)
because the runner, scorers and report built on it are tools in their own
right, and tests import them like any other module.

* :mod:`app.eval.snapshots` -- frozen, versioned deal/product snapshots and
  the offline ``GraphDeps`` fakes built from them.
* :mod:`app.eval.recorder` -- recording / replaying wrappers for the agents'
  chat models, keyed by request content so parallel calls replay correctly.
* :mod:`app.eval.runner` -- scenarios, record/replay runs of the real graph,
  and the run artifact (its format is documented there) that scorers read.
* :mod:`app.eval.scorers` -- deterministic scorers over an artifact and its
  snapshot, aggregated into a versioned ``RunScore`` with a PASS/FAIL/NA
  verdict gated on recomputed dietary compliance.
"""
