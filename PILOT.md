# ReviewOps researcher pilot

ReviewOps is a local workflow prototype. Its intended users are researchers coordinating experiment evidence and review decisions. The startup cases and import example are synthetic. No researcher pilot has been performed, and no Millennium workflow has been validated yet.

## Questions for the first conversation

1. Walk through the last experiment you handed to someone else. Where were the inputs, outputs, review comments and final decision?
2. What made it hard to decide whether the run was ready for review?
3. What evidence must be present before you would approve the review?
4. Have changed files or an unclear version caused a mistake or repeated work?
5. Which part of this prototype would help, and which part would you remove?

Do not collect confidential files for a public demo. Record the user’s explanation of the workflow and use permitted examples.

## Tasks

Try the current process and ReviewOps on matched synthetic or permitted cases. Alternate task order where practical; repeated tasks introduce learning effects.

| Task | Observable outcome |
|---|---|
| Find a run that is ready for review | Correct evidence and completeness identified |
| Detect the missing evidence | Missing file noticed before an approval |
| Detect changed evidence | Manifest mismatch correctly interpreted |
| Assign a reviewer and request a change | Assignee and decision note persist after restart |
| Submit revised evidence | Old approval does not carry over; old files stay readable |
| Explain the previous decision | Correct actor label, note and evidence snapshot found |
| Export the audit | Another reader can reconstruct the recorded decisions |

Measure actual elapsed time, task success, and mistakes. Do not fill in numbers in advance. A few participants can provide useful feedback but do not establish a general productivity improvement.

## Record after each session

- Date, participant role, and permission to quote them:
- Current workflow:
- Case and task:
- Time and result using the existing process:
- Time and result using ReviewOps:
- Errors or hesitation:
- What the participant said:
- One change to make:
- What remains uncertain:

## First improvement

Select one change supported by the session. State the original behavior, intended behavior, acceptance test and result. Keep the first unsuccessful attempt if it explains a design decision.

## Publication checklist

The local tool has no authentication and no multiuser hosting. Before a real team deployment, decide identity/access control, data handling, shared storage and operating ownership. Public examples stay synthetic or explicitly permitted. Describe AI assistance accurately and include measured findings only after the pilot occurs.
