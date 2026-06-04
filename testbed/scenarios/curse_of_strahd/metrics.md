# Quality Metrics — Curse of Strahd

## Story Quality
- The world seed should create a Gothic horror setting (mists, Barovia, Castle Ravenloft hint).
- Characters should experience fear, doubt, and resilience consistent with their descriptions.
- NPCs (Ireena, Ismark, Strahd, Vistani) should appear and be persistent once introduced.
- The story should progress logically: Village → Road → Castle hints.
- No character should be narratively alive after being killed or mortally wounded.

## World Seed Expectations
- WORLD_SEED must generate at least one location (the Village of Barovia).
- All four player characters must be placed in the same starting location.
- World time must be set to a reasonable evening/night hour.

## Storage Integrity
- Every NPC mentioned by name in narration must exist in npc.json.
- No duplicate location entries (same place under different names).
- No "Auto-created placeholder" entries remain after SA maintenance.
- Character locations must stay consistent with scene locations.

## Agent Behaviour
- SA must call create_npc for newly introduced beings.
- Characters must call relationship_update after meeting significant NPCs.
- No turn should stall (same GM correction repeated >5 times for same character).
- Each character must complete their turn within the scene (acted = true).

## Error Conditions (write to errors.txt)
- Missing expected NPCs (Ireena, Ismark, Strahd if encountered).
- Duplicate locations with "placeholder" summary.
- Character appears in narration after confirmed death.
- Characters at location X when scene occurs at location Y (drift).
- GM correction loop exceeding per-character limit (5).
- SA writes to auto-managed fields (location, last_active, last_acted).
