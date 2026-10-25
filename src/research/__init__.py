"""Research Desk — daily research + advisory layer (advisory-only).

Modules:
  personas — the single source of truth for the four expert persona prompts
  collect  — trust-tiered, fail-closed input collectors
  brief    — advisor → risk-auditor brief builder (Gemini, template fallback)
  deliver  — DB persistence + digest-email section

Nothing in this package may place, queue, or automate a trade. See
docs/RESEARCH_DESK_BUILD_PLAN.md and docs/RESEARCH_DESK_SECURITY.md.
"""
