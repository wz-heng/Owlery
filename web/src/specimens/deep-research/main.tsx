import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../styles/tokens.css";
import "../../styles/shape.css";
import "../../index.css";
import "../streaming-anatomy/anatomy.css";
import "../shared/later.css";
import { LaterSpecimen } from "../shared/LaterSpecimen";
createRoot(document.getElementById("root")!).render(<StrictMode><LaterSpecimen id="research" /></StrictMode>);
