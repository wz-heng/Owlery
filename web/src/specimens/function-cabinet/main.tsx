import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../styles/tokens.css";
import "../../styles/shape.css";
import "../../index.css";
import "../streaming-anatomy/anatomy.css";
import "./cabinet.css";
import { FunctionCabinet } from "./FunctionCabinet";
createRoot(document.getElementById("root")!).render(<StrictMode><FunctionCabinet /></StrictMode>);
