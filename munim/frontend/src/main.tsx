import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// ?view=merchant opens the shop owner's side (phone frame); everything else is the team view.
// Loaded on demand so its code and CSS never reach the team view.
const MerchantApp = lazy(() => import("./merchant/MerchantApp"));
const merchant = new URLSearchParams(window.location.search).get("view") === "merchant";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {merchant ? (
      <Suspense fallback={null}>
        <MerchantApp />
      </Suspense>
    ) : (
      <App />
    )}
  </StrictMode>,
);
