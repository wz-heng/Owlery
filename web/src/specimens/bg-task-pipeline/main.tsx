import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "../../styles/tokens.css";
import "../../styles/shape.css";
import "../../index.css";
import "../streaming-anatomy/anatomy.css";
import "./pipeline.css";
import { BgTaskPipelineSpecimen } from "./BgTaskPipelineSpecimen";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BgTaskPipelineSpecimen />
  </StrictMode>
);
