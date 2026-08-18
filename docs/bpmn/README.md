# Impodo BPMN models

These BPMN 2.0 models describe the workflows currently implemented in Impodo.
They are documentation models, not executable workflow definitions.

## Model set

Open the overview first:

- [Current end-to-end workflow](current/impodo-current-workflow.bpmn)

![Current end-to-end Impodo BPMN overview.](current/impodo-current-workflow.png)

The overview calls the detailed workflow models:

1. [Project setup](current/00-project-setup.bpmn)
2. [Source data](current/01-source-data.bpmn)
3. [Odoo data](current/02-odoo-data.bpmn)
4. [Match data](current/03-match-data.bpmn)
5. [Prepare data](current/04-prepare-data.bpmn)
6. [Final review](current/05-final-review.bpmn)
7. [Load into Odoo](current/06-load-into-odoo.bpmn)

The files use standard BPMN 2.0 XML and BPMN Diagram Interchange coordinates.
They can be opened in BPMN-compatible tools such as the bpmn.io modeler or
Camunda Modeler.

## Current capability boundary

The overview deliberately shows two source-mode paths:

- a file-source project follows **Source data → Odoo data → Match data →
  Prepare data → Final review → Load into Odoo**;
- an Odoo-source project follows **Odoo source data → Freeze Odoo records**
  and then reaches the current implemented boundary. Odoo-source mapping and
  round-trip update are planned, not current.

The load process models only the current approved disposable local or remote
Odoo 19 target capability. It does not represent production cutover.

## BPMN conventions

- **User task:** a decision or action performed by the data manager in the
  Impodo browser.
- **Service task:** automated behavior performed by Impodo.
- **Exclusive gateway:** a decision with one selected path.
- **Collapsed call activity:** a link from the overview to a detailed workflow.
- **Message flow:** a bounded interaction between Impodo and the exact Odoo 19
  target.
- **End event:** a completed stage, a needs-attention state, or an explicit
  current product boundary.

The models use two lanes, **Data manager** and **Impodo**, where responsibility
needs to be explicit. Odoo is a separate black-box participant because Impodo
calls it through narrow supported interfaces rather than controlling its
internal process.

## Authority and maintenance

The models summarize current user and developer workflow documentation. The
accepted contracts and implementation remain authoritative where a diagram is
ambiguous.

Regenerate the diagrams after changing the model definitions:

```console
uv run python scripts/generate_current_bpmn.py
```

Check that committed files match the generator:

```console
uv run python scripts/generate_current_bpmn.py --check
```

Also run the documentation checks listed in the main
[documentation index](../README.md).
