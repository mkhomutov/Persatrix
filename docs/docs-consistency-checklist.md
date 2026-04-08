# Documentation Consistency Checklist

> **Quick-reference companion** to the [Documentation Guide](./documentation-guide.md).

---

## Pre-Update Checklist

Before making documentation changes:

- [ ] Read the [Documentation Guide](./documentation-guide.md) for comprehensive update guidance
- [ ] Review related documents to ensure consistency
- [ ] Identify the audience for your changes (users, developers, operators)

---

## Content Checklist

### Architecture Alignment

- [ ] Component names match current structure (`internal/`, `agents/`, `cli/`)
- [ ] Module references match actual directory layout
- [ ] Security boundaries (CLI ↔ Orchestrator ↔ Agents) are correctly described
- [ ] Data flow descriptions match architecture diagrams
- [ ] Protobuf contract references match `proto/*.proto`

### Feature Status

Use only the [standard status markers](./documentation-guide.md#status-markers):

- [ ] Mark implemented features with ✅ **Implemented**
- [ ] Mark in-progress features with 🚧 **In Progress**
- [ ] Mark limited features with ⚠️ **Partial**
- [ ] Mark unimplemented features with 📋 **Planned**
- [ ] Mark post-current-phase features with 🔮 **Future**
- [ ] Ensure status labels match current implementation state

### Security & Permissions

- [ ] Agent permission descriptions match `config/agents.yaml`
- [ ] Deny-by-default model accurately described
- [ ] Security gate descriptions match `internal/security/` implementation

---

## Cross-Reference Checklist

### Single Source of Truth (Anti-Duplication)

- [ ] Check the [ownership map](./documentation-guide.md#ownership-map) before adding content
- [ ] New content goes in the canonical document for that topic
- [ ] Cross-references use links to canonical docs (not copied content)
- [ ] If copying >3 lines from another doc, replace with a link to the source

### Link Validation

- [ ] All markdown links use relative paths
- [ ] All file links point to existing files
- [ ] All anchor links point to existing sections
- [ ] No broken links to removed/renamed files
- [ ] External links use HTTPS where possible

### Cross-Reference Completeness

- [ ] Each document has "Related Documentation" or "See Also" section
- [ ] Config files reference their JSON schemas
- [ ] Code references are accurate (file paths, function names)

---

## Review Checklist

Before approving documentation changes:

- [ ] Content is accurate and reflects current implementation
- [ ] No single-source-of-truth violations (duplicate content)
- [ ] Status markers are current
- [ ] Links are valid
- [ ] Terminology is consistent
- [ ] File size limits respected (code: ≤500 lines, docs: ≤3000 words)
