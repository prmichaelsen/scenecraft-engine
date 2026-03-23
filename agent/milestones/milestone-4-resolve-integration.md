# Milestone 4: Resolve Integration & Distribution

**Goal**: Package as a Resolve script, add end-to-end workflow, and prepare for distribution
**Duration**: 1 week
**Dependencies**: M3 - Effect Library & Intelligence
**Status**: Not Started

---

## Overview

This milestone packages the tool for real-world use. It includes a Resolve-native script that runs from the Workspace > Scripts menu, a streamlined end-to-end workflow, README documentation, and installation instructions.

---

## Deliverables

### 1. Resolve Script Integration
- Python script for Resolve's Scripts folder
- Workspace > Scripts menu integration
- Resolve API interaction for timeline frame rate detection (if available)

### 2. Distribution Package
- README with installation and usage instructions
- Requirements.txt or pip-installable package
- Example outputs and demo workflow
- Cross-platform install instructions (Windows, macOS, Linux)

---

## Success Criteria

- [ ] Script runs from Resolve's Workspace > Scripts menu
- [ ] End-to-end workflow documented and tested
- [ ] README covers installation, usage, and troubleshooting
- [ ] Works on at least 2 platforms (Linux + one other)

---

## Tasks

1. [Task 10: Resolve Script Packaging](../tasks/milestone-4-resolve-integration/task-10-resolve-script.md) - Package for Resolve Scripts folder, menu integration
2. [Task 11: Documentation & Distribution](../tasks/milestone-4-resolve-integration/task-11-documentation.md) - README, install guide, examples, demo workflow

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Resolve scripting API differences across versions | Medium | Medium | Test with Resolve 18+; provide fallback instructions |
| Platform-specific path differences | Low | Medium | Document per-platform script folder locations |

---

**Blockers**: None
