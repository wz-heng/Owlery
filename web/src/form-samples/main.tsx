/** Entry for the dev-only sample room. Pulls the *real* token and app
 * stylesheets so the candidates sit on the true parchment ground with
 * the true type stack and the true `.markdown` body rules — the sheet
 * is only evidence if nothing here is a mock-up of the theme. */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../styles/tokens.css";
import "../index.css";
import { SampleRoom } from "./SampleRoom";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SampleRoom />
  </StrictMode>
);
