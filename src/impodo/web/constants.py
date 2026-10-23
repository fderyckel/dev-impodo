"""HTTP limits, paging choices, and UI option constants."""

import re

SOURCE_SYSTEMS = (
    "Dynamics AX 2012",
    "Dynamics 365",
    "Odoo 19",
    "Odoo 20",
    "Salesforce",
    "Excel or manual files",
    "Another ERP or CRM",
    "Other",
)
ODOO_APPLICATIONS = (
    "Accounting",
    "Contacts",
    "Inventory",
    "Manufacturing",
    "Purchase",
    "Sales",
    "Custom applications",
)
TRANSFORMATION_IMPACT_PAGE_SIZE = 100
TRANSFORMATION_IMPACT_OUTCOMES = {
    "changed",
    "fallback",
    "null",
    "invalid",
    "provided",
}
_MANUAL_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_MANUAL_FIELD_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAPPING_MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAPPING_MAX_FORM_FIELDS = 25_000
MAPPING_MAX_JSON_ENTRIES = 10_000
MAPPING_MAX_FORM_NAME_LENGTH = 256
MAPPING_MAX_FORM_VALUE_LENGTH = 64 * 1024
DEFAULT_MAPPING_FIELDS_PER_PAGE = 3
MAPPING_FIELD_PAGE_SIZES = (3, 10, 20, 50)
VALUE_MATCH_MAX_SOURCE_CHOICES = 500
VALUE_MATCH_MAX_TARGET_CHOICES = 2_000
DEFAULT_SUMMARY_ROWS_PER_PAGE = 20
SUMMARY_ROW_PAGE_SIZES = (10, 20, 50, 100)
NORMALIZATION_GROUPS_PER_PAGE = 50
DEFAULT_LOAD_ROWS_PER_PAGE = 20
LOAD_ROW_PAGE_SIZES = (20, 50)
_APPLICATION_MODULE_PREFIXES = {
    "Accounting": ("account", "analytic"),
    "Contacts": ("contacts",),
    "Inventory": ("stock", "product", "uom"),
    "Manufacturing": ("mrp", "maintenance", "quality"),
    "Purchase": ("purchase",),
    "Sales": ("sale", "crm"),
}
__all__ = [
    "DEFAULT_LOAD_ROWS_PER_PAGE",
    "DEFAULT_MAPPING_FIELDS_PER_PAGE",
    "DEFAULT_SUMMARY_ROWS_PER_PAGE",
    "LOAD_ROW_PAGE_SIZES",
    "MAPPING_FIELD_PAGE_SIZES",
    "MAPPING_MAX_FORM_FIELDS",
    "MAPPING_MAX_FORM_NAME_LENGTH",
    "MAPPING_MAX_FORM_VALUE_LENGTH",
    "MAPPING_MAX_JSON_ENTRIES",
    "MAPPING_MAX_REQUEST_BYTES",
    "NORMALIZATION_GROUPS_PER_PAGE",
    "ODOO_APPLICATIONS",
    "SOURCE_SYSTEMS",
    "SUMMARY_ROW_PAGE_SIZES",
    "TRANSFORMATION_IMPACT_OUTCOMES",
    "TRANSFORMATION_IMPACT_PAGE_SIZE",
    "VALUE_MATCH_MAX_SOURCE_CHOICES",
    "VALUE_MATCH_MAX_TARGET_CHOICES",
    "_APPLICATION_MODULE_PREFIXES",
    "_MANUAL_FIELD_NAME",
    "_MANUAL_FIELD_TYPE",
]
