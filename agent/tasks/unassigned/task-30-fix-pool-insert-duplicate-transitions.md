# Task 30: Fix Pool Insert Duplicate Transitions

**Status**: Not Started  
**Estimated Time**: 4-6 hours  
**Dependencies**: None  
**Design Reference**: None  
**Milestone**: Unassigned  

---

## Objective

Fix the pool insert system that creates duplicate/overlapping transitions on the same track, corrupting the render timeline. A global cleanup deleted 353 corrupt transitions in one session — the root cause must be fixed to prevent recurrence.

## Context

When pool segments or keyframes are inserted into the timeline, the system creates new transitions without checking for existing ones at the same timestamp/track. This produces:

1. **Overlapping stubs**: Small no-video transitions that overlap existing video transitions
2. **Zero-length transitions**: Transitions where from_kf.timestamp == to_kf.timestamp  
3. **Cross-track transitions**: Transitions on track_1 referencing keyframes on track_2/3 (109 found, migrated)
4. **Long ghost transitions**: Multi-minute no-video transitions on overlay tracks that obscure content

## Steps

### 1. Identify the pool insert codepath
- Find the endpoint(s) that handle pool segment/keyframe insertion
- Trace how transitions are created during insert
- Identify where the overlap check is missing

### 2. Add overlap detection before insert
- Before creating a new transition, query existing transitions on the same track
- Check if the new transition's time range overlaps any existing transition
- If overlap found: skip creation, or split the existing transition to accommodate

### 3. Add track validation
- Ensure new transitions are created on the same track as their from/to keyframes
- If keyframes are on different tracks, assign the transition to the `to` keyframe's track

### 4. Add zero-length guard
- Reject transitions where from_kf.timestamp >= to_kf.timestamp

### 5. Add a DB-level constraint or migration
- Consider a unique constraint or trigger to prevent future duplicates
- Run a cleanup pass on existing data as part of the migration

## Verification

- [ ] Pool insert no longer creates overlapping transitions
- [ ] Pool insert no longer creates zero-length transitions
- [ ] Pool insert assigns correct track_id based on keyframe tracks
- [ ] Existing corrupt data cleaned up
- [ ] Render pipeline produces clean output without manual cleanup
