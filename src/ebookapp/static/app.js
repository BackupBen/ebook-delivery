// Kleine Komfortfunktionen der Verwaltung. Alles funktioniert auch ohne JavaScript:
// Texte bleiben markierbar, Formulare sind normale HTML-Formulare.
(function () {
  "use strict";

  function textOf(element) {
    if ("value" in element && typeof element.value === "string") {
      return element.value;
    }
    return element.textContent || "";
  }

  function fallbackCopy(element) {
    if (typeof element.select === "function") {
      element.select();
    } else {
      var range = document.createRange();
      range.selectNodeContents(element);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
    try {
      return document.execCommand("copy");
    } catch (error) {
      return false;
    }
  }

  function report(button, ok) {
    var status = document.getElementById(button.getAttribute("data-copy-status") || "");
    var message = ok ? "Kopiert." : "Kopieren nicht möglich. Bitte den Text markieren und kopieren.";
    if (status) {
      status.textContent = message;
    } else {
      var original = button.getAttribute("data-label") || button.textContent;
      button.setAttribute("data-label", original);
      button.textContent = ok ? "Kopiert" : "Bitte manuell kopieren";
      window.setTimeout(function () {
        button.textContent = original;
      }, 2500);
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) {
      return;
    }
    var source = document.getElementById(button.getAttribute("data-copy"));
    if (!source) {
      return;
    }
    var text = textOf(source);
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () {
          report(button, true);
        },
        function () {
          report(button, fallbackCopy(source));
        }
      );
    } else {
      report(button, fallbackCopy(source));
    }
  });

  // Fehlermeldungen bleiben stehen; der Fokus springt dorthin, damit sie nicht übersehen werden.
  var errorBox = document.getElementById("error-box");
  if (errorBox) {
    errorBox.focus();
  }
})();
