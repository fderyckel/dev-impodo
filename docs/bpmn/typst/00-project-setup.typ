// Print with an active virtual environment:
// typst compile docs/bpmn/typst/00-project-setup.typ docs/bpmn/typst/00-project-setup.pdf

#set page(
  width: 297mm,
  height: 210mm,
  margin: (x: 14mm, y: 12mm),
)

#let ink = rgb("#18324B")
#let muted = rgb("#5C6F82")
#let user_fill = rgb("#E8F3FF")
#let impodo_fill = rgb("#EAF7EE")
#let button_fill = rgb("#6B3FA0")

#set text(size: 8.5pt, fill: ink)

#let button(label) = box(
  inset: (x: 1.5mm, y: 0.55mm),
  radius: 1.2mm,
  fill: button_fill,
)[
  #text(size: 6.6pt, weight: "bold", fill: white)[#label]
]

#let card(fill, heading, detail, width: 58mm) = box(
  width: width,
  inset: (x: 2.5mm, y: 1.5mm),
  radius: 2mm,
  fill: fill,
  stroke: (paint: ink, thickness: 0.6pt),
)[
  #align(center)[
    *#heading*
    #v(2pt)
    #text(size: 7.2pt, fill: muted)[#detail]
  ]
]

#let arrow = block(width: 10mm)[
  #align(center)[#text(size: 16pt, fill: muted)[→]]
]

#let down = align(center)[#text(size: 12pt, fill: muted)[↓]]

= Stage 0: Create a data project

#text(fill: muted)[
  Follow the blue actions. Impodo performs the green actions in response. At
  the end, continue with the source-data stage that matches your choice.
]

#v(4mm)

#align(center)[
  #grid(
    columns: (78mm, 78mm),
    column-gutter: 6mm,
    [#block(
      inset: (x: 3mm, y: 2mm),
      radius: 2mm,
      fill: user_fill,
    )[*Blue: what you do*]],
    [#block(
      inset: (x: 3mm, y: 2mm),
      radius: 2mm,
      fill: impodo_fill,
    )[*Green: what Impodo does with your choices*]],
  )
]

#v(4mm)

#align(center)[
  #grid(
    columns: (16mm, 58mm, 10mm, 58mm, 10mm, 58mm, 16mm),
    align: horizon + center,
    [*You*],
    [#card(
      user_fill,
      "1. Describe the migration",
      [
        #align(left)[
          #text(fill: button_fill)[•] Click #button("New project")
          #linebreak()
          #text(fill: button_fill)[•] Name the migration
          #linebreak()
          #text(fill: button_fill)[•] Explain its goal
        ]
      ],
    )],
    [#arrow],
    [#card(
      user_fill,
      "2. Choose the source",
      "Choose Files or Data already in Odoo.",
    )],
    [#arrow],
    [#card(
      user_fill,
      "3. Confirm the project",
      [Click #button("Create project") to confirm the migration scope.],
    )],
    [],
  )
]

#v(1mm)
#down
#v(1mm)

#align(center)[
  #grid(
    columns: (16mm, 66mm, 10mm, 66mm, 16mm),
    align: horizon + center,
    [*Impodo*],
    [#card(
      impodo_fill,
      "Uses your project choices",
      "Keeps the migration name, purpose, and source choice together.",
      width: 66mm,
    )],
    [#arrow],
    [#card(
      impodo_fill,
      "Creates your work area",
      "Creates the data project, first Data version, and workspace.",
      width: 66mm,
    )],
    [],
  )
]

#v(1mm)
#down
#v(1mm)

#align(center)[
  #grid(
    columns: (16mm, 62mm, 16mm),
    align: horizon + center,
    [*You*],
    [#card(
      user_fill,
      "4. Continue to the workspace",
      [Click #button("Open workspace") to continue from the data project overview.],
      width: 62mm,
    )],
    [],
  )
]

#v(1mm)
#down
#v(1mm)

#align(center)[
  #grid(
    columns: (16mm, 86mm, 16mm),
    align: horizon + center,
    [*Impodo*],
    [#card(
      impodo_fill,
      "Stage 0 is complete",
      "Your Authoring workspace is ready for the source-data work.",
      width: 86mm,
    )],
    [],
  )
]

#v(2mm)

#align(center)[
  #text(size: 8pt, fill: muted)[*Next stages — use the diagram for your source.*]
]

#v(1mm)

#align(center)[
  #grid(
    columns: (82mm, 18mm, 82mm),
    align: horizon + center,
    [#align(center)[#text(size: 14pt, fill: muted)[↙]]],
    [],
    [#align(center)[#text(size: 14pt, fill: muted)[↘]]],
  )
]

#v(1mm)

#align(center)[
  #grid(
    columns: (82mm, 18mm, 82mm),
    align: horizon + center,
    [#card(
      user_fill,
      "Stage 1: Source data",
      "Continue with the files you chose for this data project.",
      width: 82mm,
    )],
    [],
    [#card(
      user_fill,
      "Stage 2: Odoo data",
      "Add the Odoo address, database login details, and read-only API key for the instance whose data you will fetch.",
      width: 82mm,
    )],
  )
]

#v(4mm)

#block(
  inset: 3mm,
  radius: 2mm,
  fill: impodo_fill,
)[
  *During project setup, your data stays unchanged.* Impodo does not inspect
  source rows, contact Odoo, create a Recipe, or write records to Odoo.
]
