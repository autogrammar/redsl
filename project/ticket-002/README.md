# Ticket 002: Adopt pinned Wellman metadata validation in ReDSL

- **ID**: ticket-002
- **Owner**: agent:codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-10-02

## Goal and scope

Pinned dependency-free Wellman metadata checks and repair of the target-owned ticket index. Preserve all unknown primary source and Planfile changes.

## Acceptance criteria

- [x] AC-01: Pinned requirements and Docs metadata validate.
- [x] AC-02: Managed scoped governance and existing product checks pass.
- [ ] AC-03: Independent protected publication completes.

## Validation

Pinned Wellman metadata: valid, 0 findings. Managed gate: 0 errors, 0 warnings. Product defaults: 688 passed, 8 skipped, 122 deselected (slow/e2e/integration excluded by repository configuration); isolated verification environment, no primary environment changes. Generated test artifacts were preserved privately and restored to their exact pre-test bytes. Protected publication remains pending profile enrollment.
