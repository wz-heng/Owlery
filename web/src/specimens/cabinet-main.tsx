import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "../styles/tokens.css";
import "../styles/shape.css";
import "../index.css";
import "./streaming-anatomy/anatomy.css";
import "./agent-delegation/delegation.css";
import "./bg-task-pipeline/pipeline.css";
import "./shared/later.css";
import "./function-cabinet/cabinet.css";
import { CabinetApp } from "./CabinetApp";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <CabinetApp />
  </StrictMode>,
);
