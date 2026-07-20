---
name: prd-builder
description: Interview the user section-by-section to build a complete, well-reasoned Product Requirements Document (PRD) from scratch, then write it to a markdown file. Trigger for requests to write, draft, or spec out a new PRD, feature spec, or one-pager for a product/feature being built — including vague asks like "help me plan out this feature" or "help me think through this product concept," even without the words "PRD" or "requirements doc." Do NOT draft a full PRD from a one-liner — the point is asking clarifying questions first. Do NOT trigger for editing/reformatting an existing PRD, handing over a blank template, meta-questions about how PRDs are written, or one-pagers/summaries unrelated to a product the user is building (e.g. a competitor summary).
---

# PRD Builder

Write PRDs by interviewing first, drafting second. A PRD written from a one-line idea is full of the author's guesses; the value of this skill is replacing those guesses with the user's actual answers before a single section gets drafted.

## The core idea

Don't draft anything until you understand the whole picture. Work through the sections below one at a time, in order, asking a small number of focused questions per section (2-4 is usually right — avoid dumping ten questions at once). After each section, briefly reflect back what you heard in a sentence or two, so the user can correct you before you move on. That correction loop is what actually produces accuracy, not the volume of questions.

Adapt to how much the user already gave you:
- If their initial message already answers a section, don't ask it again — summarize what you inferred and ask them to confirm or correct it.
- If an answer is thin or generic ("improve user engagement"), push once for specifics ("engagement measured how — DAU, session length, a specific action?") rather than accepting a vague answer and moving on.
- If the user says "just use your best guess" for a section, note the assumption explicitly in the final doc rather than silently treating it as fact.

Skip a section entirely if it plainly doesn't apply (e.g., no monetization angle for an internal tool) — note the skip rather than forcing a question.

## Sections to work through

Ask about these roughly in order. Each maps to a section of the final PRD.

### 1. Problem & context
What's broken or missing today? Who feels this pain, how often, and how badly? Why solve it now rather than later? What prompted this — a metric, a complaint, a competitive gap, a strategic bet?

### 2. Goals & non-goals
What does success look like if this ships? Just as important: what is explicitly out of scope, so reviewers don't assume the PRD is promising more than it is.

### 3. Target users & use cases
Who is this for — one persona or several? Walk through the primary use case(s) as a short scenario or user story, not just a label like "power users."

### 4. Requirements
Functional: what must the thing actually do? Draw these out as discrete, testable statements, not a paragraph of prose.
Non-functional: performance, scale, security/privacy, accessibility, platform constraints — whatever applies. Skip categories that don't.

### 5. Success metrics
How will the team know this worked, concretely? Push for a number or a named metric over "increase engagement." Ask about both the target metric and any guardrail metrics (things that shouldn't regress).

### 6. Scope, timeline & dependencies
What's in v1 vs. later phases? Any hard deadline or dependency (another team, a platform, a launch event)? What teams or systems does this touch?

### 7. Risks & open questions
What could go wrong — technically, in adoption, or strategically? What's still genuinely unresolved that the PRD should flag rather than paper over? Open questions are a feature of a good PRD, not a flaw — don't pressure the user into fake certainty here.

## When to stop asking and start writing

You have enough once you can write a specific, testable sentence for each section above. If you're about to write something generic enough to apply to almost any product ("this will improve the user experience"), that's the signal you need one more question, not that you should soften the language and move on.

Before drafting, give the user a compact recap of everything gathered — a few lines per section — and ask if anything's missing or wrong. This catches gaps before they're baked into a full document, which is much cheaper to fix at the recap stage than after.

## Writing the PRD

Once confirmed, write the file using this structure:

```markdown
# [Product/Feature Name] — PRD

**Author:** [user, if known] | **Date:** [today's date] | **Status:** Draft

## Problem & Context

## Goals
## Non-Goals

## Target Users & Use Cases

## Requirements
### Functional
### Non-Functional

## Success Metrics

## Scope & Timeline

## Risks & Open Questions
```

Notes on filling it in:
- Write requirements as a numbered or bulleted list of discrete, testable statements (e.g., "Users can filter results by date range" not "good filtering support").
- In Risks & Open Questions, list genuinely unresolved items as questions, not disguised statements — if the user shrugged on something, say so.
- Keep the whole document as tight as the content allows. A clear one-page PRD beats a padded five-page one; don't invent detail to fill sections that legitimately have little to say (e.g., a simple internal tool may need only a line or two under Non-Functional Requirements).

Save the file as `<feature-name>-prd.md` (kebab-case) in the user's working folder, or wherever they indicate the PRD should live.

## After writing

Point out anything you marked as an assumption or open question, since those are the parts most likely to need a second pass from the user or a stakeholder. Offer to revise any section based on feedback rather than treating the first draft as final.
