"""Package initialization for OpenBox parity modules."""

# Keep flat `import parity_*` working via the root shims (parity_*.py).
# The shims already alias `sys.modules["parity_*"]` to `pkg.parity.parity_*`
# on first flat import, so no eager import is needed here. Eagerly
# importing every parity_*.py at package init creates circular imports
# (e.g. parity_perf -> parity_gamescope -> pkg.parity -> parity_perf).
