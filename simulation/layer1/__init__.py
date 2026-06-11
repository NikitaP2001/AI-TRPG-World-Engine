"""Layer 1 — Causal simulation layer.

Continuous field-based simulation where features interact through
shared physical fields (water_table, biomass, soil_moisture, etc.).

Architecture:
  FieldRegistry      — holds all continuous fields (KDTree-IDW)
  MutableField       — field with per-tick effect stack
  Feature (base)     — reads fields, writes effects
  SimEngine           — runs ticks: compute effects → apply → update geometry
"""
