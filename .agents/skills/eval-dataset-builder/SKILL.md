---
name: eval-dataset-builder
description: Analyze the current Agent project and design, generate, validate, and extend evaluation datasets based on real project capabilities, business rules, knowledge sources, existing tests, and known failures.
---

# Purpose

Build evaluation datasets grounded in the actual project.

The goal is not to maximize the number of generated cases.

The goal is to maximize meaningful scenario coverage while keeping expected results verifiable.

# Instruction Priority

The user's explicit instructions take precedence over this skill.

Do not change existing project code, evaluation scripts, schemas, or datasets unless the user explicitly requests modification.

# Workflow

Always follow the phases below in order.

## Phase 1: Project Discovery

Before generating any dataset cases, inspect the relevant project implementation.

For RAG evaluation, inspect at least:

- RAG retrieval implementation
- query builder
- embedding implementation
- reranker
- knowledge base documents
- current `rag_eval.jsonl`
- current RAG evaluation script
- existing RAG evaluation reports if available

Identify:

- retrieval pipeline
- searchable knowledge sources
- source names
- section names
- business rules
- query preprocessing
- retrieval filters
- reranking rules
- top-k behavior
- currently evaluated metrics
- existing dataset schema
- known failures

Do not invent project capabilities.

Do not invent business rules.

## Phase 2: Existing Schema Analysis

Read the existing evaluation dataset before proposing changes.

Determine:

- current fields
- required fields
- optional fields
- expected labels
- how the evaluation script consumes each field

Prefer compatibility with the existing schema.

If a schema change is recommended, explain:

1. why the current schema is insufficient;
2. what new field is required;
3. how the evaluation script would use it.

Do not modify the schema automatically.

## Phase 3: Scenario Matrix

Before generating individual cases, create a scenario matrix.

Each scenario must represent a meaningful difference in system behavior rather than only a wording difference.

Classify scenarios using categories such as:

- happy_path
- boundary
- negative
- ambiguity
- hard_negative
- failure
- adversarial
- out_of_scope
- regression

For RAG evaluation, consider dimensions including:

- business topic
- user intent
- query specificity
- terminology variation
- knowledge source
- section
- evidence requirement
- business state
- ambiguity
- irrelevant competing documents
- missing knowledge

Every scenario should have:

- scenario_id
- description
- capability
- category
- priority
- expected knowledge source
- expected section when applicable
- expected behavior
- ground truth source

## Phase 4: Coverage Analysis

Compare the scenario matrix with the existing dataset.

Classify scenarios as:

- covered
- weakly_covered
- uncovered

Distinguish between:

- scenario variation
- linguistic variation

Multiple paraphrases of the same business situation do not count as multiple scenario types.

Produce a coverage report before generating new cases.

## Phase 5: Case Generation

Generate new cases only for uncovered or weakly covered scenarios unless the user explicitly requests otherwise.

Each case must be traceable to a scenario.

Prefer:

1. new meaningful scenarios;
2. important boundary cases;
3. regression cases from real failures;
4. hard negatives;
5. linguistic variants.

Do not generate large numbers of superficial paraphrases.

Expected answers and labels must be grounded in one of:

- project knowledge base
- project code
- database fixture
- existing explicit business rule
- verified regression behavior

If ground truth cannot be established, mark the case as requiring review instead of inventing a label.

## Phase 6: Validation

Before recommending that generated cases be added to the dataset, validate:

- JSON structure
- required fields
- duplicate case IDs
- duplicate or near-duplicate cases
- unsupported source names
- unsupported section names
- contradictory labels
- unsupported ground truth
- impossible project capabilities

Do not overwrite the existing golden dataset.

Generate candidate cases separately first.

## Phase 7: Report

Always report:

- existing case count
- scenario count
- covered scenarios
- weakly covered scenarios
- uncovered scenarios
- proposed new cases
- duplicate or redundant cases
- schema issues
- ground truth issues

When generation is requested, clearly distinguish:

- existing cases
- generated candidate cases
- regression cases
- cases requiring human review

# Dataset Principles

Prefer scenario diversity over case count.

Prefer real project failures over synthetic edge cases.

Prefer deterministic expected results over subjective labels.

Do not let the model generate both an unsupported business rule and the expected answer based on that invented rule.

Do not modify frozen golden evaluation data automatically.

Candidate generated data should be reviewed before promotion into the golden evaluation set.
## Dataset Split Rules

Distinguish dataset role from dataset schema version.

A dataset repeatedly used to modify prompts, retrieval logic,
ranking rules, workflow, or parameters is a development/tuning set,
not a holdout test set.

Do not use development-set performance as the final generalization result.

Holdout cases must not be used to guide routine prompt, workflow,
retrieval, reranking, or parameter optimization.

When analyzing coverage, report development-set coverage and
holdout-set coverage separately.

Do not recommend moving holdout cases into the development set
simply to improve development coverage.

Schema versions may be unified without merging dataset roles.