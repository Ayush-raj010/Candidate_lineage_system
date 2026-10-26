# Engineering Judgment Note: Candidate Triage System

## Context

The system needs to process approximately 3,000 candidate records per
week. Delivery is constrained to two weeks, with one additional engineer
available. The goal should be a reliable, useful first release---not a
large feature set delivered without adequate validation.

## Priorities and delivery plan

### Week 1: Deliver the core workflow

1.  **Confirm requirements and data quality.** Agree on the candidate
    fields, expected search and filtering behavior, ranking inputs, and
    how recruiters will judge a useful result. Inspect sample data for
    missing, inconsistent, or duplicated values.
2.  **Build the critical path.** Prioritize candidate ingestion,
    persistent storage, candidate search and filtering, and a
    transparent skill-overlap ranking method. Return clear validation
    errors and handle missing fields safely.
3.  **Make ranking explainable.** Show which required skills matched,
    which are missing, and how the score is calculated. Document
    algorithmic time and space complexity and benchmark representative
    workloads rather than relying only on theoretical estimates.

### Week 2: Validate, harden, and deliver

1.  **Test the end-to-end workflow.** Cover normal cases, empty results,
    invalid inputs, duplicate imports, missing data, and API errors.
    Verify that recruiter actions persist correctly.
2.  **Measure performance.** Benchmark realistic candidate volumes and
    common query patterns. Add or adjust database indexes based on query
    plans and measurements. If search becomes a bottleneck, evaluate
    full-text search or a dedicated search index instead of prematurely
    introducing more infrastructure.
3.  **Add AI summaries carefully.** Treat model output as decision
    support, not an authoritative hiring decision. Ground claims in
    candidate-record evidence, expose uncertainty and missing
    information, handle provider failures, and cache results where
    appropriate. Do not send unnecessary personal data to the model.
4.  **Prepare the handoff.** Document setup, environment variables, API
    behavior, limitations, test results, and known risks. Demonstrate
    the complete workflow before release.

## Division of work

With one additional engineer, agree on ownership early to avoid
duplicated effort and late integration. One person can focus on the API,
database, and ranking implementation while the other focuses on the
interface, integration tests, and usability; both should review the
interfaces between components and jointly validate the final workflow.
Adjust the split to match the team's actual strengths.

Use small, reviewable changes and frequent integration. Agree on API
contracts before frontend work depends on them, and keep a short list of
blockers and decisions.

## Scale and technical trade-offs

A volume of 3,000 candidates per week is a useful starting estimate, but
weekly volume alone does not determine the required architecture.
Concurrent users, candidate-record size, query frequency, retention
requirements, and latency expectations also matter.

Start with a simple relational database and indexes suited to observed
filters and joins. Measure ingestion and search performance using
representative data. Keep ranking results explainable and compare
optimized implementations against a straightforward reference
implementation. Introduce more complex infrastructure only when
measurements or availability requirements justify its operational cost.

## Risks and mitigations

-   **Inconsistent candidate data:** validate imports, preserve source
    records, and report missing or conflicting information.
-   **Misleading ranking or AI output:** show matched and missing
    skills, ground claims in evidence, and keep a human recruiter
    responsible for decisions.
-   **Performance regressions:** maintain repeatable benchmarks and test
    realistic data volumes.
-   **Scope and schedule risk:** prioritize the core workflow; defer
    nonessential features until the critical path is tested.
-   **Security and privacy:** protect credentials and secrets, enforce
    access controls where authentication is required, minimize sensitive
    data exposure, and avoid logging secrets.
-   **Unverified assumptions:** distinguish measured results from
    estimates and document limitations explicitly.

## Definition of done

The first release is ready when the core workflow runs end to end,
imports and searches behave correctly, ranking output is explainable,
errors are handled clearly, automated tests pass, performance has been
measured at a representative scale, and another developer can follow the
documented setup instructions. Any unimplemented requirement or known
limitation should be stated plainly.

## Assumptions

This note assumes the two-week period is for a first usable release and
that the 3,000-candidate figure is weekly intake. Exact service-level
targets, concurrency, data-retention rules, and team responsibilities
were not specified; they should be confirmed before making stronger
capacity or architecture commitments.
