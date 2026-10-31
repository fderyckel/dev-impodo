"""Independent source rows and expected business paths for hierarchy acceptance."""

CATEGORY_ROWS = (
    ("C100", "Machines", ""),
    ("C110", "Pumps", "C100"),
    ("C111", "Service", "C110"),
    ("C112", "Seals", "C111"),
    ("C120", "Valves", "C100"),
    ("C121", "Service", "C120"),
    ("C122", "Seals", "C121"),
    ("C200", "Maintenance", ""),
    ("C210", "Pumps", "C200"),
    ("C211", "Service", "C210"),
    ("C212", "Seals", "C211"),
    ("C300", "Tools", ""),
)

PRODUCT_CATEGORY_KEYS = {
    "PX101": "C112",
    "PX102": "C122",
    "PX103": "C212",
    "PX104": "C110",
    "PX105": "C100",
    "PX106": "C300",
}

EXPECTED_PRODUCT_PATHS = {
    "PX101": ("Machines", "Pumps", "Service", "Seals"),
    "PX102": ("Machines", "Valves", "Service", "Seals"),
    "PX103": ("Maintenance", "Pumps", "Service", "Seals"),
    "PX104": ("Machines", "Pumps"),
    "PX105": ("Machines",),
    "PX106": ("Tools",),
}

EXPECTED_CATEGORY_PATHS = {
    ("Machines",),
    ("Machines", "Pumps"),
    ("Machines", "Pumps", "Service"),
    ("Machines", "Pumps", "Service", "Seals"),
    ("Machines", "Valves"),
    ("Machines", "Valves", "Service"),
    ("Machines", "Valves", "Service", "Seals"),
    ("Maintenance",),
    ("Maintenance", "Pumps"),
    ("Maintenance", "Pumps", "Service"),
    ("Maintenance", "Pumps", "Service", "Seals"),
    ("Tools",),
}


def supplied_files(rows, *, categories=CATEGORY_ROWS):
    """Supply physical parent keys independently of Impodo's hierarchy generator."""

    return {
        "products": (
            "Reference,Category\n"
            + "\n".join(f"{row[0]},{PRODUCT_CATEGORY_KEYS[row[0]]}" for row in rows)
            + "\n"
        ).encode(),
        "categories": (
            "Key,Name,Parent\n" + "\n".join(",".join(row) for row in categories) + "\n"
        ).encode(),
    }
