# sample-desk (ai-factory rehearsal project)

A small quality-control lab app, used to rehearse ai-factory end to end before a large
run. The planning documents in `specs/001-initial/` are already written and approved:
`scripts/rehearsal.py` pushes this directory into a fresh repository, creates a project
from it on a running ai-factory, approves the documents and the plan, and follows it to
the milestone.

It is deliberately small (a few hours) but covers what went wrong in the first large
run: a full-stack app that must start (`scripts/start.sh`), a real user interface with
named workflows and a component library, demo data, a chart, an import, and a design
with decisions the Reviewer can hold the code to.
