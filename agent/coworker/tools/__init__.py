"""Tool layer. Each tool module exports ``SPECS``; see registry.py.

This package deliberately imports none of its tool modules: ``load_all()``
imports them explicitly, so one broken module cannot break the others at import.
"""
