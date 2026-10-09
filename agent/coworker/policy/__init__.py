"""Policy: the decision for every side effect.

Import from the submodules directly:
    kernel.PolicyKernel   - the single decision point
    matrix.tier_default   - the tier table per autonomy level
    lexicon.classify_label- risk of a control label (buttons, menus)
    origin                - verbatim-from-content checks
    paths, prohibited, urls, shell_rules - pure checks used by the kernel and tools
"""
