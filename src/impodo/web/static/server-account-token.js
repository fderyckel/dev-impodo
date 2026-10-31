"use strict";

// Fragments are never sent to the server or included in proxy access logs.
const tokenField = document.getElementById("account-token");
if (tokenField && window.location.hash.length > 1) {
  const token = window.location.hash.slice(1);
  if (token.length <= 1024) tokenField.value = token;
  window.history.replaceState(null, "", window.location.pathname);
}
