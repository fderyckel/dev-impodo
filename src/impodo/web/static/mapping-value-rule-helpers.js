"use strict";

window.impodoMappingValueRuleHelpers = (() => {
  const displayPreviewValue = (value) => {
    if (value === null || value === undefined) {
      return "(empty)";
    }
    if (value === "") {
      return '""';
    }
    return String(value);
  };

  const defaultTextStep = (preset) => ({
    kind:
      preset === "remove_separators_between_digits"
        ? "remove_separators_between_digits"
        : "find_replace",
    search_value: "",
    replacement_value: "",
    search_mode: ["starts_with", "ends_with", "pattern"].includes(preset)
      ? preset
      : "literal",
    replace_all: !["starts_with", "ends_with"].includes(preset),
    characters:
      preset === "remove_separators_between_digits" ? " .-" : "",
  });

  return { displayPreviewValue, defaultTextStep };
})();
