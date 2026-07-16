/** Entry for the dev-only §4 family sheet. See FamilySheet.tsx. */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../styles/tokens.css";
import "../styles/shape.css";
import "../index.css";
import { FamilySheet } from "./FamilySheet";

// A design harness wants fixed data, not a live backend. Every call
// resolves 404 so the sidebar's mount fetches fall back to the seeded
// store instead of rejecting into an unhandled promise.
window.fetch = (async () =>
  new Response("null", { status: 404 })) as typeof window.fetch;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FamilySheet />
  </StrictMode>
);
