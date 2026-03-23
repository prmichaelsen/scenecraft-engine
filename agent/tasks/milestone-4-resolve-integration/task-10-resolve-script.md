# Task 10: Resolve Script Packaging

**Milestone**: [M4 - Resolve Integration & Distribution](../../milestones/milestone-4-resolve-integration.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 3 hours
**Dependencies**: Task 6
**Status**: Not Started

---

## Objective

Package the beat-lab tool as a DaVinci Resolve script that can be run from the Workspace > Scripts menu, with proper installation to Resolve's scripts directory.

---

## Steps

### 1. Create Resolve Script Entry Point

`resolve_script.py`:
- Detect Resolve's Python API availability
- If available, get timeline frame rate from current project
- Prompt user for audio file path (or use file dialog if GUI available)
- Run analysis and generation pipeline
- Output .setting file to user-specified location

### 2. Installation Script

- Detect Resolve scripts folder per platform:
  - Windows: `%AppData%/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Comp/`
  - macOS: `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Comp/`
  - Linux: `~/.local/share/DaVinciResolve/Fusion/Scripts/Comp/`
- Copy/symlink script to appropriate location
- Verify dependencies are available to Resolve's Python

### 3. Resolve API Integration (Optional Enhancement)

- If Resolve scripting API available:
  - Read timeline FPS automatically
  - Get audio file from timeline media pool
  - Import generated .setting directly into Fusion page
- Graceful fallback if API not available

### 4. Test in Resolve

- Install script to Scripts folder
- Launch from Workspace > Scripts menu
- Verify end-to-end workflow within Resolve

---

## Verification

- [ ] Script appears in Resolve's Workspace > Scripts menu
- [ ] Script runs without error when launched from Resolve
- [ ] Correct scripts folder detected per platform
- [ ] Installation script copies files to correct location
- [ ] Fallback works when Resolve API is not available

---

**Next Task**: [Task 11: Documentation & Distribution](task-11-documentation.md)
