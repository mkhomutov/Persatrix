"""The EXP-001 harness: runs the pre-registered experiment and scores it.

EXP-001 compares five ways of getting advice on a plan, from one model call
to a governed persona discussion with memory. What it runs, and how the
scores decide, is fixed in ``docs/experiments/EXP-001-preregistration.md``
and its part 2; this package only carries those documents out.

- :mod:`evaluators.exp001.materials` reads and checks the series files.
- :mod:`evaluators.exp001.costs` records each model call, prices it with the
  fixed table, and draws each series' arm order.

The package imports nothing from the persona runtime, so its pure parts can
be tested and used without starting any agent.
"""
