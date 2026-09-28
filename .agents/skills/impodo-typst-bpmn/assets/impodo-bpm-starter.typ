// Copy this file into docs/bpmn/typst/ and replace the example content.
// The starter intentionally uses only Typst's built-in layout primitives.

#set page(
  width: 297mm,
  height: 210mm,
  margin: (x: 14mm, y: 12mm),
)

#let ink = rgb("#18324B")
#let muted = rgb("#5C6F82")
#let user_fill = rgb("#E8F3FF")
#let impodo_fill = rgb("#EAF7EE")
#let decision_fill = rgb("#FFF5D8")
#let result_fill = rgb("#F2EEFF")

#set text(size: 9pt, fill: ink)

#let card(fill, heading, detail) = block(
  width: 48mm,
  height: 25mm,
  inset: 3mm,
  radius: 2mm,
  fill: fill,
  stroke: (paint: ink, thickness: 0.6pt),
)[
  #align(center)[
    *#heading*
    #v(2pt)
    #text(size: 7.5pt, fill: muted)[#detail]
  ]
]

#let arrow = box(width: 9mm)[
  #align(center)[#text(size: 16pt, fill: muted)[→]]
]

#let down = align(center)[#text(size: 16pt, fill: muted)[↓]]

= Example workflow title

#text(fill: muted)[
  One sentence that tells the reader what they can understand or decide from
  this page.
]

#v(7mm)

#grid(
  columns: (20mm, 48mm, 9mm, 48mm, 9mm, 48mm),
  align: center,
  [*You*],
  [#card(user_fill, "1. Start", "Describe the first action.")],
  [#arrow],
  [#card(user_fill, "2. Decide", "State the business choice.")],
  [#arrow],
  [#card(user_fill, "3. Confirm", "Use the exact control label if needed.")],
)

#v(3mm)
#down
#v(3mm)

#grid(
  columns: (20mm, 48mm),
  align: center,
  [*Impodo*],
  [#card(impodo_fill, "4. Impodo creates", "Explain the saved result in plain language.")],
)

#v(3mm)
#down
#v(3mm)

#grid(
  columns: (20mm, 48mm, 9mm, 48mm, 9mm, 48mm),
  align: center,
  [*You*],
  [#card(user_fill, "5. Continue", "Name the next action.")],
  [#arrow],
  [#card(decision_fill, "Which path?", "State one clear business question.")],
  [#arrow],
  [#card(result_fill, "Next stage", "Name the next useful outcome.")],
)

#v(7mm)

#block(
  inset: 3mm,
  radius: 2mm,
  fill: rgb("#F5F8FB"),
)[
  *Scope note:* State an important boundary here when it prevents confusion.
  For example, say that project creation does not inspect source rows or write
  to Odoo.
]
