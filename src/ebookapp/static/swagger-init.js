// Startet die interaktive API-Dokumentation mit lokal ausgelieferten Dateien (kein CDN).
window.addEventListener("DOMContentLoaded", function () {
  "use strict";
  window.ui = SwaggerUIBundle({
    url: "/api/v1/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    persistAuthorization: false,
    tryItOutEnabled: false,
    defaultModelsExpandDepth: 0,
    docExpansion: "list",
    validatorUrl: null,
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout"
  });
});
