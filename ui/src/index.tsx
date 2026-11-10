import { render } from "solid-js/web";
import { App } from "./App";
import "./styles.css";

window.addEventListener(
  "wheel",
  (e) => {
    if (e.ctrlKey) e.preventDefault();
  },
  { passive: false },
);

const root = document.getElementById("root");
if (root) {
  render(() => <App />, root);
}
