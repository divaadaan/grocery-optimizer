"""Recipe evaluation harness (ROADMAP Track E).

Code the harness runs on, kept in the app package (not under ``tests/``)
because the runner, scorers and report built on it are tools in their own
right, and tests import them like any other module.

* :mod:`app.eval.snapshots` -- frozen, versioned deal/product snapshots and
  the offline ``GraphDeps`` fakes built from them.
"""
