# Typst BPM print-layout trial

This folder contains print-friendly Typst versions of Impodo workflow diagrams.
The first trial is [Project setup](00-project-setup.typ). It is written for a
data manager who wants to understand the journey without reading BPMN XML.

The editable [BPMN 2.0 model](../current/00-project-setup.bpmn) remains the
semantic workflow model. The Typst file is its one-page presentation for
printing and discussion.

This project uses a portable Typst CLI at `.venv/tools/typst/typst.exe`. When
the virtual environment is active, its ignored `typst.cmd` shim makes the
normal `typst` command available. The virtual environment and generated PDF
are ignored by Git. Check the installed version with:

```powershell
typst --version
```

Create the PDF with:

```powershell
typst compile docs/bpmn/typst/00-project-setup.typ docs/bpmn/typst/00-project-setup.pdf
```

Activate the virtual environment before using `typst` in a new terminal:

```powershell
.\.venv\Scripts\Activate.ps1
```

Print the PDF in landscape at actual size or fit it to a single page.
