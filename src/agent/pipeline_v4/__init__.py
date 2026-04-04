"""
src/agent/pipeline_v4/ — Pipeline v4 (Architecture v4, MVP)

Components:
  state.py          — PipelineState + State Immutability Lock
  validation.py     — Validation Layer (COVERAGE_RULES + template consistency)
  final_validator.py — Final Validator (Python regex, VND normalization)
  audit.py          — Audit Trail (JSONL per session)
  orchestrator.py   — Main pipeline orchestrator tying all components

Strategy: develop song song với R25 (planner). Switch khi benchmark v4 ≥ R25.
"""
